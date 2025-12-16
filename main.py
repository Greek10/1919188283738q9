# Discord Slash Bot (Pydroid-friendly)
# - /ask -> OpenAI rule helper (Embed output)
# - /stopmotion -> makes a stop-motion GIF from images in channel history
# - /markarea -> user provides a TEMPLATE image (slash attachment option),
#                bot grabs latest canvas update image from a chosen channel,
#                places template into the region defined by 4 coords (no cropping),
#                and compares COLORS vs the canvas region as a percentage.
#
# COORDINATE SYSTEM:
#   User inputs use BOTTOM-LEFT as (0,0).
#   Image processing uses TOP-LEFT as (0,0).
#   Conversion: y_img = (H - 1) - y_user
#
# Requirements:
#   pip install -U discord.py pillow
#
# Env vars:
#   DISCORD_TOKEN
#   OPENAI_API_KEY

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

# -------------------- COMMON HELPERS --------------------
async def _download_bytes(session, url: str, timeout_s: int = 30) -> bytes:
    async with session.get(url, timeout=timeout_s) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.read()

def _clamp_int(v: int, lo: int, hi: int) -> int:
    if v < lo: return lo
    if v > hi: return hi
    return v

def _user_to_image_y(y_user: int, img_h: int) -> int:
    # user coordinate system: bottom-left origin
    # image coordinate system: top-left origin
    return (img_h - 1) - y_user

async def _find_latest_image_url(channel: discord.TextChannel | discord.Thread) -> str | None:
    async for msg in channel.history(limit=50, oldest_first=False):
        for a in msg.attachments:
            ct = (a.content_type or "")
            if ct.startswith("image/") and a.url:
                return a.url
        for e in msg.embeds:
            if e.image and e.image.url:
                return e.image.url
            if e.thumbnail and e.thumbnail.url:
                return e.thumbnail.url
    return None

def _palette_color_set(img_rgba, sample_max_side: int = 512, colors: int = 256) -> set[tuple[int, int, int]]:
    """
    Returns a SET of RGB colors used by the image after adaptive palette quantization.
    Makes comparisons stable (avoids compression noise creating millions of near-colors).
    """
    from PIL import Image

    im = img_rgba
    w, h = im.size

    # downscale for speed if huge
    if max(w, h) > sample_max_side:
        if w >= h:
            nw = sample_max_side
            nh = max(1, int(h * (sample_max_side / w)))
        else:
            nh = sample_max_side
            nw = max(1, int(w * (sample_max_side / h)))
        im = im.resize((nw, nh), resample=Image.Resampling.BILINEAR)

    pal = im.convert("P", palette=Image.Palette.ADAPTIVE, colors=colors)
    used = pal.getcolors(maxcolors=colors * 4) or []
    used_indices = {idx for (_, idx) in used}

    palette = pal.getpalette() or []
    out = set()
    for idx in used_indices:
        base = idx * 3
        if base + 2 < len(palette):
            out.add((palette[base], palette[base + 1], palette[base + 2]))
    return out

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
    await interaction.followup.send(embed=build_embed(result))

# -------------------- STOP-MOTION GIF COMMAND --------------------
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

    await interaction.followup.send(
        content=f"GIF generated ({len(pal_frames)} frames, {fps} fps) from the last {hours} hour(s):",
        file=discord.File(fp=out, filename="stopmotion.gif")
    )

# -------------------- MARK AREA (BOTTOM-LEFT COORDS + TEMPLATE PLACE + COLOR COMPARE) --------------------
@bot.tree.command(
    name="markarea",
    description="Place a template image into a canvas region (bottom-left coords) and compare colors vs that region."
)
@app_commands.describe(
    source_channel="Channel with the latest canvas update image.",
    template="Template image to place (attachment option).",
    x1="Corner 1 X", y1="Corner 1 Y",
    x2="Corner 2 X", y2="Corner 2 Y",
    x3="Corner 3 X", y3="Corner 3 Y",
    x4="Corner 4 X", y4="Corner 4 Y",
)
async def markarea(
    interaction: discord.Interaction,
    source_channel: discord.TextChannel,
    template: discord.Attachment,
    x1: int, y1: int,
    x2: int, y2: int,
    x3: int, y3: int,
    x4: int, y4: int,
):
    from PIL import Image  # lazy import

    if not (template.content_type or "").startswith("image/"):
        await interaction.response.send_message("That template doesn’t look like an image.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    # Latest canvas image URL
    try:
        canvas_url = await _find_latest_image_url(source_channel)
    except discord.Forbidden:
        await interaction.followup.send("I don’t have permission to read message history in that channel.")
        return

    if not canvas_url:
        await interaction.followup.send("I couldn’t find any recent images in that channel.")
        return

    # Download both
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            canvas_bytes = await _download_bytes(session, canvas_url, timeout_s=30)
        template_bytes = await template.read()
    except Exception as e:
        await interaction.followup.send(f"Failed to download images: {e}")
        return

    # Open
    try:
        canvas = Image.open(BytesIO(canvas_bytes)).convert("RGBA")
    except Exception as e:
        await interaction.followup.send(f"Couldn’t open the canvas image: {e}")
        return

    try:
        user_img = Image.open(BytesIO(template_bytes)).convert("RGBA")
    except Exception as e:
        await interaction.followup.send(f"Couldn’t open your template image: {e}")
        return

    W, H = canvas.size

    # Convert user (bottom-left) Y to image (top-left) Y, then clamp
    def to_img_pt(xu: int, yu: int) -> tuple[int, int]:
        xi = _clamp_int(xu, 0, W - 1)
        yi = _clamp_int(_user_to_image_y(yu, H), 0, H - 1)
        return (xi, yi)

    p1 = to_img_pt(x1, y1)
    p2 = to_img_pt(x2, y2)
    p3 = to_img_pt(x3, y3)
    p4 = to_img_pt(x4, y4)

    xs = [p1[0], p2[0], p3[0], p4[0]]
    ys = [p1[1], p2[1], p3[1], p4[1]]

    left = max(0, min(xs))
    right = min(W, max(xs) + 1)
    top = max(0, min(ys))
    bottom = min(H, max(ys) + 1)

    box_w = right - left
    box_h = bottom - top

    if box_w < 2 or box_h < 2:
        await interaction.followup.send("Those coordinates create a region that’s too small. Try wider corners.")
        return

    # Canvas region (cropped only for analysis)
    canvas_region = canvas.crop((left, top, right, bottom))

    # Template is NOT cropped — resized to fit region (may stretch)
    user_fit = user_img.resize((box_w, box_h), resample=Image.Resampling.LANCZOS)

    # Overlay preview: paste template into canvas at region
    overlay = canvas.copy()
    overlay.paste(user_fit, (left, top), user_fit)

    # Color comparison (palette quantized)
    canvas_colors = _palette_color_set(canvas_region, sample_max_side=512, colors=256)
    user_colors = _palette_color_set(user_fit, sample_max_side=512, colors=256)

    if not user_colors:
        shared_count = 0
        shared_pct = 0.0
    else:
        shared_count = len(canvas_colors.intersection(user_colors))
        # As requested: 0% if canvas shares no colors with input image
        shared_pct = (shared_count / len(user_colors)) * 100.0

    # Send overlay image
    out = BytesIO()
    overlay.save(out, format="PNG")
    out.seek(0)

    # Convert box back to user (bottom-left) display for clarity
    # top_img -> y_user_top, bottom_img-1 -> y_user_bottom
    y_user_top = (H - 1) - top
    y_user_bottom = (H - 1) - (bottom - 1)

    await interaction.followup.send(
        content=(
            f"🧩 **Overlay preview** placed into the canvas region.\n"
            f"Canvas: **{W}×{H}**\n"
            f"Region (user coords, bottom-left): "
            f"**left={left}, bottom={y_user_bottom}, right={right-1}, top={y_user_top}** "
            f"(size **{box_w}×{box_h}**)\n\n"
            f"🎨 **Color comparison (quantized to 256 colors):**\n"
            f"- Canvas region colors: **{len(canvas_colors)}**\n"
            f"- Your template colors: **{len(user_colors)}**\n"
            f"- Shared colors: **{shared_count}**\n"
            f"- **Shared color %:** **{shared_pct:.2f}%**"
        ),
        file=discord.File(fp=out, filename="overlay_preview.png")
    )

    # free memory ASAP
    del canvas_bytes, template_bytes, canvas, user_img, canvas_region, user_fit, overlay

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
     raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)