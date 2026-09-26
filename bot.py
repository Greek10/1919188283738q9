import os
import time
import asyncio
import logging
from io import BytesIO
from datetime import timedelta
import re
import math

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# ----------------- CONFIG --------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0") or "0")          # for /progress /live_progress /archieved + /canvas
TIMELAPSE_CHANNEL_ID = int(os.getenv("TIMELAPSE_CHANNEL_ID", "0") or "0")    # for /timelapse

# OWNER (for owner-only slash commands)
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or "0")

# Preset log channel (presets are stored as messages here)
PRESET_LOG_CHANNEL_ID = int(os.getenv("PRESET_LOG_CHANNEL_ID", "0") or "0")

# Render (or any host) gives you a port to bind to via $PORT. Default to 8080 for local runs.
PORT = int(os.getenv("PORT", "8080") or "8080")

COOLDOWN_SECONDS_PER_PIXEL = 15
POLL_SECONDS = 1800

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pixelbot")

# -------------------- SHARED HTTP SESSION --------------------
# Reused everywhere instead of opening a new aiohttp.ClientSession per request/poll.
# This avoids the overhead of a fresh TCP/TLS handshake every 30s across every
# background loop (live_progress, archieved, archieved_text, etc).
http_session: aiohttp.ClientSession | None = None

async def get_http_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    return http_session

# -------------------- DISCORD BOT --------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    log.info(f"✅ Logged in as {bot.user}")


# -------------------- KEEPALIVE / HEALTH-CHECK WEB SERVER --------------------
# Render's "Web Service" type requires binding to $PORT and answering HTTP
# requests, or it treats the deploy as failed even if the process itself is
# healthy. This tiny server exists purely to satisfy that port-detection
# check (and doubles as an uptime-pinger target / health check endpoint).
async def _health(request: web.Request) -> web.Response:
    status = "ready" if bot.is_ready() else "starting"
    return web.json_response({
        "status": status,
        "logged_in_as": str(bot.user) if bot.user else None,
        "guilds": len(bot.guilds) if bot.is_ready() else 0,
    })

async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"✅ Web server listening on port {PORT}")
    return runner

# -------------------- STARTUP / SHUTDOWN --------------------
async def _shutdown():
    log.info("Shutting down: cancelling background tasks...")
    all_tasks = list(_active_checks.values()) + list(_active_archives.values()) + list(_active_text_archivers.values())
    for t in all_tasks:
        if not t.done():
            t.cancel()
    if all_tasks:
        await asyncio.gather(*all_tasks, return_exceptions=True)

    global http_session
    if http_session is not None and not http_session.closed:
        await http_session.close()

async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")

    if BOT_OWNER_ID == 0:
        log.warning("BOT_OWNER_ID is not set. Owner-only commands (/check, /archieved, /archieved_text) will deny everyone.")

    if PRESET_LOG_CHANNEL_ID == 0:
        log.warning("PRESET_LOG_CHANNEL_ID is not set. /preset and preset-based /live_progress will fail until you set it.")

    if SOURCE_CHANNEL_ID == 0:
        log.warning("SOURCE_CHANNEL_ID is not set. /progress /live_progress /archieved /canvas will fail until you set it.")

    if TIMELAPSE_CHANNEL_ID == 0:
        log.warning("TIMELAPSE_CHANNEL_ID is not set. /timelapse will fail until you set it.")

    web_runner = await start_web_server()

    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    finally:
        await _shutdown()
        await web_runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
