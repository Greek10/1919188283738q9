# Discord Slash Bot (Pydroid-friendly)
# - /ask -> OpenAI rule helper (Embed output)
# - /stopmotion -> makes a stop-motion GIF from images in channel history
# - /template -> template progresser (single run
# - /check -> LIVE template progresser (repeats every N minutes, auto-stops after duration)
#            + optional role ping on regression (attack)
#            + ETA estimate when it has at least 2 samples
# - /snapshots -> NEW: periodically grabs the most recent screenshot in a source channel
#                and re-posts it into an archive channel at a user-set interval
#
# Requirements:
#   pip install -U discord.py pillow aiohttp
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
from typing import Optional, Tuple

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
    try:
        await bot.tree.sync()
    except Exception as e:
        print("⚠️ Slash sync error:", e)
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
    # User uses bottom-left origin; image uses top-left.
    return (img_h - 1) - y_user

async def _find_latest_image_url(channel: discord.TextChannel | discord.Thread, limit: int = 50) -> Optional[str]:
    async for msg in channel.history(limit=limit, oldest_first=False):
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

def parse_coords_4pairs(coords: str):
    matches = re.findall(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)", coords or "")
    if len(matches) != 4:
        raise ValueError("Coords must be exactly 4 pairs like (x1,y1)(x2,y2)(x3,y3)(x4,y4).")
    return [(int(x), int(y)) for x, y in matches]

def _make_template_progress_preview(canvas_crop, template_crop, red_alpha: int = 150):
    """
    Output = template normally.
    If template pixel alpha>0 and canvas RGB != template RGB -> apply red 'light' overlay.
    If matches -> show clean template color.
    """
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

            cr, cg, cb, _ = cpx[x, y]

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

def _exact_progress_percent(canvas_rgba, template_rgba) -> Tuple[float, int, int]:
    """
    Progress = exact RGB matches / template non-transparent pixels.
    """
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
            cr, cg, cb, _ = cpx[x, y]
            total += 1
            if (cr, cg, cb) == (tr, tg, tb):
                matched += 1

    pct = (matched / total * 100.0) if total else 0.0
    return pct, matched, total

def _fmt_eta_minutes(minutes: float) -> str:
    if minutes <= 0 or minutes != minutes or minutes == float("inf"):
        return "Unknown"
    secs = int(minutes * 60)
    h = secs // 3600
    secs -= h * 3600
    m = secs // 60
    s = secs - m * 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

async def run_markarea_once(
    *,
    source_channel: discord.TextChannel,
    template_bytes: bytes,
    coords: str,
):
    """
    Returns: (png_bytes, box_w, box_h, matched, total, pct)
    """
    from PIL import Image
    import aiohttp

    pts = parse_coords_4pairs(coords)
    (x1, y1), (x2, y2), (x3, y3), (x4, y4) = pts

    canvas_url = await _find_latest_image_url(source_channel)
    if not canvas_url:
        raise RuntimeError("No recent canvas image found in the source channel.")

    async with aiohttp.ClientSession() as session:
        canvas_bytes = await _download_bytes(session, canvas_url, timeout_s=30)

    canvas = Image.open(BytesIO(canvas_bytes)).convert("RGBA")
    tmpl = Image.open(BytesIO(template_bytes)).convert("RGBA")

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
        raise RuntimeError("Those coordinates create a region that’s too small.")

    canvas_crop = canvas.crop((left, top, right, bottom))

    # Template crop rule:
    # - If already region-sized -> no crop
    # - Else crop using same coords in template coordinate system
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
@bot.tree.command(name="ask", description="Check if something is bannable under the game rules (not official).")
@app_commands.describe(message="Describe what happened / what was drawn / what was said.")
async def ask(interaction: discord.Interaction, message: str):
    if not cooldown_ok(interaction.user.id):
        await interaction.response.send_message("⏳ Cooldown active.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    raw = await call_openai_async(SYSTEM_PROMPT, message)
    await interaction.followup.send(embed=build_embed(safe_parse(raw)))

# -------------------- /STOPMOTION --------------------
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

@bot.tree.command(name="stopmotion", description="Make a stop-motion GIF from images posted in this channel in the last N hours.")
@app_commands.describe(hours="Hours back (default 24).", fps="FPS (default 4).", max_frames="Max frames (default 60).", max_side="Max side (default 512).")
async def stopmotion(interaction: discord.Interaction, hours: int = 24, fps: int = 4, max_frames: int = 60, max_side: int = 512):
    from PIL import Image
    import aiohttp

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
@bot.tree.command(name="template", description="Template progresser (single run).")
@app_commands.describe(
    source_channel="Channel with the latest canvas update image.",
    template="Template image attachment.",
    coords="(x1,y1)(x2,y2)(x3,y3)(x4,y4)"
)
async def template_cmd(interaction: discord.Interaction, source_channel: discord.TextChannel, template: discord.Attachment, coords: str):
    if not (template.content_type or "").startswith("image/"):
        await interaction.response.send_message("That template doesn’t look like an image.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        template_bytes = await template.read()
        png_bytes, box_w, box_h, matched, total, pct = await run_markarea_once(
            source_channel=source_channel,
            template_bytes=template_bytes,
            coords=coords,
        )

        remaining = max(0, total - matched)
        out = BytesIO(png_bytes)

        await interaction.followup.send(
            content=(
                f"**Template Progress**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"**Region:** `{box_w}×{box_h}`\n"
                f"**Pixels:** `{matched:,} / {total:,}` (remaining `{remaining:,}`)\n"
                f"**Completion:** **{pct:.2f}%**\n"
                f"**ETA:** `Unknown` (needs multiple updates to estimate)"
            ),
            file=discord.File(fp=out, filename="template_progress.png")
        )
    except Exception as e:
        await interaction.followup.send(f"❌ /template failed: `{type(e).__name__}: {e}`")

# -------------------- /CHECK (LIVE) --------------------
# One active check per user per guild.
_active_checks: dict[tuple[int, int], asyncio.Task] = {}

@bot.tree.command(name="check", description="Live template progresser: repeats updates until duration ends.")
@app_commands.describe(
    mode="start or stop",
    source_channel="Channel containing the latest canvas updates.",
    output_channel="Channel to send updates to (defaults to where you run the command).",
    template="Template image attachment (required for start).",
    coords="(x1,y1)(x2,y2)(x3,y3)(x4,y4) (required for start).",
    interval_minutes="How often to update (default 12).",
    duration_minutes="How long to run before auto-stopping (default 60).",
    ping_role="Optional: role to ping when regression is detected.",
    attack_threshold="Only ping if pixels decrease by at least this many since last update (default 1).",
    confirm_attacks="Require 2 negative updates in a row before pinging (default true)."
)
async def check(
    interaction: discord.Interaction,
    mode: str,
    source_channel: Optional[discord.TextChannel] = None,
    output_channel: Optional[discord.TextChannel] = None,
    template: Optional[discord.Attachment] = None,
    coords: Optional[str] = None,
    interval_minutes: int = 12,
    duration_minutes: int = 60,
    ping_role: Optional[discord.Role] = None,
    attack_threshold: int = 1,
    confirm_attacks: bool = True,
):
    guild_id = interaction.guild_id or 0
    user_id = interaction.user.id
    key = (guild_id, user_id)

    mode = (mode or "").lower().strip()
    if mode not in ("start", "stop"):
        await interaction.response.send_message("Mode must be `start` or `stop`.", ephemeral=True)
        return

    # STOP
    if mode == "stop":
        task = _active_checks.pop(key, None)
        if task and not task.done():
            task.cancel()
            await interaction.response.send_message("🛑 Live check stopped.", ephemeral=True)
        else:
            await interaction.response.send_message("No active live check running.", ephemeral=True)
        return

    # START validation
    if source_channel is None or template is None or coords is None:
        await interaction.response.send_message(
            "For `mode=start`, you must provide: `source_channel`, `template`, and `coords`.",
            ephemeral=True
        )
        return

    if not (template.content_type or "").startswith("image/"):
        await interaction.response.send_message("That template doesn’t look like an image.", ephemeral=True)
        return

    if interval_minutes < 1:
        interval_minutes = 1
    if interval_minutes > 120:
        interval_minutes = 120

    if duration_minutes < 1:
        duration_minutes = 1
    if duration_minutes > 24 * 60:
        duration_minutes = 24 * 60

    if attack_threshold < 1:
        attack_threshold = 1
    if attack_threshold > 999999:
        attack_threshold = 999999

    out_ch = output_channel or interaction.channel
    if not isinstance(out_ch, discord.TextChannel):
        await interaction.response.send_message("Output channel must be a normal text channel.", ephemeral=True)
        return

    # cancel old if exists
    old = _active_checks.pop(key, None)
    if old and not old.done():
        old.cancel()

    template_bytes = await template.read()

    await interaction.response.send_message(
        f"✅ Live check started.\n"
        f"• Updates: every **{interval_minutes} min**\n"
        f"• Duration: **{duration_minutes} min**\n"
        f"• Output: {out_ch.mention}",
        ephemeral=True
    )

    async def runner():
        start_ts = time.time()
        end_ts = start_ts + duration_minutes * 60

        last_matched: Optional[int] = None
        last_total: Optional[int] = None
        last_ts: Optional[float] = None
        negative_streak = 0

        while time.time() < end_ts:
            try:
                png_bytes, box_w, box_h, matched, total, pct = await run_markarea_once(
                    source_channel=source_channel,
                    template_bytes=template_bytes,
                    coords=coords,
                )

                # delta calc
                delta_str = "—"
                eta_str = "Unknown"
                finish_str = "Unknown"
                remaining = max(0, total - matched)

                now_ts = time.time()
                if last_matched is not None and last_ts is not None and total > 0:
                    delta = matched - last_matched
                    if delta > 0:
                        delta_str = f"+{delta:,}"
                        negative_streak = 0
                    elif delta < 0:
                        delta_str = f"{delta:,}"
                        negative_streak += 1
                    else:
                        delta_str = "0"
                        negative_streak = 0

                    # ETA estimate only if we have positive rate
                    dt_min = max(0.001, (now_ts - last_ts) / 60.0)
                    rate_ppm = (matched - last_matched) / dt_min  # pixels per minute
                    if rate_ppm > 0:
                        eta_min = remaining / rate_ppm
                        eta_str = _fmt_eta_minutes(eta_min)
                        finish_unix = time.time() + eta_min * 60
                        finish_dt = discord.utils.utcnow() + timedelta(seconds=int(eta_min * 60))
                        finish_str = finish_dt.strftime("%Y-%m-%d %H:%M UTC")

                    # Attack ping logic
                    if delta < 0 and abs(delta) >= attack_threshold and ping_role is not None:
                        confirmed = True
                        if confirm_attacks:
                            confirmed = (negative_streak >= 2)
                        if confirmed:
                            await out_ch.send(
                                content=f"{ping_role.mention} ⚠️ **Attack suspected** — progress dropped by **{abs(delta):,}** pixels.",
                                allowed_mentions=discord.AllowedMentions(roles=True)
                            )
                            negative_streak = 0  # reset so it doesn't spam

                # post update
                out = BytesIO(png_bytes)
                msg = (
                    f"**Live Template Check**\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"**Region:** `{box_w}×{box_h}`\n"
                    f"**Pixels:** `{matched:,} / {total:,}` (remaining `{remaining:,}`)\n"
                    f"**Completion:** **{pct:.2f}%**\n"
                    f"**Change:** `{delta_str}`\n"
                    f"**ETA:** `{eta_str}`\n"
                    f"**Finish (UTC):** `{finish_str}`"
                )
                await out_ch.send(content=msg, file=discord.File(fp=out, filename="template_progress.png"))

                # auto-stop on completion
                if total > 0 and matched >= total:
                    await out_ch.send("✅ Template complete — live check stopped automatically.")
                    break

                last_matched = matched
                last_total = total
                last_ts = now_ts

            except asyncio.CancelledError:
                raise
            except Exception as e:
                await out_ch.send(f"⚠️ Live check error: `{type(e).__name__}: {e}`")

            await asyncio.sleep(interval_minutes * 60)

        await out_ch.send("✅ Live check finished.")

    task = asyncio.create_task(runner())
    _active_checks[key] = task

# -------------------- /SNAPSHOTS (archive latest screenshot repeatedly) --------------------
# One active snapshot job per guild (simple + clean).
_active_snapshots: dict[int, asyncio.Task] = {}

@bot.tree.command(name="snapshots", description="Archive the most recent screenshot from a channel at a set interval.")
@app_commands.describe(
    mode="start or stop",
    source_channel="Channel where the canvas bot posts the (updating) screenshot.",
    archive_channel="Channel where snapshots should be posted.",
    interval_minutes="Minutes between snapshots (default 5).",
    duration_minutes="How long to run before auto-stopping (default 60).",
    only_when_changed="If true, only posts when the latest image URL changes (default true)."
)
async def snapshots(
    interaction: discord.Interaction,
    mode: str,
    source_channel: Optional[discord.TextChannel] = None,
    archive_channel: Optional[discord.TextChannel] = None,
    interval_minutes: int = 5,
    duration_minutes: int = 60,
    only_when_changed: bool = True
):
    guild_id = interaction.guild_id or 0
    mode = (mode or "").lower().strip()

    if mode not in ("start", "stop"):
        await interaction.response.send_message("Mode must be `start` or `stop`.", ephemeral=True)
        return

    if mode == "stop":
        task = _active_snapshots.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
            await interaction.response.send_message("🛑 Snapshot job stopped.", ephemeral=True)
        else:
            await interaction.response.send_message("No snapshot job running in this server.", ephemeral=True)
        return

    # start
    if source_channel is None or archive_channel is None:
        await interaction.response.send_message(
            "For `mode=start`, you must provide `source_channel` and `archive_channel`.",
            ephemeral=True
        )
        return

    if interval_minutes < 1:
        interval_minutes = 1
    if interval_minutes > 60:
        interval_minutes = 60

    if duration_minutes < 1:
        duration_minutes = 1
    if duration_minutes > 24 * 60:
        duration_minutes = 24 * 60

    # cancel old if exists
    old = _active_snapshots.pop(guild_id, None)
    if old and not old.done():
        old.cancel()

    await interaction.response.send_message(
        f"✅ Snapshots started.\n"
        f"• Source: {source_channel.mention}\n"
        f"• Archive: {archive_channel.mention}\n"
        f"• Interval: **{interval_minutes} min**\n"
        f"• Duration: **{duration_minutes} min**",
        ephemeral=True
    )

    async def runner():
        import aiohttp

        start_ts = time.time()
        end_ts = start_ts + duration_minutes * 60
        last_url: Optional[str] = None

        while time.time() < end_ts:
            try:
                url = await _find_latest_image_url(source_channel, limit=80)
                if not url:
                    await archive_channel.send("⚠️ No recent image found to snapshot.")
                else:
                    should_post = True
                    if only_when_changed and last_url is not None and url == last_url:
                        should_post = False

                    if should_post:
                        async with aiohttp.ClientSession() as session:
                            img_bytes = await _download_bytes(session, url, timeout_s=30)

                        ts = discord.utils.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                        file = discord.File(fp=BytesIO(img_bytes), filename="snapshot.png")
                        await archive_channel.send(
                            content=f"**Canvas Snapshot** — `{ts}`",
                            file=file
                        )
                        last_url = url

            except asyncio.CancelledError:
                raise
            except discord.Forbidden:
                # If we can't read history or post, stop to avoid infinite errors.
                try:
                    await interaction.followup.send("❌ Missing permissions (read history and/or send messages). Stopping snapshots.", ephemeral=True)
                except Exception:
                    pass
                break
            except Exception as e:
                await archive_channel.send(f"⚠️ Snapshot error: `{type(e).__name__}: {e}`")

            await asyncio.sleep(interval_minutes * 60)

        await archive_channel.send("✅ Snapshots finished.")

    task = asyncio.create_task(runner())
    _active_snapshots[guild_id] = task

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)