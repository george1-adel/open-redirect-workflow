#!/usr/bin/env python3
"""
bot.py — Telegram bot for automated Open Redirect scanning via GitHub Actions.
"""

import asyncio
import io
import json
import logging
import re
import time
import datetime
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
from telegram.error import TelegramError

import config

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("open-redirect-bot")
# Suppress spam from httpx polling
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── GitHub Actions helpers ────────────────────────────────────────────────────

GH_HEADERS = {
    "Authorization": f"Bearer {config.GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _gh_url(path: str) -> str:
    return f"https://api.github.com/repos/{config.GITHUB_REPO}/{path}"


def trigger_workflow(targets: List[str], batch_label: str, chat_id: str) -> bool:
    """Trigger workflow_dispatch. Returns True on success."""
    targets_str = "\n".join(targets)
    payload = {
        "ref": config.GITHUB_BRANCH,
        "inputs": {
            "targets": targets_str,
            "batch_id": batch_label,
            "chat_id": chat_id,
        },
    }
    url = _gh_url(f"actions/workflows/{config.GITHUB_WORKFLOW_ID}/dispatches")
    try:
        resp = requests.post(url, json=payload, headers=GH_HEADERS, timeout=30)
        logger.info("workflow_dispatch response: %d %s", resp.status_code, resp.text[:200])
        return resp.status_code == 204
    except Exception as e:
        logger.error("trigger_workflow error: %s", e)
        return False


def get_latest_run_id(after_ts: float) -> Optional[int]:
    """Return the run ID of the most recent workflow_dispatch triggered after `after_ts`."""
    url = _gh_url(
        f"actions/workflows/{config.GITHUB_WORKFLOW_ID}/runs?per_page=10&event=workflow_dispatch"
    )
    for attempt in range(15):
        time.sleep(6)
        try:
            resp = requests.get(url, headers=GH_HEADERS, timeout=30)
            if resp.status_code != 200:
                logger.warning("get_latest_run_id status %d", resp.status_code)
                continue
            runs = resp.json().get("workflow_runs", [])
            logger.info("Checking %d runs (attempt %d)", len(runs), attempt + 1)
            for run in runs:
                created = run.get("created_at", "")
                try:
                    ts = datetime.datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    ts = 0
                if ts >= after_ts - 10:  # 10s tolerance
                    logger.info("Found run %d created_at=%s", run["id"], created)
                    return run["id"]
        except Exception as e:
            logger.error("get_latest_run_id error: %s", e)
    return None


def wait_for_run(run_id: int) -> Tuple[str, str]:
    """
    Poll until the run completes.
    Returns (conclusion, html_url).
    """
    url = _gh_url(f"actions/runs/{run_id}")
    deadline = time.time() + config.POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(config.POLL_INTERVAL)
        try:
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
        except Exception as e:
            logger.error("wait_for_run error: %s", e)
    return "timed_out", ""


# ── Target parsing ────────────────────────────────────────────────────────────

def parse_targets(text: str) -> List[str]:
    """Extract unique URLs / domains from a block of text."""
    # Split on whitespace, commas, semicolons
    raw = re.split(r"[\n\r,;\s]+", text.strip())
    targets = []
    seen = set()
    for token in raw:
        token = token.strip().strip('"').strip("'")
        if not token or len(token) < 4:
            continue
        # Accept if it starts with http(s):// or contains a dot (domain)
        if re.match(r"https?://", token, re.IGNORECASE) or (
            "." in token and not token.startswith(".")
        ):
            # Skip obviously non-URL tokens
            if " " in token:
                continue
            if token not in seen:
                seen.add(token)
                targets.append(token)
    return targets


def chunk(lst: List[str], size: int) -> List[List[str]]:
    return [lst[i : i + size] for i in range(0, len(lst), size)]


# ── Core scanning  ────────────────────────────────────────────────────────────

async def run_scan(update: Update, targets: List[str]) -> None:
    """Process all targets in batches and report results."""
    total = len(targets)
    batches = chunk(targets, config.BATCH_SIZE)
    num_batches = len(batches)

    await update.message.reply_text(
        f"🚀 *بدء السكان*\n"
        f"📋 التارجتس: `{total}`\n"
        f"📦 الباتشات: `{num_batches}` (كل باتش {config.BATCH_SIZE} على الأكتر)",
        parse_mode="Markdown",
    )

    for idx, batch in enumerate(batches, start=1):
        label = f"{idx}/{num_batches}"

        progress_msg = await update.message.reply_text(
            f"⏳ *باتش {label}* — بيبعت لـ GitHub Actions…",
            parse_mode="Markdown",
        )

        # timestamp before trigger
        trigger_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
        chat_id_str = str(update.effective_chat.id)

        success = await asyncio.get_event_loop().run_in_executor(
            None, trigger_workflow, batch, label, chat_id_str
        )
        if not success:
            await progress_msg.edit_text(
                f"❌ *باتش {label}* — فشل تشغيل الـ workflow!\n"
                f"تأكد من الـ GitHub token والـ repo name.",
                parse_mode="Markdown",
            )
            continue

        await progress_msg.edit_text(
            f"🔍 *باتش {label}* — بستنى ظهور الـ run…",
            parse_mode="Markdown",
        )

        run_id = await asyncio.get_event_loop().run_in_executor(
            None, get_latest_run_id, trigger_ts
        )

        if run_id is None:
            await progress_msg.edit_text(
                f"⚠️ *باتش {label}* — مش لاقي الـ run على GitHub Actions!\n"
                f"افتح Actions tab يدوياً.",
                parse_mode="Markdown",
            )
            continue

        await progress_msg.edit_text(
            f"⚙️ *باتش {label}* — قيد الفحص (run `#{run_id}`)\n"
            f"_الرجاء الانتظار، جاري فحص التارجتس بدقة…_",
            parse_mode="Markdown",
        )

        conclusion, html_url = await asyncio.get_event_loop().run_in_executor(
            None, wait_for_run, run_id
        )

        emoji = {
            "success": "✅",
            "failure": "❌",
            "timed_out": "⏰",
            "cancelled": "🚫",
        }.get(conclusion, "⚠️")

        await progress_msg.edit_text(
            f"{emoji} *باتش {label}* — `{conclusion}`\n"
            f"🔗 [شوف الـ run على GitHub]({html_url})\n"
            f"📩 _النتايج اتبعتت لتلجرام من الـ workflow مباشرة_",
            parse_mode="Markdown",
        )

    await update.message.reply_text(
        f"🏁 *خلصت كل الباتشات ({num_batches})!*\n"
        f"شوف النتايج فوق 👆",
        parse_mode="Markdown",
    )


# ── Handlers ──────────────────────────────────────────────────────────────────

def check_auth(update: Update) -> bool:
    """Check if the user/chat is allowed to use this bot."""
    if not update.effective_chat:
        return False
    return str(update.effective_chat.id) in config.ALLOWED_CHATS

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not check_auth(update):
        return
    await update.message.reply_text(
        "👋 *Open Redirect Scanner Bot*\n\n"
        "ابعتلي أي حاجة من دول:\n\n"
        "🔹 `/scan https://example.com` — تارجت واحد\n"
        "🔹 رسالة فيها روابط (كل رابط في سطر)\n"
        "🔹 ملف `.txt` فيه التارجتس\n\n"
        "📦 كل باتش = 50 تارجت → GitHub Actions\n"
        "📩 النتايج بتوصلك هنا أوتوماتيك ✅❌",
        parse_mode="Markdown",
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not check_auth(update):
        return
    if not context.args:
        await update.message.reply_text(
            "❗ الاستخدام: `/scan <url>`\nمثال: `/scan https://example.com`",
            parse_mode="Markdown",
        )
        return
    targets = parse_targets(" ".join(context.args))
    if not targets:
        await update.message.reply_text("❌ مش لاقي URL صحيح.")
        return
    await run_scan(update, targets)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not check_auth(update):
        return
    text = update.message.text or ""
    targets = parse_targets(text)
    if not targets:
        await update.message.reply_text(
            "❌ مش لاقي URLs أو دومينز.\n"
            "ابعت روابط كل واحد في سطر، أو ارفع ملف `.txt`"
        )
        return
    await run_scan(update, targets)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not check_auth(update):
        return
    doc: Document = update.message.document
    if not doc:
        return

    fname = (doc.file_name or "").lower()
    if not fname.endswith(".txt") and doc.mime_type not in ("text/plain", "application/octet-stream"):
        await update.message.reply_text("❌ ارفع ملف `.txt` بس.", parse_mode="Markdown")
        return

    msg = await update.message.reply_text("📥 بقرأ الملف…")

    file = await doc.get_file()
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    text = buf.getvalue().decode("utf-8", errors="ignore")

    targets = parse_targets(text)
    if not targets:
        await msg.edit_text("❌ الملف مش فيه URLs صحيحة.")
        return

    await msg.edit_text(f"📄 الملف فيه `{len(targets)}` تارجت. بديء…", parse_mode="Markdown")
    await run_scan(update, targets)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update caused error: %s", context.error, exc_info=context.error)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan",  cmd_scan))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    logger.info("Bot started — polling for messages…")
    # drop_pending_updates=False so we don't miss messages sent while bot was offline
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
