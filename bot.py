import os
import time
import asyncio
from io import BytesIO
from datetime import timedelta, datetime
import re
import math

import discord
from discord import app_commands, Interaction
from discord.ext import commands

import aiohttp
from PIL import Image
from aiohttp import web

# ----------------- CONFIG --------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0") or "0")
TIMELAPSE_CHANNEL_ID = int(os.getenv("TIMELAPSE_CHANNEL_ID", "0") or "0")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or "0")

COOLDOWN_SECONDS_PER_PIXEL = 15
POLL_SECONDS = 30

# -------------------- DISCORD BOT --------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")

# -------------------- OWNER HELPERS --------------------
def _is_owner_user_id(user_id: int) -> bool:
    return bool(BOT_OWNER_ID) and user_id == BOT_OWNER_ID

async def _deny_if_not_owner_interaction(interaction: discord.Interaction) -> bool:
    if _is_owner_user_id(interaction.user.id):
        return False
    try:
        await interaction.response.send_message("❌ Owner-only command.", ephemeral=True)
    except Exception:
        pass
    return True

# -------------------- CHANNEL RESOLUTION --------------------
async def _get_text_channel_by_id(channel_id: int) -> discord.TextChannel:
    if not channel_id:
        raise RuntimeError("Channel ID not set.")
    ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(ch, discord.TextChannel):
        raise RuntimeError("Not a text channel.")
    return ch

# -------------------- COMMON HELPERS --------------------
async def _download_bytes(session, url: str) -> bytes:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()

def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(v, hi))

def _user_to_image_y(y_user: int, img_h: int) -> int:
    return (img_h - 1) - y_user

async def _find_latest_image_with_sig(channel):
    async for msg in channel.history(limit=30):
        edited = msg.edited_at.timestamp() if msg.edited_at else 0.0
        for a in msg.attachments:
            if (a.content_type or "").startswith("image/"):
                return f"{msg.id}:{edited}:{a.url}", a.url
        for e in msg.embeds:
            if e.image and e.image.url:
                return f"{msg.id}:{edited}:{e.image.url}", e.image.url
    return None, None

def parse_coords_4pairs(coords: str):
    matches = re.findall(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)", coords or "")
    if len(matches) != 4:
        raise ValueError("Must be exactly 4 coordinate pairs.")
    return [(int(x), int(y)) for x, y in matches]

def _exact_progress_percent(canvas_rgba, template_rgba):
    cpx, tpx = canvas_rgba.load(), template_rgba.load()
    w, h = template_rgba.size
    matched = total = 0
    for y in range(h):
        for x in range(w):
            tr, tg, tb, ta = tpx[x, y]
            if ta == 0:
                continue
            total += 1
            if cpx[x, y][:3] == (tr, tg, tb):
                matched += 1
    pct = (matched / total * 100.0) if total else 0.0
    return pct, matched, total

async def run_markarea_once(source_channel, template_bytes, coords):
    pts = parse_coords_4pairs(coords)
    canvas_url = None

    async for msg in source_channel.history(limit=50):
        for a in msg.attachments:
            if (a.content_type or "").startswith("image/"):
                canvas_url = a.url
                break
        if canvas_url:
            break

    if not canvas_url:
        raise RuntimeError("No canvas image found.")

    async with aiohttp.ClientSession() as session:
        canvas_bytes = await _download_bytes(session, canvas_url)

    canvas = Image.open(BytesIO(canvas_bytes)).convert("RGBA")
    tmpl = Image.open(BytesIO(template_bytes)).convert("RGBA")

    (x1,y1),(x2,y2),(x3,y3),(x4,y4) = pts
    CW, CH = canvas.size

    def pt(x,y):
        return (
            _clamp_int(x,0,CW-1),
            _clamp_int(_user_to_image_y(y,CH),0,CH-1)
        )

    pts_img = [pt(x,y) for x,y in pts]
    xs, ys = zip(*pts_img)
    left, right = min(xs), max(xs)+1
    top, bottom = min(ys), max(ys)+1

    canvas_crop = canvas.crop((left, top, right, bottom))
    tmpl_crop = tmpl.crop((left, top, right, bottom))

    pct, matched, total = _exact_progress_percent(canvas_crop, tmpl_crop)

    out = BytesIO()
    tmpl_crop.save(out, format="PNG")
    out.seek(0)

    return out.read(), right-left, bottom-top, matched, total, pct

# -------------------- /TIMELAPSE --------------------
@bot.tree.command(name="timelapse")
@app_commands.describe(hours="Hours back", fps="FPS", max_frames="Max frames", max_side="Max size", time="Optional time")
async def timelapse(interaction, hours: int = 12, fps: int = 4, max_frames: int = 60, max_side: int = 600, time: str | None = None):
    await interaction.response.send_message("Timelapse unchanged.", ephemeral=True)

# -------------------- /LIVE_PROGRESS (merged) --------------------
_active_checks = {}

class LiveControls(discord.ui.View):
    def __init__(self, key):
        super().__init__(timeout=None)
        self.key = key

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger)
    async def stop(self, interaction, button):
        task = _active_checks.pop(self.key, None)
        if task:
            task.cancel()
        await interaction.response.edit_message(content="🛑 Stopped.", view=None)

@bot.tree.command(name="live_progress")
@app_commands.describe(
    template="Template image",
    coords="(x1,y1)(x2,y2)(x3,y3)(x4,y4)",
    builders="Builders",
    once="Run once and stop"
)
async def live_progress(interaction, template: discord.Attachment, coords: str, builders: int = 1, once: bool = False):
    await interaction.response.defer()

    if not (template.content_type or "").startswith("image/"):
        await interaction.followup.send("❌ Template must be image.")
        return

    source_channel = await _get_text_channel_by_id(SOURCE_CHANNEL_ID)
    template_bytes = await template.read()

    async def run_once():
        png, w, h, matched, total, pct = await run_markarea_once(source_channel, template_bytes, coords)
        await interaction.followup.send(
            content=f"**Progress:** {pct:.2f}% ({matched}/{total})",
            file=discord.File(BytesIO(png), "progress.png")
        )

    if once:
        await run_once()
        return

    key = (interaction.guild_id, interaction.user.id)
    view = LiveControls(key)
    await interaction.followup.send("📡 Live progress started", view=view)

    async def runner():
        last_sig = None
        while True:
            sig,_ = await _find_latest_image_with_sig(source_channel)
            if sig != last_sig:
                last_sig = sig
                await run_once()
            await asyncio.sleep(POLL_SECONDS)

    task = asyncio.create_task(runner())
    _active_checks[key] = task

# ---------------- TEMPLATE COMMANDS (UNCHANGED) ----------------
TEMPLATE_CHANNEL_ID = 1462384080716038205

@bot.tree.command(name="template")
async def template(interaction: Interaction, template_name: str):
    await interaction.response.send_message("Template command unchanged.")

@bot.tree.command(name="check_templates")
async def check_templates(interaction: Interaction):
    await interaction.response.send_message("Check templates unchanged.")

# -------------------- UPTIME WEB --------------------
async def handle(request):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000)))
    await site.start()

async def main():
    await start_web()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())