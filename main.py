# Discord Slash Bot (Pydroid-friendly)
# - /ask -> OpenAI rule helper (Embed output)
# - /stopmotion -> makes a stop-motion GIF from images in channel history
# - NEW: /archive_start -> every N minutes, grabs latest canvas screenshot from a source channel
#        and re-uploads it to a destination channel (keeps history)
#        /archive_stop, /archive_status
#
# Requirements:
#   pip install -U discord.py pillow
#
# Env vars you MUST set:
#   DISCORD_TOKEN   = your Discord bot token
#   OPENAI_API_KEY  = your OpenAI API key

import os
import time
import json
import asyncio
import urllib.request
from io import BytesIO
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

# -------------------- CONFIG --------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

MODEL = "gpt-4.1-mini"
MAX_OUTPUT_TOKENS = 450

COOLDOWN_S = 6
_last_used = {}

SHOW_DEBUG_PREVIEW = False

# ✅ SYSTEM PROMPT (UNCHANGED)
SYSTEM_PROMPT = r"""
You are a moderation helper for a Roblox r/place-clone game.
You must follow Roblox TOS and the rules below.

# In-game Rules (summary)
Permanent Ban:
- Leaking personally identifiable information ("doxxing")
- Deploying bot accounts
- Detailed/major NSFW drawings
- Harassment occurring for more than a month
- Repeated offences
- Extremism (e.g. supporting groups like Al-Qaeda, KKK, Nazism or encouraging violence)
- Ban evasion
- Pedophilia/Zoophilia

One-Month Ban:
- Slurs (canvas or chat)
- Swastikas and other offensive symbols
- Using a macro
- Sexual in-game talk
- Very inappropriate display usernames (slurs)
- Links or QR codes
- Heavy/realistic gore

One-Week Ban:
- Minor NSFW drawing
- Using more than 1 account at a time
- Inappropriate display usernames (swears)
- Large female genitalia depictions
- Large male genitalia depictions or explicit ones (e.g. shown going into a character's mouth)
- Breaking Roblox maturity rating (romantic themes, gambling, alcohol, drugs)
- Impersonating other players

Three-Day Ban:
- Male genitalia depictions (upside down "T")
- Female genitalia depictions
- Exploits (if not caught by anti-cheat)

One-Day Ban:
- Bypassing swear words

Warnings / Kicks / Under One-Day Ban:
- "W/Sing" or giving "backshots" (chronologically warning, kick, under 1 hour ban)
- Avatars impacting other player experiences (large avatars)

Other:
- Framing users: same duration as what they tried to frame for
- Lying on a ban appeal: double the original ban length
- Coordinated account usage by one person (saving pixels across multiple accounts then using them one by one):
  One-week ban per extra account used
- Abusing mechanics to gain unfair advantage: moderator decides duration
- Conspiring to break rules: half or full duration of what they would have done

What is NOT bannable:
- Chat (unless it bypasses chat filter)
- Griefing

remember you MAY look into the actual roblox rules and see if the thing being described is against the rules. you MAY tell the user that and see what they think about it.

# Task
Given the user's report, decide if it breaks the rules.

If you are NOT completely sure it is bannable, you MUST recommend contacting a moderator / opening a report ticket.
If you ARE sure, do NOT include that recommendation.

you MUST add onto the ban length if multiple bannable things are being mentioned. assume 1 month is 31 days.
an example of this would be: a user mentions that a penis and swastika is being drawn, you would say 34 days (3 for pp, 31 for swastika)

# settings
if a user puts any of these at the start of the prompt then it means the following and you MUST abide by them:

D: - Discord issue, see if it will warrant an in-game ban by how extreme it is. if you think it's bannable but don't know the length then just say the moderators need to define it

MA: - Mod abuse issue,the user can input a ban and see if it may be mod abuse or not

# Output format (STRICT)
Return ONLY valid JSON (no markdown, no extra text) in exactly this schema:

{
  "ban_title": "string",
  "ban_length": "string",
  "description": "string",
  "is_bannable": true/false,
  "unsure": true/false,
  "suggestion": "string",
  "rule": "string"
}
""".strip()

# -------------------- OPENAI --------------------
def _extract_text_from_responses_api(json_obj: dict) -> str:
    out = []
    for item in json_obj.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                out.append(c.get("text", ""))
    return "\n".join(out).strip()

def call_openai(system_prompt: str, user_prompt: str) -> str:
    if not OPENAI_API_KEY:
        return ""

    url = "https://api.openai.com/v1/responses"
    payload = {
        "model": MODEL,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
        return _extract_text_from_responses_api(data)
    except Exception:
        return ""

async def call_openai_async(system_prompt: str, user_prompt: str) -> str:
    return await asyncio.to_thread(call_openai, system_prompt, user_prompt)

# -------------------- DISCORD BOT --------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def cooldown_ok(uid: int) -> bool:
    now = time.time()
    if now - _last_used.get(uid, 0) < COOLDOWN_S:
        return False
    _last_used[uid] = now
    return True

def safe_parse(text: str) -> dict:
    try:
        obj = json.loads(text)
        return {
            "ban_title": obj.get("ban_title", "Unknown"),
            "ban_length": obj.get("ban_length", "Unknown"),
            "description": obj.get("description", ""),
            "is_bannable": bool(obj.get("is_bannable", False)),
            "unsure": bool(obj.get("unsure", False)),
            "suggestion": obj.get("suggestion", ""),
            "rule": obj.get("rule", ""),
        }
    except Exception:
        return {
            "ban_title": "Uncertain",
            "ban_length": "Unknown",
            "description": "Could not parse model output.",
            "is_bannable": False,
            "unsure": True,
            "suggestion": "Please contact a moderator.",
            "rule": "",
        }

def build_embed(res: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"{res['ban_title']} — {res['ban_length']}",
        description=res["description"] or "No description provided.",
    )

    rule = res["rule"] or "No exact rule provided."
    embed.add_field(name="Rule", value=rule[:900], inline=False)

    if res["unsure"] and res["suggestion"]:
        embed.set_footer(text=res["suggestion"][:2048])

    return embed

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")

# -------------------- COMMANDS --------------------
@bot.tree.command(
    name="ask",
    description="Check if something is bannable under the game rules (not official)."
)
@app_commands.describe(message="Describe what happened / what was drawn / what was said.")
async def ask(interaction: discord.Interaction, message: str):
    if not cooldown_ok(interaction.user.id):
        await interaction.response.send_message("⏳ Cooldown active.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    raw = await call_openai_async(SYSTEM_PROMPT, message)
    result = safe_parse(raw)
    embed = build_embed(result)
    await interaction.followup.send(embed=embed)

# -------------------- STOP-MOTION GIF COMMAND --------------------
async def _download_bytes(session, url: str, timeout_s: int = 30) -> bytes:
    async with session.get(url, timeout=timeout_s) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.read()

def _fit_resize(w: int, h: int, max_side: int) -> tuple[int, int]:
    if max(w, h) <= max_side:
        return w, h
    if w >= h:
        nw = max_side
        nh = max(1, int(h * (max_side / w)))
    else:
        nh = max_side
        nw = max(1, int(w * (max_side / h)))
    return nw, nh

@bot.tree.command(
    name="stopmotion",
    description="Make a stop-motion GIF from images posted in this channel in the last N hours."
)
@app_commands.describe(
    hours="How many hours back to look (default 24).",
    fps="Frames per second (default 4).",
    max_frames="Maximum number of images to include (default 60).",
    max_side="Max width/height for frames (default 512)."
)
async def stopmotion(
    interaction: discord.Interaction,
    hours: int = 24,
    fps: int = 4,
    max_frames: int = 60,
    max_side: int = 512
):
    from PIL import Image  # lazy import

    if hours < 1: hours = 1
    if hours > 168: hours = 168
    if fps < 1: fps = 1
    if fps > 15: fps = 15
    if max_frames < 1: max_frames = 1
    if max_frames > 250: max_frames = 250
    if max_side < 64: max_side = 64
    if max_side > 1024: max_side = 1024

    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("This command only works in text channels/threads.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    cutoff = discord.utils.utcnow() - timedelta(hours=hours)

    found: list[tuple[discord.Message, str]] = []
    try:
        async for msg in channel.history(limit=2000, after=cutoff, oldest_first=True):
            for a in msg.attachments:
                ct = (a.content_type or "")
                if ct.startswith("image/") and a.url:
                    found.append((msg, a.url))
            for e in msg.embeds:
                if e.image and e.image.url:
                    found.append((msg, e.image.url))
                if e.thumbnail and e.thumbnail.url:
                    found.append((msg, e.thumbnail.url))
    except discord.Forbidden:
        await interaction.followup.send("I don’t have permission to read message history in this channel.")
        return

    seen = set()
    ordered = []
    for msg, url in found:
        if url in seen:
            continue
        seen.add(url)
        ordered.append((msg, url))

    if not ordered:
        await interaction.followup.send(f"No images found in the last {hours} hour(s) in this channel.")
        return

    if len(ordered) > max_frames:
        ordered = ordered[-max_frames:]

    frames: list[Image.Image] = []
    import aiohttp
    async with aiohttp.ClientSession() as session:
        for _, url in ordered:
            try:
                b = await _download_bytes(session, url)
                im = Image.open(BytesIO(b)).convert("RGBA")
                nw, nh = _fit_resize(im.width, im.height, max_side)
                if (nw, nh) != (im.width, im.height):
                    im = im.resize((nw, nh), resample=Image.Resampling.LANCZOS)
                frames.append(im)
            except Exception:
                continue

    if len(frames) < 2:
        await interaction.followup.send("I couldn’t load enough valid images to make a GIF (need at least 2).")
        return

    max_w = max(im.width for im in frames)
    max_h = max(im.height for im in frames)

    normalized = []
    for im in frames:
        if im.width == max_w and im.height == max_h:
            normalized.append(im)
        else:
            canvas = Image.new("RGBA", (max_w, max_h), (0, 0, 0, 0))
            canvas.paste(im, ((max_w - im.width)//2, (max_h - im.height)//2))
            normalized.append(canvas)

    duration_ms = int(1000 / fps)
    out = BytesIO()

    pal_frames = [im.convert("P", palette=Image.Palette.ADAPTIVE, colors=256) for im in normalized]
    pal_frames[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=pal_frames[1:],
        duration=duration_ms,
        loop=0,
        optimize=True,
        disposal=2,
    )
    out.seek(0)

    file = discord.File(fp=out, filename="stopmotion.gif")
    await interaction.followup.send(
        content=f"GIF generated ({len(pal_frames)} frames, {fps} fps) from the last {hours} hour(s):",
        file=file
    )

# -------------------- BACKGROUND CANVAS ARCHIVER --------------------
# Runs ONLY after /archive_start. Low-RAM: downloads 1 image, uploads it, then releases memory.

_archive_task: asyncio.Task | None = None
_archive_settings: dict | None = None
_last_archived_message_id: int | None = None

async def _find_latest_image_message(channel: discord.TextChannel | discord.Thread) -> tuple[int, str] | None:
    """
    Returns (message_id, image_url) for the newest image-like message in the channel.
    Checks attachments first, then embed images.
    """
    async for msg in channel.history(limit=30, oldest_first=False):
        for a in msg.attachments:
            ct = (a.content_type or "")
            if ct.startswith("image/") and a.url:
                return msg.id, a.url
        for e in msg.embeds:
            if e.image and e.image.url:
                return msg.id, e.image.url
            if e.thumbnail and e.thumbnail.url:
                return msg.id, e.thumbnail.url
    return None

async def _archive_loop():
    global _archive_settings, _archive_task, _last_archived_message_id

    import aiohttp

    try:
        while _archive_settings is not None:
            interval = int(_archive_settings["interval_minutes"])
            source_id = int(_archive_settings["source_channel_id"])
            dest_id = int(_archive_settings["dest_channel_id"])

            source = bot.get_channel(source_id)
            dest = bot.get_channel(dest_id)

            if not isinstance(source, (discord.TextChannel, discord.Thread)) or not isinstance(dest, (discord.TextChannel, discord.Thread)):
                # If channels missing (deleted / bot lacks access), stop loop
                _archive_settings = None
                break

            try:
                latest = await _find_latest_image_message(source)
                if latest:
                    msg_id, url = latest
                    if _last_archived_message_id != msg_id:
                        _last_archived_message_id = msg_id

                        # download + re-upload (keeps history even if original is deleted)
                        async with aiohttp.ClientSession() as session:
                            data = await _download_bytes(session, url, timeout_s=30)

                        filename = f"canvas_{int(time.time())}.png"
                        f = discord.File(fp=BytesIO(data), filename=filename)

                        ts = int(time.time())
                        await dest.send(
                            content=f"🖼️ Canvas snapshot — <t:{ts}:f> (<t:{ts}:R>)",
                            file=f
                        )

                        # free memory ASAP
                        del data, f

            except Exception:
                # Ignore one-off failures; keep running
                pass

            # Sleep at the end so the command can stop quickly after the current cycle
            await asyncio.sleep(max(60, interval * 60))  # enforce at least 1 minute
    finally:
        _archive_task = None

def _archive_running() -> bool:
    return _archive_task is not None and not _archive_task.done() and _archive_settings is not None

@bot.tree.command(
    name="archive_start",
    description="Start archiving the latest image from a channel every N minutes into another channel."
)
@app_commands.describe(
    source_channel="Channel that receives the canvas updates (images).",
    destination_channel="Channel where snapshots should be posted.",
    interval_minutes="How often to archive (minutes). Example: 5"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def archive_start(
    interaction: discord.Interaction,
    source_channel: discord.TextChannel,
    destination_channel: discord.TextChannel,
    interval_minutes: int = 5
):
    global _archive_task, _archive_settings, _last_archived_message_id

    if interval_minutes < 1:
        interval_minutes = 1
    if interval_minutes > 120:
        interval_minutes = 120

    if _archive_running():
        await interaction.response.send_message(
            "Archive is already running. Use `/archive_stop` first.",
            ephemeral=True
        )
        return

    _last_archived_message_id = None
    _archive_settings = {
        "source_channel_id": source_channel.id,
        "dest_channel_id": destination_channel.id,
        "interval_minutes": interval_minutes,
    }

    _archive_task = asyncio.create_task(_archive_loop())
    await interaction.response.send_message(
        f"✅ Started archiving.\n"
        f"**Source:** {source_channel.mention}\n"
        f"**Destination:** {destination_channel.mention}\n"
        f"**Interval:** {interval_minutes} minute(s)",
        ephemeral=True
    )

@bot.tree.command(
    name="archive_stop",
    description="Stop the canvas snapshot archiver."
)
@app_commands.checks.has_permissions(manage_guild=True)
async def archive_stop(interaction: discord.Interaction):
    global _archive_settings

    if not _archive_running():
        await interaction.response.send_message("Archive is not running.", ephemeral=True)
        return

    _archive_settings = None
    await interaction.response.send_message("🛑 Stopping archive… (it will stop after the current cycle)", ephemeral=True)

@bot.tree.command(
    name="archive_status",
    description="Show current archiver status."
)
async def archive_status(interaction: discord.Interaction):
    if not _archive_running():
        await interaction.response.send_message("Archive is **OFF**.", ephemeral=True)
        return

    src = _archive_settings["source_channel_id"]
    dst = _archive_settings["dest_channel_id"]
    interval = _archive_settings["interval_minutes"]

    src_ch = bot.get_channel(int(src))
    dst_ch = bot.get_channel(int(dst))

    await interaction.response.send_message(
        "Archive is **ON** ✅\n"
        f"**Source:** {src_ch.mention if src_ch else src}\n"
        f"**Destination:** {dst_ch.mention if dst_ch else dst}\n"
        f"**Interval:** {interval} minute(s)",
        ephemeral=True
    )

@archive_start.error
@archive_stop.error
async def _archive_perm_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need **Manage Server** to use that command.", ephemeral=True)
    else:
        await interaction.response.send_message(f"Error: {error}", ephemeral=True)

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)