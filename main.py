# Discord Slash Bot (Pydroid-friendly)
# - Hardcoded pre-prompt (characteristics) in code
# - /ask sends user message to ChatGPT with that system prompt
# - Bot formats response into a clean Discord Embed
# - NEW: /stopmotion -> grabs images from the past N hours in the current channel
#        and stitches them into a stop-motion GIF (oldest -> newest)
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
from datetime import timedelta, timezone

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
    # Lazy imports to reduce idle RAM (only loaded when command runs)
    from PIL import Image

    if hours < 1:
        hours = 1
    if hours > 168:  # 7 days cap
        hours = 168

    if fps < 1:
        fps = 1
    if fps > 15:
        fps = 15

    if max_frames < 1:
        max_frames = 1
    if max_frames > 250:
        max_frames = 250

    if max_side < 64:
        max_side = 64
    if max_side > 1024:
        max_side = 1024

    # Must be a text channel (or thread) that supports history
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("This command only works in text channels/threads.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    cutoff = discord.utils.utcnow() - timedelta(hours=hours)

    # Collect image URLs (attachments + embedded images)
    found: list[tuple[discord.Message, str]] = []
    try:
        async for msg in channel.history(limit=2000, after=cutoff, oldest_first=True):
            # Attachments first (most reliable)
            for a in msg.attachments:
                ct = (a.content_type or "")
                if ct.startswith("image/") and a.url:
                    found.append((msg, a.url))

            # Embeds with images (less reliable, but helpful)
            for e in msg.embeds:
                # image
                if e.image and e.image.url:
                    found.append((msg, e.image.url))
                # thumbnail
                if e.thumbnail and e.thumbnail.url:
                    found.append((msg, e.thumbnail.url))

    except discord.Forbidden:
        await interaction.followup.send("I don’t have permission to read message history in this channel.")
        return

    # De-duplicate by URL while preserving order
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

    # If too many, keep the most recent max_frames but still in chronological order
    if len(ordered) > max_frames:
        ordered = ordered[-max_frames:]

    # Download + decode images
    frames: list[Image.Image] = []
    # discord.py includes aiohttp; use it to avoid blocking
    import aiohttp

    async with aiohttp.ClientSession() as session:
        for msg, url in ordered:
            try:
                b = await _download_bytes(session, url)
                im = Image.open(BytesIO(b)).convert("RGBA")
                # resize down to save RAM/size
                nw, nh = _fit_resize(im.width, im.height, max_side)
                if (nw, nh) != (im.width, im.height):
                    im = im.resize((nw, nh), resample=Image.Resampling.LANCZOS)
                frames.append(im)
            except Exception:
                # Skip bad images instead of failing whole render
                continue

    if len(frames) < 2:
        await interaction.followup.send("I couldn’t load enough valid images to make a GIF (need at least 2).")
        return

    # Normalize all frames to the same canvas size (max frame width/height)
    max_w = max(im.width for im in frames)
    max_h = max(im.height for im in frames)

    normalized: list[Image.Image] = []
    for im in frames:
        if im.width == max_w and im.height == max_h:
            normalized.append(im)
            continue
        canvas = Image.new("RGBA", (max_w, max_h), (0, 0, 0, 0))
        x = (max_w - im.width) // 2
        y = (max_h - im.height) // 2
        canvas.paste(im, (x, y))
        normalized.append(canvas)

    # Encode GIF in memory
    duration_ms = int(1000 / fps)
    out = BytesIO()

    # Convert to palette mode for smaller GIFs
    pal_frames = []
    for im in normalized:
        pal_frames.append(im.convert("P", palette=Image.Palette.ADAPTIVE, colors=256))

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

    # Send result
    file = discord.File(fp=out, filename="stopmotion.gif")
    await interaction.followup.send(
        content=f"🎞️ Stop-motion GIF ({len(pal_frames)} frames, {fps} fps) from the last {hours} hour(s):",
        file=file
    )

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)