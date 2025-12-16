# Discord Slash Bot (Pydroid-friendly)
# - /ask -> OpenAI rule helper (Embed output)
# - /stopmotion -> makes a stop-motion GIF from images in channel history
# - /markarea -> template progresser:
#       * crops BOTH canvas + template using same coords (bottom-left origin)
#       * output preview shows TEMPLATE normally
#       * canvas never appears
#       * mismatched pixels get a RED "light" overlay
#       * matched pixels show the actual template color
#       * progress % = exact pixel matches / template non-transparent pixels
#
# COORDINATE SYSTEM:
#   User inputs use BOTTOM-LEFT as (0,0).
#   PIL images use TOP-LEFT as (0,0).
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
import re

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

def _make_template_progress_preview(canvas_crop, template_crop, red_alpha: int = 140):
    from PIL import Image

    w, h = template_crop.size
    out = template_crop.copy().convert("RGBA")

    cpx = canvas_crop.load()
    tpx = template_crop.load()
    opx = out.load()

    red_alpha = _clamp_int(red_alpha, 0, 255)

    for y in range(h):
        for x in range(w):
            tr, tg, tb, ta = tpx[x, y]
            if ta == 0:
                continue

            cr, cg, cb, ca = cpx[x, y]

            if (cr, cg, cb) == (tr, tg, tb):
                opx[x, y] = (tr, tg, tb, ta)
            else:
                a = (red_alpha * ta) // 255
                inv = 255 - a
                rr = (tr * inv + 255 * a) // 255
                rg = (tg * inv + 0 * a) // 255
                rb = (tb * inv + 0 * a) // 255
                opx[x, y] = (rr, rg, rb, ta)

    return out

def _exact_progress_percent(canvas_rgba, template_rgba) -> tuple[float, int, int]:
    if canvas_rgba.size != template_rgba.size:
        return 0.0, 0, 0

    cpx = canvas_rgba.load()
    tpx = template_rgba.load()
    w, h = template_rgba.size

    matched = 0
    total = 0

    for y in range(h):
        for x in range(w):
            tr, tg, tb, ta = tpx[x, y]
            if ta == 0:
                continue
            cr, cg, cb, ca = cpx[x, y]
            total += 1
            if (cr, cg, cb) == (tr, tg, tb):
                matched += 1

    pct = (matched / total * 100.0) if total else 0.0
    return pct, matched, total

def parse_coords_4pairs(coords: str):
    """
    Accepts formats like:
      (x1,y1)(x2,y2)(x3,y3)(x4,y4)
      (x1, y1) (x2, y2) (x3, y3) (x4, y4)
    Returns list of 4 tuples [(x,y),...]
    """
    if not coords:
        raise ValueError("Missing coords.")
    matches = re.findall(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)", coords)
    if len(matches) != 4:
        raise ValueError("Coords must be exactly 4 pairs like (x1,y1)(x2,y2)(x3,y3)(x4,y4).")
    pts = [(int(x), int(y)) for (x, y) in matches]
    return pts

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
    await interaction.followup.send(embed=build_embed(safe_parse(raw)))

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
    from PIL import Image

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

    found: list[str] = []
    try:
        async for msg in channel.history(limit=2000, after=cutoff, oldest_first=True):
            for a in msg.attachments:
                ct = (a.content_type or "")
                if ct.startswith("image/") and a.url:
                    found.append(a.url)
            for e in msg.embeds:
                if e.image and e.image.url:
                    found.append(e.image.url)
                if e.thumbnail and e.thumbnail.url:
                    found.append(e.thumbnail.url)
    except discord.Forbidden:
        await interaction.followup.send("I don’t have permission to read message history in this channel.")
        return

    seen = set()
    ordered = []
    for url in found:
        if url in seen:
            continue
        seen.add(url)
        ordered.append(url)

    if not ordered:
        await interaction.followup.send(f"No images found in the last {hours} hour(s) in this channel.")
        return

    if len(ordered) > max_frames:
        ordered = ordered[-max_frames:]

    frames: list[Image.Image] = []
    import aiohttp
    async with aiohttp.ClientSession() as session:
        for url in ordered:
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

# -------------------- MARKAREA --------------------
@bot.tree.command(
    name="template",
    description="Template progresser."
)
@app_commands.describe(
    source_channel="Channel where the canvas updates occurs.",
    template="Template image (attachment option).",
    coords="4 corners like (x1,y1)(x2,y2)(x3,y3)(x4,y4)"
)
async def markarea(
    interaction: discord.Interaction,
    source_channel: discord.TextChannel,
    template: discord.Attachment,
    coords: str
):
    from PIL import Image

    if not (template.content_type or "").startswith("image/"):
        await interaction.response.send_message("That template doesn’t look like an image.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    try:
        (x1, y1), (x2, y2), (x3, y3), (x4, y4) = parse_coords_4pairs(coords)
    except Exception as e:
        await interaction.followup.send(f"❌ Bad coords. Use: `(x1,y1)(x2,y2)(x3,y3)(x4,y4)`\nError: `{e}`")
        return

    try:
        try:
            canvas_url = await _find_latest_image_url(source_channel)
        except discord.Forbidden:
            await interaction.followup.send("I don’t have permission to read message history in that channel.")
            return

        if not canvas_url:
            await interaction.followup.send("I couldn’t find any recent images in that channel.")
            return

        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                canvas_bytes = await _download_bytes(session, canvas_url, timeout_s=30)
            template_bytes = await template.read()
        except Exception as e:
            await interaction.followup.send(f"Failed to download images: {e}")
            return

        try:
            canvas = Image.open(BytesIO(canvas_bytes)).convert("RGBA")
            tmpl = Image.open(BytesIO(template_bytes)).convert("RGBA")
        except Exception as e:
            await interaction.followup.send(f"Couldn’t open images: {e}")
            return

        CW, CH = canvas.size
        TW, TH = tmpl.size

        def to_canvas_img_pt(xu: int, yu: int) -> tuple[int, int]:
            xi = _clamp_int(xu, 0, CW - 1)
            yi = _clamp_int(_user_to_image_y(yu, CH), 0, CH - 1)
            return (xi, yi)

        p1 = to_canvas_img_pt(x1, y1)
        p2 = to_canvas_img_pt(x2, y2)
        p3 = to_canvas_img_pt(x3, y3)
        p4 = to_canvas_img_pt(x4, y4)

        xs = [p1[0], p2[0], p3[0], p4[0]]
        ys = [p1[1], p2[1], p3[1], p4[1]]

        left = max(0, min(xs))
        right = min(CW, max(xs) + 1)
        top = max(0, min(ys))
        bottom = min(CH, max(ys) + 1)

        box_w = right - left
        box_h = bottom - top

        if box_w < 2 or box_h < 2:
            await interaction.followup.send("Those coordinates create a region that’s too small. Try wider corners.")
            return

        canvas_crop = canvas.crop((left, top, right, bottom))

        if (TW, TH) == (box_w, box_h):
            tmpl_crop = tmpl
        else:
            def to_tmpl_img_pt(xu: int, yu: int) -> tuple[int, int]:
                xi = _clamp_int(xu, 0, TW - 1)
                yi = _clamp_int(_user_to_image_y(yu, TH), 0, TH - 1)
                return (xi, yi)

            tp1 = to_tmpl_img_pt(x1, y1)
            tp2 = to_tmpl_img_pt(x2, y2)
            tp3 = to_tmpl_img_pt(x3, y3)
            tp4 = to_tmpl_img_pt(x4, y4)

            txs = [tp1[0], tp2[0], tp3[0], tp4[0]]
            tys = [tp1[1], tp2[1], tp3[1], tp4[1]]

            t_left = max(0, min(txs))
            t_right = min(TW, max(txs) + 1)
            t_top = max(0, min(tys))
            t_bottom = min(TH, max(tys) + 1)

            if (t_right - t_left) != box_w or (t_bottom - t_top) != box_h:
                await interaction.followup.send(
                    "Your template image doesn’t cover that coordinate region.\n"
                    "Upload either:\n"
                    "- a full canvas-sized template (same coordinate system), OR\n"
                    "- a template that is exactly the region size."
                )
                return

            tmpl_crop = tmpl.crop((t_left, t_top, t_right, t_bottom))

        pct, matched, total = _exact_progress_percent(canvas_crop, tmpl_crop)
        preview = _make_template_progress_preview(canvas_crop, tmpl_crop, red_alpha=150)

        out_preview = BytesIO()
        preview.save(out_preview, format="PNG")
        out_preview.seek(0)

        await interaction.followup.send(
            content=(
                f" **Template Progress**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f" **Region**: `{box_w}×{box_h}`\n"
                f" **Pixels Completetion**: `{matched:,} / {total:,}`\n"
                f" **Percentage Completion**: **{pct:.2f}%**"
            ),
            file=discord.File(fp=out_preview, filename="template_progress.png")
        )

        del canvas_bytes, template_bytes, canvas, tmpl, canvas_crop, tmpl_crop, preview

    except Exception as e:
        await interaction.followup.send(f"❌ /markarea failed: `{type(e).__name__}: {e}`")

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)