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
from searcher import SearchManager

logger = logging.getLogger(__name__)

# Global search manager
manager = SearchManager()

# Global progress message info (single active search)
progress_message_id: Optional[int] = None
progress_chat_id: Optional[int] = None
progress_task: Optional[asyncio.Task] = None


# -------------------------------
# Owner-only decorator
# -------------------------------
def owner_only(func):
    """Decorator to restrict command to owner only."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != config.OWNER_ID:
            await update.message.reply_text("⛔ Access denied. Owner only.")
            return
        return await func(update, context)
    return wrapper


# -------------------------------
# Minimal HTTP health-check server for Railway/UptimeRobot
# -------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # Suppress default logging


def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health server listening on port {port}")


# -------------------------------
# Telegram command handlers
# -------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 DDGS Dork Search Bot\n\n"
        "Commands:\n"
        "/search – start a new search using loaded dorks\n"
        "/export – get current sites.txt (owner only)\n"
        "/status – show search status\n"
        "/listdorks – list loaded dorks\n"
        "/listproxies – list loaded proxies (owner only)\n\n"
        "Upload:\n"
        "- dorks.txt to load dorks\n"
        "- proxy.txt to load proxies"
    )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global progress_message_id, progress_chat_id, progress_task

    if manager.is_running():
        status = await manager.get_status()
        await update.message.reply_text(
            f"⚠️ Search already running.\n"
            f"Processed: {status['processed']}/{status['total']}\n"
            f"Unique sites: {status['unique_count']}\n"
            f"Failed: {status['failed']}"
        )
        return

    # Start search (if no dorks loaded, it will try dorks.txt)
    started = await manager.start_search()
    if not started:
        await update.message.reply_text(
            "❌ No dorks loaded. Upload a dorks.txt file or ensure dorks.txt exists."
        )
        return

    # Send initial progress message
    msg = await update.message.reply_text(await _format_progress_message())
    progress_chat_id = update.effective_chat.id
    progress_message_id = msg.message_id

    # Pass the actual application instance to the updater
    app = context.application
    progress_task = asyncio.create_task(_progress_updater(app))


@owner_only
async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export current sites (owner only)."""
    sites = await manager.export_sites()
    if not sites:
        await update.message.reply_text("📄 No sites collected yet.")
        return

    # Write current sites.txt and send
    await manager.write_sites_file()
    with open(config.SITES_FILE, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename="sites.txt",
            caption=f"📄 Current results\nUnique sites: {len(sites)}"
        )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await manager.get_status()
    state = "RUNNING" if status["running"] else "IDLE"
    await update.message.reply_text(
        f"🔎 DDGS Search Status\n"
        f"State: {state}\n"
        f"Dorks: {status['processed']}/{status['total']}\n"
        f"Current: {status['current_dork'] or '-'}\n"
        f"Unique sites: {status['unique_count']}\n"
        f"Failed queries: {status['failed']}\n"
        f"Workers: {status['workers']}\n"
        f"Proxy: {'ON' if status['proxy_enabled'] else 'OFF'}\n"
        f"Proxies loaded: {status.get('proxy_count', 0)}"
    )


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
async def listproxies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List loaded proxies (owner only)."""
    if not manager.proxies:
        await update.message.reply_text("⚠️ No proxies loaded.")
        return
    proxies = manager.proxies[:10]
    msg = "📄 Loaded proxies (showing first 10):\n" + "\n".join(f"• `{p}`" for p in proxies)
    if len(manager.proxies) > 10:
        msg += f"\n... and {len(manager.proxies)-10} more"
    await update.message.reply_text(msg, parse_mode="Markdown")


@owner_only
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle uploaded .txt file as either dorks or proxies (owner only)."""
    doc: Document = update.message.document
    filename = doc.file_name.lower()

    if not filename.endswith(".txt"):
        await update.message.reply_text("⚠️ Please upload a .txt file.")
        return

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    text = data.decode("utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]

    if not lines:
        await update.message.reply_text("⚠️ File is empty or contains only comments/blank lines.")
        return

    # Check if it's a proxy file
    if "proxy" in filename:
        count = manager.set_proxies(lines)
        await update.message.reply_text(f"✅ Loaded {count} proxies from {doc.file_name}.")
    else:
        # Treat as dorks
        count = manager.set_dorks(lines)
        await update.message.reply_text(f"✅ Loaded {count} dorks from {doc.file_name}.")


# -------------------------------
# Progress message updater
# -------------------------------
async def _format_progress_message() -> str:
    status = await manager.get_status()
    proxy_status = "ON" if status['proxy_enabled'] else "OFF"
    proxy_count = status.get('proxy_count', 0)
    return (
        f"🔎 DDGS Search\n\n"
        f"Status: {'RUNNING' if status['running'] else 'DONE'}\n\n"
        f"Dorks: {status['processed']}/{status['total']}\n"
        f"Current: {status['current_dork'] or '-'}\n\n"
        f"Unique sites: {status['unique_count']}\n"
        f"Failed queries: {status['failed']}\n\n"
        f"Workers: {status['workers']}\n"
        f"Proxy: {proxy_status} ({proxy_count} loaded)\n\n"
        f"Last update: {time.strftime('%H:%M:%S', time.localtime(status['last_update']))}"
    )


async def _progress_updater(app: Application):
    """Periodically edit the progress message while search is running."""
    global progress_message_id, progress_chat_id, progress_task
    bot = app.bot
    try:
        while manager.is_running():
            await asyncio.sleep(config.PROGRESS_UPDATE_INTERVAL)
            if progress_message_id and progress_chat_id:
                try:
                    await bot.edit_message_text(
                        chat_id=progress_chat_id,
                        message_id=progress_message_id,
                        text=await _format_progress_message()
                    )
                except Exception as e:
                    logger.error(f"Failed to edit progress message: {e}")
        # Search finished – send final update and file
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

            # Send final sites.txt (only to owner)
            sites = await manager.export_sites()
            if sites:
                await manager.write_sites_file()
                with open(config.SITES_FILE, "rb") as f:
                    await bot.send_document(
                        chat_id=progress_chat_id,
                        document=f,
                        filename="sites.txt",
                        caption=f"✅ DONE!\nSearch completed successfully.\n\nUnique sites: {len(sites)}"
                    )
            else:
                await bot.send_message(
                    chat_id=progress_chat_id,
                    text="✅ DONE! No sites found."
                )
    finally:
        progress_task = None
        progress_message_id = None
        progress_chat_id = None


# -------------------------------
# Error handler
# -------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Update caused error: {context.error}")


# -------------------------------
# Main
# -------------------------------
def main():
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set!")
        return

    # Start HTTP health server for Railway/UptimeRobot
    start_health_server()

    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("search", search_cmd))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("listdorks", listdorks_cmd))
    application.add_handler(CommandHandler("listproxies", listproxies_cmd))
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
