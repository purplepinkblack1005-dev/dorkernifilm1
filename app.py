import asyncio
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from functools import wraps

from telegram import Update, Document
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import config
from searcher import SearchManager, now_str

logger = logging.getLogger(__name__)

manager = SearchManager()

progress_message_id: Optional[int] = None
progress_chat_id: Optional[int] = None
progress_task: Optional[asyncio.Task] = None
last_progress_text: Optional[str] = None


# -------------------------------
# Owner-only decorator
# -------------------------------
def owner_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != config.OWNER_ID:
            await update.message.reply_text("⛔ Access denied. Owner only.")
            return
        return await func(update, context)
    return wrapper


# -------------------------------
# Health server
# -------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Health server listening on port {port}")


# -------------------------------
# Helper: extract lines from a replied .txt document
# -------------------------------
async def _read_document_from_reply(update: Update):
    """
    If the message is a reply to a message that has a .txt document attached,
    download and return (filename, lines). Otherwise return (None, None).
    """
    reply = update.message.reply_to_message
    if not reply or not reply.document:
        return None, None

    doc: Document = reply.document
    filename = (doc.file_name or "").lower()
    if not filename.endswith(".txt"):
        return None, None

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    text = data.decode("utf-8", errors="ignore")
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    return doc.file_name, lines


# -------------------------------
# /start
# -------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global progress_message_id, progress_chat_id, progress_task, last_progress_text

    if manager.is_running():
        status = await manager.get_status()
        await update.message.reply_text(
            f"⚠️ Search already running.\n"
            f"⏱️ Runtime: {status['runtime_str']}\n"
            f"Processed: {status['processed']}/{status['total']}\n"
            f"Unique sites: {status['unique_count']}\n"
            f"Failed: {status['failed']}"
        )
        return

    if getattr(config, "DORKS_URL", ""):
        await update.message.reply_text("📥 Loading dorks...")

    started = await manager.start_search()
    if not started:
        await update.message.reply_text(
            "❌ No dorks loaded. Use `/adddorks <url>`, `/adddorks <dork>`, or reply to a .txt with `/adddorks`.",
            parse_mode="Markdown"
        )
        return

    text = await _format_progress_message()
    msg = await update.message.reply_text(text)
    progress_chat_id = update.effective_chat.id
    progress_message_id = msg.message_id
    last_progress_text = text
    progress_task = asyncio.create_task(_progress_updater(context.application))


# -------------------------------
# /stop
# -------------------------------
async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager.is_running():
        await update.message.reply_text("ℹ️ No search is currently running.")
        return
    await update.message.reply_text("🛑 Stopping search...")
    stopped = await manager.stop_search()
    if stopped:
        await update.message.reply_text(
            f"✅ Search stopped.\n"
            f"⏱️ Runtime: {manager.get_runtime_str()}\n"
            f"Processed: {manager.processed}/{manager.total}\n"
            f"Unique sites: {len(manager.unique_sites)}"
        )
    else:
        await update.message.reply_text("⚠️ Failed to stop search.")


# -------------------------------
# /help
# -------------------------------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 **Available Commands**\n\n"
        "**Search:**\n"
        "/start – Start a new search\n"
        "/stop – Stop the running search\n"
        "/status – Show search status\n\n"
        "**Dorks:**\n"
        "/adddorks `<dork>` – Add one literal dork\n"
        "/adddorks `<url>` – Fetch & merge dorks from a raw URL\n"
        "↩️ *Reply to any .txt file* with /adddorks – Load dorks from file\n"
        "/listdorks – List loaded dorks\n"
        "/cleardorks – Clear all dorks\n\n"
        "**Proxies (owner only):**\n"
        "/addproxy `<host:port:user:pass>` – Add one proxy\n"
        "/addproxy `<host:port>` – Add one proxy (no auth)\n"
        "↩️ *Reply to any .txt file* with /addproxy – Load proxies from file\n"
        "/listproxies – List proxies\n"
        "/clearproxies – Clear proxies\n\n"
        "**Export (owner only):**\n"
        "/export – Download sites.txt\n\n"
        "**Info:**\n"
        "/help – This message",
        parse_mode="Markdown"
    )


# -------------------------------
# /adddorks — literal dork, URL, OR reply-to-file
# -------------------------------
@owner_only
async def adddorks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Case 1 — reply to a .txt file
    filename, lines = await _read_document_from_reply(update)
    if filename:
        if not lines:
            await update.message.reply_text("⚠️ File is empty or contains only comments/blank lines.")
            return
        count = manager.add_dorks(lines)
        await update.message.reply_text(
            f"✅ Added {len(lines)} dorks from `{filename}`.\n"
            f"Total dorks: **{count}**",
            parse_mode="Markdown"
        )
        return

    # Case 2 — no args
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage:\n"
            "• `/adddorks site:example.com inurl:admin` — add one dork\n"
            "• `/adddorks https://example.com/dorks.txt` — fetch from URL\n"
            "• *Reply to any .txt file* with `/adddorks` — load from that file",
            parse_mode="Markdown"
        )
        return

    arg = " ".join(context.args).strip()
    if not arg:
        await update.message.reply_text("⚠️ Please provide a dork or URL.")
        return

    # URL mode
    if arg.startswith("http://") or arg.startswith("https://"):
        msg = await update.message.reply_text(
            f"📥 Fetching dorks from:\n`{arg}`\n\nThis may take up to 60s (cold start)...",
            parse_mode="Markdown"
        )
        added = await asyncio.to_thread(manager.load_dorks_from_remote, arg)
        if added == 0:
            await msg.edit_text(
                "❌ Failed to fetch dorks from that URL.\n"
                "Check the URL in your browser — it must return raw dork lines."
            )
            return
        await msg.edit_text(
            f"✅ Fetched & merged **{added}** new dorks.\n"
            f"Total dorks: **{len(manager.dorks)}**",
            parse_mode="Markdown"
        )
        return

    # Literal dork
    count = manager.add_dorks([arg])
    await update.message.reply_text(
        f"✅ Added dork: `{arg}`\n"
        f"Total dorks: {count}",
        parse_mode="Markdown"
    )


# -------------------------------
# /addproxy — literal proxy OR reply-to-file
# -------------------------------
@owner_only
async def addproxy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Case 1 — reply to a .txt file
    filename, lines = await _read_document_from_reply(update)
    if filename:
        if not lines:
            await update.message.reply_text("⚠️ File is empty or contains only comments/blank lines.")
            return
        count = manager.add_proxies(lines)
        await update.message.reply_text(
            f"✅ Added {len(lines)} proxy lines from `{filename}`.\n"
            f"Total proxies: **{count}**",
            parse_mode="Markdown"
        )
        return

    # Case 2 — no args
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage:\n"
            "• `/addproxy host:port:user:pass` — add one proxy\n"
            "• `/addproxy host:port` — add one proxy (no auth)\n"
            "• *Reply to any .txt file* with `/addproxy` — load from that file",
            parse_mode="Markdown"
        )
        return

    # Single proxy
    proxy_line = " ".join(context.args).strip()
    count = manager.add_proxies([proxy_line])
    await update.message.reply_text(f"✅ Added proxy.\nTotal proxies: {count}")


# -------------------------------
# /listproxies, /clearproxies
# -------------------------------
@owner_only
async def listproxies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager.proxies:
        await update.message.reply_text("⚠️ No proxies loaded.")
        return
    proxies = manager.proxies[:10]
    msg = "📄 Loaded proxies (showing first 10):\n" + "\n".join(f"• `{p}`" for p in proxies)
    if len(manager.proxies) > 10:
        msg += f"\n... and {len(manager.proxies)-10} more"
    await update.message.reply_text(msg, parse_mode="Markdown")


@owner_only
async def clearproxies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager.proxies:
        await update.message.reply_text("⚠️ No proxies to clear.")
        return
    count = len(manager.proxies)
    manager.clear_proxies()
    await update.message.reply_text(f"✅ Cleared {count} proxies.")


# -------------------------------
# /listdorks, /cleardorks
# -------------------------------
async def listdorks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager.dorks:
        await update.message.reply_text("⚠️ No dorks loaded.")
        return
    dorks = manager.dorks[:30]
    msg = "📄 Loaded dorks:\n" + "\n".join(f"• `{d}`" for d in dorks)
    if len(manager.dorks) > 30:
        msg += f"\n... and {len(manager.dorks)-30} more"
    await update.message.reply_text(msg, parse_mode="Markdown")


@owner_only
async def cleardorks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager.dorks:
        await update.message.reply_text("⚠️ No dorks to clear.")
        return
    count = len(manager.dorks)
    manager.clear_dorks()
    await update.message.reply_text(f"✅ Cleared {count} dorks.")


# -------------------------------
# /status, /export
# -------------------------------
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await manager.get_status()
    state = "RUNNING" if status["running"] else "IDLE"
    speed = status.get("speed", 0.0)
    await update.message.reply_text(
        f"🔎 DDGS Search Status\n"
        f"State: {state}\n"
        f"⏱️ Runtime: {status['runtime_str']}\n"
        f"⏳ ETA: {status['eta_str']}\n"
        f"⚡ Speed: {speed:.2f} dorks/s\n"
        f"Dorks: {status['processed']}/{status['total']}\n"
        f"Current: {status['current_dork'] or '-'}\n"
        f"Unique sites: {status['unique_count']}\n"
        f"Failed queries: {status['failed']}\n"
        f"Workers: {status['workers']}\n"
        f"Proxy: {'ON' if status['proxy_enabled'] else 'OFF'}\n"
        f"Proxies loaded: {status.get('proxy_count', 0)}\n"
        f"Proxy retries: {status.get('proxy_retries', 0)}\n"
        f"🕐 Time: {now_str()} PHT"
    )


@owner_only
async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sites = await manager.export_sites()
    if not sites:
        await update.message.reply_text("📄 No sites collected yet.")
        return
    await manager.write_sites_file()
    with open(config.SITES_FILE, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename="sites.txt",
            caption=(
                f"📄 Current results\n"
                f"⏱️ Runtime: {manager.get_runtime_str()}\n"
                f"Unique sites: {len(sites)}"
            )
        )


# -------------------------------
# Auto file upload (no reply) → dorks by default
# -------------------------------
@owner_only
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Any .txt uploaded without a reply command → treated as DORKS.
    To load PROXIES from a file, reply to it with /addproxy.
    To load DORKS from a file explicitly, reply with /adddorks.
    """
    doc: Document = update.message.document
    filename = (doc.file_name or "file.txt").lower()

    if not filename.endswith(".txt"):
        await update.message.reply_text("⚠️ Please upload a .txt file.")
        return

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    text = data.decode("utf-8", errors="ignore")
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]

    if not lines:
        await update.message.reply_text("⚠️ File is empty or contains only comments/blank lines.")
        return

    count = manager.add_dorks(lines)
    await update.message.reply_text(
        f"✅ Added {len(lines)} dorks from `{doc.file_name}`.\n"
        f"Total dorks: **{count}**\n\n"
        f"💡 To load as *proxies* instead, reply to the file with `/addproxy`.",
        parse_mode="Markdown"
    )


# -------------------------------
# Progress message
# -------------------------------
async def _format_progress_message() -> str:
    status = await manager.get_status()
    proxy_status = "ON" if status["proxy_enabled"] else "OFF"
    proxy_count = status.get("proxy_count", 0)
    proxy_retries = status.get("proxy_retries", 0)
    state = "RUNNING" if status["running"] else "DONE"
    speed = status.get("speed", 0.0)

    return (
        f"🔎 DDGS Search\n\n"
        f"Status: {state}\n"
        f"⏱️ Runtime: {status['runtime_str']}\n"
        f"⏳ ETA: {status['eta_str']}\n"
        f"⚡ Speed: {speed:.2f} dorks/s\n\n"
        f"Dorks: {status['processed']}/{status['total']}\n"
        f"Current: {status['current_dork'] or '-'}\n\n"
        f"Unique sites: {status['unique_count']}\n"
        f"Failed queries: {status['failed']}\n\n"
        f"Workers: {status['workers']}\n"
        f"Proxy: {proxy_status} ({proxy_count} loaded)\n"
        f"Proxy retries: {proxy_retries}\n\n"
        f"🕐 Last update: {now_str()} PHT"
    )


async def _progress_updater(app: Application):
    global progress_message_id, progress_chat_id, progress_task, last_progress_text
    bot = app.bot
    try:
        while manager.is_running():
            await asyncio.sleep(config.PROGRESS_UPDATE_INTERVAL)
            if progress_message_id and progress_chat_id:
                text = await _format_progress_message()
                if text == last_progress_text:
                    continue
                try:
                    await bot.edit_message_text(
                        chat_id=progress_chat_id,
                        message_id=progress_message_id,
                        text=text
                    )
                    last_progress_text = text
                except Exception as e:
                    logger.error(f"Failed to edit progress message: {e}")

        if progress_message_id and progress_chat_id:
            final_text = await _format_progress_message()
            try:
                await bot.edit_message_text(
                    chat_id=progress_chat_id,
                    message_id=progress_message_id,
                    text=final_text
                )
            except Exception as e:
                logger.error(f"Failed to edit final message: {e}")

            sites = await manager.export_sites()
            runtime_str = manager.get_runtime_str()
            if sites:
                await manager.write_sites_file()
                with open(config.SITES_FILE, "rb") as f:
                    await bot.send_document(
                        chat_id=progress_chat_id,
                        document=f,
                        filename="sites.txt",
                        caption=(
                            f"✅ DONE!\n"
                            f"⏱️ Runtime: {runtime_str}\n"
                            f"Unique sites: {len(sites)}\n"
                            f"🕐 Finished at: {now_str()} PHT"
                        )
                    )
            else:
                await bot.send_message(
                    chat_id=progress_chat_id,
                    text=f"✅ DONE! No sites found.\n⏱️ Runtime: {runtime_str}"
                )
    finally:
        progress_task = None
        progress_message_id = None
        progress_chat_id = None
        last_progress_text = None


# -------------------------------
# Error handler
# -------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = str(context.error)
    if "Conflict" in err and "getUpdates" in err:
        logger.warning("Telegram conflict (another instance polling) — ignoring")
        return
    logger.error(f"Update caused error: {context.error}")


# -------------------------------
# Main
# -------------------------------
def main():
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN environment variable not set!")
        return
    if not config.OWNER_ID:
        logger.error("❌ OWNER_ID environment variable not set or invalid!")
        return

    logger.info(f"✅ Config loaded. Owner ID: {config.OWNER_ID}")
    logger.info(f"🕐 Timezone: {os.getenv('TZ', 'Asia/Manila')}")
    logger.info(f"📁 Dorks file: {config.DORKS_FILE}")
    logger.info(f"📁 Proxy file: {config.PROXIES_FILE}")

    start_health_server()

    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("status", status_cmd))

    # Use "adddorks" (plural) as primary, keep "adddork" as an alias
    application.add_handler(CommandHandler("adddorks", adddorks_cmd))
    application.add_handler(CommandHandler("adddork", adddorks_cmd))
    application.add_handler(CommandHandler("listdorks", listdorks_cmd))
    application.add_handler(CommandHandler("cleardorks", cleardorks_cmd))

    application.add_handler(CommandHandler("addproxy", addproxy_cmd))
    application.add_handler(CommandHandler("listproxies", listproxies_cmd))
    application.add_handler(CommandHandler("clearproxies", clearproxies_cmd))

    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("help", help_cmd))

    # Upload without reply → dorks by default
    application.add_handler(MessageHandler(filters.Document.FileExtension("txt"), handle_document))

    application.add_error_handler(error_handler)

    logger.info("Bot started. Press Ctrl+C to stop.")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO
    )
    main()
