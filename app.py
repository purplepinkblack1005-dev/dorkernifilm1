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
    """Start the search."""
    global progress_message_id, progress_chat_id, progress_task

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

    # Start search (if no dorks loaded, it will try dorks.txt)
    started = await manager.start_search()
    if not started:
        await update.message.reply_text(
            "❌ No dorks loaded. Use /adddork or upload a dorks.txt file first."
        )
        return

    # Send initial progress message
    msg = await update.message.reply_text(await _format_progress_message())
    progress_chat_id = update.effective_chat.id
    progress_message_id = msg.message_id

    # Pass the actual application instance to the updater
    app = context.application
    progress_task = asyncio.create_task(_progress_updater(app))


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop the currently running search."""
    global progress_task

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


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all available commands."""
    await update.message.reply_text(
        "📋 **Available Commands**\n\n"
        "**Search Control:**\n"
        "/start – Start a new search using loaded dorks\n"
        "/stop – Stop the currently running search\n"
        "/status – Show current search status\n\n"
        "**Dork Management:**\n"
        "/adddork `<dork>` – Add a single dork\n"
        "/listdorks – List all loaded dorks\n"
        "/cleardorks – Clear all loaded dorks\n"
        "📎 Upload dorks.txt – Load dorks from file\n\n"
        "**Proxy Management (owner only):**\n"
        "/addproxy `<host:port:user:pass>` – Add a proxy\n"
        "/listproxies – List loaded proxies\n"
        "/clearproxies – Clear all loaded proxies\n"
        "📎 Upload proxy.txt – Load proxies from file\n\n"
        "**Export (owner only):**\n"
        "/export – Download current sites.txt\n\n"
        "**Info:**\n"
        "/help – Show this help message",
        parse_mode="Markdown"
    )


@owner_only
async def adddork_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a single dork to the list."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: `/adddork <dork>`\n"
            "Example: `/adddork site:example.com inurl:admin`",
            parse_mode="Markdown"
        )
        return

    dork = " ".join(context.args).strip()
    if not dork:
        await update.message.reply_text("⚠️ Please provide a valid dork.")
        return

    count = manager.add_dorks([dork])
    await update.message.reply_text(
        f"✅ Added dork: `{dork}`\n"
        f"Total dorks: {count}",
        parse_mode="Markdown"
    )


@owner_only
async def addproxy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a single proxy to the list."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: `/addproxy <host:port:username:password>`\n"
            "Or: `/addproxy <host:port>`\n"
            "Example: `/addproxy 192.168.1.1:8080:user:pass`",
            parse_mode="Markdown"
        )
        return

    proxy_line = " ".join(context.args).strip()
    if not proxy_line:
        await update.message.reply_text("⚠️ Please provide a valid proxy.")
        return

    count = manager.add_proxies([proxy_line])
    await update.message.reply_text(
        f"✅ Added proxy.\n"
        f"Total proxies: {count}"
    )


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
            caption=(
                f"📄 Current results\n"
                f"⏱️ Runtime: {manager.get_runtime_str()}\n"
                f"Unique sites: {len(sites)}"
            )
        )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await manager.get_status()
    state = "RUNNING" if status["running"] else "IDLE"
    speed = status.get('speed', 0.0)
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
async def cleardorks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all loaded dorks."""
    if not manager.dorks:
        await update.message.reply_text("⚠️ No dorks to clear.")
        return

    count = len(manager.dorks)
    manager.clear_dorks()
    await update.message.reply_text(f"✅ Cleared {count} dorks.")


@owner_only
async def clearproxies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all loaded proxies."""
    if not manager.proxies:
        await update.message.reply_text("⚠️ No proxies to clear.")
        return

    count = len(manager.proxies)
    manager.clear_proxies()
    await update.message.reply_text(f"✅ Cleared {count} proxies.")


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
        count = manager.add_proxies(lines)
        await update.message.reply_text(f"✅ Added proxies. Total: {count}")
    else:
        # Treat as dorks
        count = manager.add_dorks(lines)
        await update.message.reply_text(f"✅ Added dorks. Total: {count}")


# -------------------------------
# Progress message updater
# -------------------------------
async def _format_progress_message() -> str:
    status = await manager.get_status()
    proxy_status = "ON" if status['proxy_enabled'] else "OFF"
    proxy_count = status.get('proxy_count', 0)
    state = "RUNNING" if status['running'] else "DONE"
    speed = status.get('speed', 0.0)

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
                            f"Unique sites: {len(sites)}"
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

    # Search control
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("status", status_cmd))

    # Dork management
    application.add_handler(CommandHandler("adddork", adddork_cmd))
    application.add_handler(CommandHandler("listdorks", listdorks_cmd))
    application.add_handler(CommandHandler("cleardorks", cleardorks_cmd))

    # Proxy management
    application.add_handler(CommandHandler("addproxy", addproxy_cmd))
    application.add_handler(CommandHandler("listproxies", listproxies_cmd))
    application.add_handler(CommandHandler("clearproxies", clearproxies_cmd))

    # Export
    application.add_handler(CommandHandler("export", export_cmd))

    # Help
    application.add_handler(CommandHandler("help", help_cmd))

    # File upload handler
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
