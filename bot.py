#!/usr/bin/env python3
"""
bot.py — Telegram bot for automated Open Redirect scanning via GitHub Actions.

Features:
  • Send a single URL or multi-line message with many URLs
  • Upload a .txt file with any number of targets
  • Targets are split into batches of BATCH_SIZE (default 50)
  • Each batch triggers a GitHub Actions workflow_dispatch
  • Bot polls until the workflow completes, then reports results to Telegram
  • Works sequentially: batch-1 → results → batch-2 → results → …
"""

import asyncio
import io
import json
import logging
import re
import time
from typing import List, Optional, Tuple

import requests
from telegram import Update, Document
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("open-redirect-bot")

# ── GitHub Actions helpers ────────────────────────────────────────────────────

GH_HEADERS = {
    "Authorization": f"Bearer {config.GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _gh_url(path: str) -> str:
    return f"https://api.github.com/repos/{config.GITHUB_REPO}/{path}"


def trigger_workflow(targets: List[str], batch_label: str) -> bool:
    """Trigger workflow_dispatch and return True on success."""
    targets_str = "\n".join(targets)
    payload = {
        "ref": config.GITHUB_BRANCH,
        "inputs": {
            "targets": targets_str,
            "batch_id": batch_label,
        },
    }
    url = _gh_url(f"actions/workflows/{config.GITHUB_WORKFLOW_ID}/dispatches")
    resp = requests.post(url, json=payload, headers=GH_HEADERS, timeout=30)
    if resp.status_code == 204:
        logger.info("Workflow triggered for batch %s", batch_label)
        return True
    logger.error("Failed to trigger workflow: %s %s", resp.status_code, resp.text)
    return False


def get_latest_run_id(after_ts: float) -> Optional[int]:
    """Return the run ID of the most recent workflow_dispatch triggered after `after_ts`."""
    url = _gh_url(
        f"actions/workflows/{config.GITHUB_WORKFLOW_ID}/runs?per_page=5&event=workflow_dispatch"
    )
    for attempt in range(10):
        time.sleep(5)
        resp = requests.get(url, headers=GH_HEADERS, timeout=30)
        if resp.status_code != 200:
            continue
        runs = resp.json().get("workflow_runs", [])
        for run in runs:
            created = run.get("created_at", "")
            # parse ISO8601
            import datetime
            try:
                ts = datetime.datetime.fromisoformat(
                    created.replace("Z", "+00:00")
                ).timestamp()
            except Exception:
                ts = 0
            if ts >= after_ts - 5:  # 5s tolerance
                return run["id"]
    return None


def wait_for_run(run_id: int) -> Tuple[str, str]:
    """
    Poll until the run completes.
    Returns (conclusion, html_url).
    conclusion is one of: success, failure, cancelled, timed_out, …
    """
    url = _gh_url(f"actions/runs/{run_id}")
    deadline = time.time() + config.POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(config.POLL_INTERVAL)
        resp = requests.get(url, headers=GH_HEADERS, timeout=30)
        if resp.status_code != 200:
            continue
        data = resp.json()
        status = data.get("status")
        conclusion = data.get("conclusion")
        html_url = data.get("html_url", "")
        logger.info("Run %d status=%s conclusion=%s", run_id, status, conclusion)
        if status == "completed":
            return conclusion or "unknown", html_url
    return "timed_out", ""


# ── Target parsing helpers ────────────────────────────────────────────────────

URL_RE = re.compile(
    r"https?://[^\s,;\"'<>()\[\]]+"
    r"|(?<!\w)[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z]{2,})+(?:/[^\s,;\"'<>()\[\]]*)?",
    re.IGNORECASE,
)


def parse_targets(text: str) -> List[str]:
    """Extract unique URLs / domains from a block of text."""
    raw = re.split(r"[\n,;\s]+", text.strip())
    targets = []
    seen = set()
    for token in raw:
        token = token.strip().strip('"').strip("'")
        if not token:
            continue
        # Accept if it looks like a URL or domain
        if re.match(r"https?://", token, re.IGNORECASE) or "." in token:
            if token not in seen:
                seen.add(token)
                targets.append(token)
    return targets


def chunk(lst: List[str], size: int) -> List[List[str]]:
    return [lst[i : i + size] for i in range(0, len(lst), size)]


# ── Core scanning logic ───────────────────────────────────────────────────────

async def run_scan(update: Update, targets: List[str]) -> None:
    """Process all targets in batches and report results to Telegram."""
    total = len(targets)
    batches = chunk(targets, config.BATCH_SIZE)
    num_batches = len(batches)

    await update.message.reply_text(
        f"🚀 *Starting scan*\n"
        f"📋 Targets: `{total}`\n"
        f"📦 Batches: `{num_batches}` (max {config.BATCH_SIZE} each)",
        parse_mode="Markdown",
    )

    for idx, batch in enumerate(batches, start=1):
        label = f"{idx}/{num_batches}"
        progress_msg = await update.message.reply_text(
            f"⏳ *Batch {label}* — triggering GitHub Actions…",
            parse_mode="Markdown",
        )

        # record time just before trigger so we can find the run
        import datetime
        trigger_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

        success = trigger_workflow(batch, label)
        if not success:
            await progress_msg.edit_text(
                f"❌ *Batch {label}* — failed to trigger workflow. "
                f"Check your GitHub token / repo settings.",
                parse_mode="Markdown",
            )
            continue

        # Find the run ID (may take a few seconds to appear)
        await progress_msg.edit_text(
            f"🔍 *Batch {label}* — waiting for run to appear…",
            parse_mode="Markdown",
        )

        loop = asyncio.get_event_loop()
        run_id = await loop.run_in_executor(None, get_latest_run_id, trigger_ts)

        if run_id is None:
            await progress_msg.edit_text(
                f"⚠️ *Batch {label}* — could not find workflow run. "
                f"Check Actions tab manually.",
                parse_mode="Markdown",
            )
            continue

        await progress_msg.edit_text(
            f"⚙️ *Batch {label}* — scanning… (run `#{run_id}`)\n"
            f"_Polling every {config.POLL_INTERVAL}s, timeout {config.POLL_TIMEOUT//60}min_",
            parse_mode="Markdown",
        )

        conclusion, html_url = await loop.run_in_executor(
            None, wait_for_run, run_id
        )

        if conclusion == "success":
            emoji = "✅"
        elif conclusion == "timed_out":
            emoji = "⏰"
        else:
            emoji = "❌"

        await progress_msg.edit_text(
            f"{emoji} *Batch {label}* — `{conclusion}`\n"
            f"🔗 [View run on GitHub]({html_url})\n"
            f"_Results sent to Telegram by the workflow itself_",
            parse_mode="Markdown",
        )

    # Final summary
    await update.message.reply_text(
        f"🏁 *All {num_batches} batch(es) completed!*\n"
        f"Check above for per-batch results.",
        parse_mode="Markdown",
    )


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Open Redirect Scanner Bot*\n\n"
        "اعمل أي حاجة من دي:\n"
        "• ابعت رابط واحد أو أكتر (كل واحد في سطر أو مفصول بفاصلة)\n"
        "• ارفع ملف `.txt` فيه التارجتس\n"
        "• `/scan <url>` — لسكان سريع لتارجت واحد\n\n"
        "📦 الباتش الواحد = 50 تارجت، بيتبعت لـ GitHub Actions\n"
        "📩 النتايج بتجيلك هنا أوتوماتيك",
        parse_mode="Markdown",
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /scan <url>"""
    args = context.args
    if not args:
        await update.message.reply_text("❗ الاستخدام: `/scan <url>`", parse_mode="Markdown")
        return
    targets = parse_targets(" ".join(args))
    if not targets:
        await update.message.reply_text("❌ مش لاقي URLs صحيحة في الرسالة دي.")
        return
    await run_scan(update, targets)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages containing targets."""
    text = update.message.text or ""
    targets = parse_targets(text)
    if not targets:
        await update.message.reply_text(
            "❌ مش لاقي URLs أو دومينز في الرسالة.\n"
            "ابعت روابط مفصولة بسطر جديد أو فاصلة."
        )
        return
    await run_scan(update, targets)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle .txt file uploads containing targets."""
    doc: Document = update.message.document
    if not doc:
        return

    # Accept only text files
    if doc.mime_type not in ("text/plain", "application/octet-stream") and \
       not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("❌ ارفع ملف `.txt` فقط.", parse_mode="Markdown")
        return

    await update.message.reply_text("📥 جاري قراءة الملف…")

    file = await doc.get_file()
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    text = buf.getvalue().decode("utf-8", errors="ignore")

    targets = parse_targets(text)
    if not targets:
        await update.message.reply_text("❌ الملف مش فيه URLs صحيحة.")
        return

    await update.message.reply_text(f"📄 الملف فيه `{len(targets)}` تارجت.", parse_mode="Markdown")
    await run_scan(update, targets)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started — polling for messages…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
