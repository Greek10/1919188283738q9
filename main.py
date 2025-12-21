import os
import time
import json
import asyncio
import urllib.request
from io import BytesIO
from datetime import timedelta
import re
import math
from typing import Optional

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

# ETA rule
COOLDOWN_SECONDS_PER_PIXEL = 15

# -------------------- MODERATION SYSTEM PROMPT --------------------
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
- Coordinated account usage by one person: One-week ban per extra account used
- Abusing mechanics to gain unfair advantage: moderator decides duration
- Conspiring to break rules: half or full duration of what they would have done

What is NOT bannable:
- Chat (unless it bypasses chat filter)
- Griefing

# Task
Given the user's report, decide if it breaks the rules.

If you are NOT completely sure it is bannable, recommend contacting a moderator.
If you ARE sure, do NOT include that recommendation.

You MUST add onto the ban length if multiple bannable things are mentioned (assume 31-day months).

# settings
D: - Discord issue -> judge if it warrants in-game ban; if unsure on length, say moderators must define it
MA: - Mod abuse checker

# Output format (STRICT JSON)
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

async def _get_latest_canvas_marker(channel: discord.TextChannel | discord.Thread):
    async for msg in channel.history(limit=50, oldest_first=False):
        image_url = None
        for a in msg.attachments:
            ct = (a.content_type or "")
            if ct.startswith("image/") and a.url:
                image_url = a.url
                break
        if not image_url:
            for e in msg.embeds:
                if e.image and e.image.url:
                    image_url = e.image.url
                    break
                if e.thumbnail and e.thumbnail.url:
                    image_url = e.thumbnail.url
                    break
        if image_url:
            edited_ts = msg.edited_at.timestamp() if msg.edited_at else None
            return msg.id, edited_ts, image_url
    return None, None, None

async def _find_latest_image_url(channel: discord.TextChannel | discord.Thread) -> str | None:
    mid, ets, url = await _get_latest_canvas_marker(channel)
    return url

def parse_coords_4pairs(coords: str):
    matches = re.findall(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)", coords or "")
    if len(matches) != 4:
        raise ValueError("Coords must be exactly 4 pairs like (x1,y1)(x2,y2)(x3,y3)(x4,y4).")
    return [(int(x), int(y)) for x, y in matches]

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
            if ta == 0:  # ignore transparent template pixels
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

def _exact_progress_percent(canvas_rgba, template_rgba):
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

def _seconds_to_hms(total_seconds: int):
    total_seconds = max(0, int(total_seconds))
    h = total_seconds // 3600
    total_seconds -= h * 3600
    m = total_seconds // 60
    s = total_seconds - m * 60
    return h, m, s

def _eta_from_progress(matched: int, total: int, builders: int):
    builders = max(1, int(builders))
    total = max(0, int(total))
    matched = max(0, min(int(matched), total))
    remaining = total - matched
    ticks = math.ceil(remaining / builders) if remaining > 0 else 0
    eta_seconds = ticks * COOLDOWN_SECONDS_PER_PIXEL
    h, m, s = _seconds_to_hms(eta_seconds)
    return remaining, eta_seconds, h, m, s

def _fmt_pct(p: float) -> str:
    return f"{p:.1f}%"

def _fmt_int(n: int) -> str:
    return f"{n:,}"

def _progress_embed(
    *,
    title: str,
    box_w: int,
    box_h: int,
    matched: int,
    total: int,
    pct: float,
    delta_matched: int | None = None,
    delta_pct: float | None = None,
    eta_hms: tuple[int, int, int] | None = None,
    remaining_px: int | None = None,
    builders: int | None = None,
    image_filename: str = "template_progress.png",
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"**{_fmt_int(matched)}/{_fmt_int(total)}** pixels completed (**{_fmt_pct(pct)}**)",
    )
    if delta_matched is not None:
        sign = "+" if delta_matched > 0 else ""
        value = f"`{sign}{_fmt_int(delta_matched)}` pixels"
        if delta_pct is not None:
            ps = "+" if delta_pct > 0 else ""
            value += f" (`{ps}{delta_pct:.2f}%`)"
        embed.add_field(name="Recent Progress", value=value, inline=False)
    embed.add_field(name="Region", value=f"`{box_w}×{box_h}`", inline=True)
    if eta_hms is not None and remaining_px is not None and builders is not None:
        h, m, s = eta_hms
        embed.add_field(
            name="ETA",
            value=f"`{h}h {m}m {s}s`  ({_fmt_int(remaining_px)} px, builders={builders})",
            inline=True
        )
    embed.set_footer(text=f"{COOLDOWN_SECONDS_PER_PIXEL}s per pixel charge (per builder)")
    embed.set_image(url=f"attachment://{image_filename}")
    return embed

# -------- core: run one comparison (now accepts optional canvas_bytes) --------
async def run_markarea_once(
    *,
    source_channel: Optional[discord.TextChannel] = None,
    canvas_bytes: Optional[bytes] = None,
    template_bytes: bytes,
    coords: str,
):
    """
    Returns (png_bytes, box_w, box_h, matched, total, pct)
    If canvas_bytes is None, pulls latest image from source_channel.
    """
    from PIL import Image
    import aiohttp

    pts = parse_coords_4pairs(coords)
    (x1, y1), (x2, y2), (x3, y3), (x4, y4) = pts

    # Get canvas bytes
    if canvas_bytes is None:
        if source_channel is None:
            raise RuntimeError("Provide a source_channel or upload a canvas image.")
        canvas_url = await _find_latest_image_url(source_channel)
        if not canvas_url:
            raise RuntimeError("No recent canvas image found in the source channel.")
        async with aiohttp.ClientSession() as session:
            canvas_bytes = await _download_bytes(session, canvas_url, timeout_s=30)

    canvas = Image.open(BytesIO(canvas_bytes)).convert("RGBA")
    tmpl = Image.open(BytesIO(template_bytes)).convert("RGBA")

    CW, CH = canvas.size
    TW, TH = tmpl.size

    def to_img_pt(xu: int, yu: int, H: int) -> tuple[int, int]:
        xi = _clamp_int(xu, 0, 10**9)  # clamp later per-image
        yi = _clamp_int((H - 1) - yu, -10**9, 10**9)
        return xi, yi

    # Convert corners for CANVAS
    cx1, cy1 = to_img_pt(x1, y1, CH)
    cx2, cy2 = to_img_pt(x2, y2, CH)
    cx3, cy3 = to_img_pt(x3, y3, CH)
    cx4, cy4 = to_img_pt(x4, y4, CH)
    cxs = [max(0, min(CW - 1, v)) for v in (cx1, cx2, cx3, cx4)]
    cys = [max(0, min(CH - 1, v)) for v in (cy1, cy2, cy3, cy4)]
    left, right = max(0, min(cxs)), min(CW, max(cxs) + 1)
    top, bottom = max(0, min(cys)), min(CH, max(cys) + 1)
    box_w, box_h = right - left, bottom - top
    if box_w < 2 or box_h < 2:
        raise RuntimeError("Those coordinates create a region that’s too small.")
    canvas_crop = canvas.crop((left, top, right, bottom))

    # TEMPLATE crop
    if (TW, TH) == (box_w, box_h):
        tmpl_crop = tmpl
    else:
        tx1, ty1 = to_img_pt(x1, y1, TH)
        tx2, ty2 = to_img_pt(x2, y2, TH)
        tx3, ty3 = to_img_pt(x3, y3, TH)
        tx4, ty4 = to_img_pt(x4, y4, TH)
        txs = [max(0, min(TW - 1, v)) for v in (tx1, tx2, tx3, tx4)]
        tys = [max(0, min(TH - 1, v)) for v in (ty1, ty2, ty3, ty4)]
        t_left, t_right = max(0, min(txs)), min(TW, max(txs) + 1)
        t_top, t_bottom = max(0, min(tys)), min(TH, max(tys) + 1)
        if (t_right - t_left) != box_w or (t_bottom - t_top) != box_h:
            raise RuntimeError(
                "Template doesn’t cover that region. Upload a full-canvas template, "
                "or a template exactly sized to the region."
            )
        tmpl_crop = tmpl.crop((t_left, t_top, t_right, t_bottom))

    pct, matched, total = _exact_progress_percent(canvas_crop, tmpl_crop)
    preview = _make_template_progress_preview(canvas_crop, tmpl_crop, red_alpha=150)

    out = BytesIO()
    preview.save(out, format="PNG")
    out.seek(0)
    return out.read(), box_w, box_h, matched, total, pct

# -------------------- /ASK --------------------
@bot.tree.command(name="ask", description="Check if something is bannable under the game rules.")
@app_commands.describe(message="Describe what happened / what was drawn / what was said.")
async def ask(interaction: discord.Interaction, message: str):
    if not cooldown_ok(interaction.user.id):
        await interaction.response.send_message("⏳ Cooldown active.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    raw = await call_openai_async(SYSTEM_PROMPT, message)
    await interaction.followup.send(embed=build_embed(safe_parse(raw)))

# -------------------- /STOPMOTION --------------------
@bot.tree.command(name="timelapse", description="Make a timelapse GIF")
@app_commands.describe(hours="Hours back (default 24).", fps="FPS (default 4).", max_frames="Max frames (default 60).", max_side="Max side (default 512).")
async def stopmotion(interaction: discord.Interaction, hours: int = 24, fps: int = 4, max_frames: int = 60, max_side: int = 512):
    from PIL import Image
    import aiohttp

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
        if url not in seen:
            seen.add(url)
            ordered.append(url)

    if not ordered:
        await interaction.followup.send(f"No images found in the last {hours} hour(s).")
        return

    if len(ordered) > max_frames:
        ordered = ordered[-max_frames:]

    frames: list[Image.Image] = []
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
        await interaction.followup.send("Not enough valid images to make a GIF (need at least 2).")
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

    out = BytesIO()
    duration_ms = int(1000 / fps)
    pal_frames = [im.convert("P", palette=Image.Palette.ADAPTIVE, colors=256) for im in normalized]
    pal_frames[0].save(out, format="GIF", save_all=True, append_images=pal_frames[1:], duration=duration_ms, loop=0, optimize=True, disposal=2)
    out.seek(0)

    await interaction.followup.send(
        content=f"GIF generated ({len(pal_frames)} frames, {fps} fps):",
        file=discord.File(fp=out, filename="stopmotion.gif")
    )

# -------------------- /TEMPLATE (single run) --------------------
@bot.tree.command(name="progress", description="Template progresser.")
@app_commands.describe(
    source_channel="Channel with the latest canvas update image. (Optional if you upload 'canvas_image')",
    canvas_image="Upload a canvas image directly (optional alternative to source_channel).",
    template="Template image attachment.",
    coords="(x1,y1)(x2,y2)(x3,y3)(x4,y4)",
    builders="How many people placing pixels in parallel (default 1)."
)
async def template_cmd(
    interaction: discord.Interaction,
    source_channel: Optional[discord.TextChannel],
    canvas_image: Optional[discord.Attachment],
    template: discord.Attachment,
    coords: str,
    builders: int = 1
):
    # Validate attachments
    if not (template.content_type or "").startswith("image/"):
        await interaction.response.send_message("That template doesn’t look like an image.", ephemeral=True)
        return
    if canvas_image and not (canvas_image.content_type or "").startswith("image/"):
        await interaction.response.send_message("The uploaded canvas image isn’t an image file.", ephemeral=True)
        return
    if not source_channel and not canvas_image:
        await interaction.response.send_message(
            "Provide a `source_channel` **or** upload a `canvas_image`.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    try:
        template_bytes = await template.read()
        # If user uploaded a canvas image, use that; else pull from source channel
        if canvas_image:
            canvas_bytes = await canvas_image.read()
            png_bytes, box_w, box_h, matched, total, pct = await run_markarea_once(
                canvas_bytes=canvas_bytes,
                template_bytes=template_bytes,
                coords=coords,
            )
        else:
            png_bytes, box_w, box_h, matched, total, pct = await run_markarea_once(
                source_channel=source_channel,
                template_bytes=template_bytes,
                coords=coords,
            )

        builders = max(1, int(builders))
        remaining, eta_seconds, h, m, s = _eta_from_progress(matched, total, builders)

        embed = _progress_embed(
            title="Progress",
            box_w=box_w,
            box_h=box_h,
            matched=matched,
            total=total,
            pct=pct,
            eta_hms=(h, m, s),
            remaining_px=remaining,
            builders=builders,
            image_filename="template_progress.png",
        )
        file = discord.File(fp=BytesIO(png_bytes), filename="template_progress.png")
        await interaction.followup.send(embed=embed, file=file)

    except Exception as e:
        await interaction.followup.send(f"❌ /template failed: `{type(e).__name__}: {e}`")

# -------------------- /CHECK (LIVE, unchanged) --------------------
_active_checks: dict[tuple[int, int], asyncio.Task] = {}

@bot.tree.command(name="live_progress", description="Live template progresser: posts on every source update.")
@app_commands.describe(
    mode="start or stop",
    source_channel="Channel containing the latest canvas updates.",
    output_channel="Channel to send updates to (defaults to where you run the command).",
    template="Template image attachment (required for start).",
    coords="(x1,y1)(x2,y2)(x3,y3)(x4,y4) (required for start).",
    duration_minutes="How long to run before auto-stopping (default 60).",
    builders="How many people placing pixels in parallel (default 1).",
    ping_role="Role to ping if progress goes backwards (optional)."
)
async def check(
    interaction: discord.Interaction,
    mode: str,
    source_channel: discord.TextChannel | None = None,
    output_channel: discord.TextChannel | None = None,
    template: discord.Attachment | None = None,
    coords: str | None = None,
    duration_minutes: int = 60,
    builders: int = 1,
    ping_role: discord.Role | None = None
):
    guild_id = interaction.guild_id or 0
    user_id = interaction.user.id
    key = (guild_id, user_id)

    mode = (mode or "").lower().strip()
    if mode not in ("start", "stop"):
        await interaction.response.send_message("Mode must be `start` or `stop`.", ephemeral=True)
        return

    if mode == "stop":
        task = _active_checks.pop(key, None)
        if task and not task.done():
            task.cancel()
            await interaction.response.send_message("🛑 Live check stopped.", ephemeral=True)
        else:
            await interaction.response.send_message("No active live check running.", ephemeral=True)
        return

    if source_channel is None or template is None or coords is None:
        await interaction.response.send_message(
            "For `mode=start`, you must provide: `source_channel`, `template`, and `coords`.",
            ephemeral=True
        )
        return
    if not (template.content_type or "").startswith("image/"):
        await interaction.response.send_message("That template doesn’t look like an image.", ephemeral=True)
        return

    if duration_minutes < 1: duration_minutes = 1
    if duration_minutes > 24 * 60: duration_minutes = 24 * 60
    builders = max(1, int(builders))

    out_ch = output_channel or interaction.channel
    if not isinstance(out_ch, discord.TextChannel):
        await interaction.response.send_message("Output channel must be a normal text channel.", ephemeral=True)
        return

    old = _active_checks.pop(key, None)
    if old and not old.done():
        old.cancel()

    template_bytes = await template.read()

    await interaction.response.send_message(
        f"✅ Live check started.\n"
        f"• Watching: {source_channel.mention}\n"
        f"• Output: {out_ch.mention}\n"
        f"• Builders: **{builders}**\n"
        f"• Duration: **{duration_minutes} min**\n"
        f"• Poll: **30s** (posts on every source update)\n"
        f"• Ping role: {ping_role.mention if ping_role else 'None'}",
        ephemeral=True
    )

    async def runner():
        end_ts = time.time() + duration_minutes * 60
        last_marker = (None, None, None)
        last_matched: Optional[int] = None
        last_total: Optional[int] = None
        last_pct: Optional[float] = None

        while time.time() < end_ts:
            try:
                marker = await _get_latest_canvas_marker(source_channel)
                if marker != last_marker and marker[2] is not None:
                    png_bytes, box_w, box_h, matched, total, pct = await run_markarea_once(
                        source_channel=source_channel,
                        template_bytes=template_bytes,
                        coords=coords,
                    )

                    delta_matched = None
                    delta_pct = None
                    if last_matched is not None and last_total == total and last_pct is not None:
                        delta_matched = matched - last_matched
                        delta_pct = pct - last_pct
                        if ping_role is not None and delta_matched < 0:
                            lost = -delta_matched
                            dec_pct = (-delta_pct) if (delta_pct is not None and delta_pct < 0) else None
                            extra = f" (**-{dec_pct:.2f}%**)" if dec_pct is not None else ""
                            await out_ch.send(f"{ping_role.mention} ⚠️ **Users may be attacking** — progress went backwards (**-{lost:,}** pixels){extra}.")

                    remaining, eta_seconds, h, m, s = _eta_from_progress(matched, total, builders)

                    embed = _progress_embed(
                        title="Progress",
                        box_w=box_w,
                        box_h=box_h,
                        matched=matched,
                        total=total,
                        pct=pct,
                        delta_matched=delta_matched,
                        delta_pct=delta_pct,
                        eta_hms=(h, m, s),
                        remaining_px=remaining,
                        builders=builders,
                        image_filename="template_progress.png",
                    )
                    file = discord.File(fp=BytesIO(png_bytes), filename="template_progress.png")
                    await out_ch.send(embed=embed, file=file)

                    last_marker = marker
                    last_matched = matched
                    last_total = total
                    last_pct = pct

                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await out_ch.send(f"⚠️ Live check error: `{type(e).__name__}: {e}`")
                await asyncio.sleep(30)

        await out_ch.send("✅ Live check finished (duration reached).")

    task = asyncio.create_task(runner())
    _active_checks[key] = task

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)