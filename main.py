import os
import time
import json
import asyncio
import urllib.request
from io import BytesIO
from datetime import timedelta
import re
import math
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import discord
from discord import app_commands
from discord.ext import commands

# -------------------- CONFIG --------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

MODEL = "gpt-4.1-mini"
MAX_OUTPUT_TOKENS = 450

COOLDOWN_S = 6
_last_used = {}

# ETA rule (your confirmed rule)
COOLDOWN_SECONDS_PER_PIXEL = 15

# How often /live_progress polls the source channel for "new message or edited message"
# 30 seconds = 30
# 30 minutes = 1800
SOURCE_POLL_SECONDS = 30

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
- Using more than a 1 account at a time
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
        await asyncio.to_thread(db_init)
    except Exception as e:
        print("⚠️ DB init failed:", e)

    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")

# -------------------- DB (Postgres presets) --------------------
def _db_url_with_sslmode(url: str) -> str:
    if not url:
        return url
    try:
        p = urlparse(url)
        q = parse_qs(p.query)
        if "sslmode" not in q:
            q["sslmode"] = ["require"]
            new_query = urlencode(q, doseq=True)
            p = p._replace(query=new_query)
            return urlunparse(p)
        return url
    except Exception:
        return url

def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set (Railway Postgres not configured).")
    import psycopg2
    return psycopg2.connect(_db_url_with_sslmode(DATABASE_URL))

def db_init():
    if not DATABASE_URL:
        return
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_presets (
                    guild_id BIGINT NOT NULL,
                    user_id  BIGINT NOT NULL,
                    name     TEXT   NOT NULL,
                    data     JSONB  NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id, name)
                );
                """)
    finally:
        conn.close()

def db_upsert_preset(guild_id: int, user_id: int, name: str, data: dict):
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_presets (guild_id, user_id, name, data)
                    VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (guild_id, user_id, name)
                    DO UPDATE SET data = EXCLUDED.data, updated_at = NOW();
                    """,
                    (guild_id, user_id, name, json.dumps(data)),
                )
    finally:
        conn.close()

def db_get_preset(guild_id: int, user_id: int, name: str) -> dict | None:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM bot_presets WHERE guild_id=%s AND user_id=%s AND name=%s;",
                (guild_id, user_id, name),
            )
            row = cur.fetchone()
            if not row:
                return None
            return row[0]
    finally:
        conn.close()

def db_list_presets(guild_id: int, user_id: int) -> list[tuple[str, str]]:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, updated_at FROM bot_presets WHERE guild_id=%s AND user_id=%s ORDER BY updated_at DESC;",
                (guild_id, user_id),
            )
            rows = cur.fetchall() or []
            out = []
            for n, ts in rows:
                out.append((str(n), ts.isoformat() if ts else ""))
            return out
    finally:
        conn.close()

def db_delete_preset(guild_id: int, user_id: int, name: str) -> bool:
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM bot_presets WHERE guild_id=%s AND user_id=%s AND name=%s;",
                    (guild_id, user_id, name),
                )
                return cur.rowcount > 0
    finally:
        conn.close()

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

async def _latest_message_signature(channel: discord.TextChannel | discord.Thread) -> tuple[int, float]:
    """
    Returns (latest_message_id, latest_message_edit_or_create_ts)
    Used to detect: new message OR edited latest message.
    """
    latest = None
    async for msg in channel.history(limit=1, oldest_first=False):
        latest = msg
    if not latest:
        return (0, 0.0)
    ts = latest.edited_at.timestamp() if latest.edited_at else latest.created_at.timestamp()
    return (int(latest.id), float(ts))

def parse_coords_4pairs(coords: str):
    matches = re.findall(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)", coords or "")
    if len(matches) != 4:
        raise ValueError("Coords must be exactly 4 pairs like (x1,y1)(x2,y2)(x3,y3)(x4,y4).")
    return [(int(x), int(y)) for x, y in matches]

def _make_template_progress_preview(canvas_crop, template_crop, red_alpha: int = 140):
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

def _seconds_to_hms(total_seconds: int) -> tuple[int, int, int]:
    total_seconds = max(0, int(total_seconds))
    h = total_seconds // 3600
    total_seconds -= h * 3600
    m = total_seconds // 60
    s = total_seconds - m * 60
    return h, m, s

def _eta_from_progress(matched: int, total: int, builders: int) -> tuple[int, int, int, int, int]:
    builders = max(1, int(builders))
    total = max(0, int(total))
    matched = max(0, min(int(matched), total))
    remaining = total - matched
    ticks = math.ceil(remaining / builders) if remaining > 0 else 0
    eta_seconds = ticks * COOLDOWN_SECONDS_PER_PIXEL
    h, m, s = _seconds_to_hms(eta_seconds)
    return remaining, eta_seconds, h, m, s

async def run_markarea_once(
    *,
    source_channel: discord.TextChannel,
    template_bytes: bytes,
    coords: str,
):
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

def _progress_embed(
    *,
    title: str,
    box_w: int,
    box_h: int,
    matched: int,
    total: int,
    pct: float,
    builders: int,
    delta_matched: int | None,
    delta_pct: float | None,
    pct_decrease: float | None,
    h: int,
    m: int,
    s: int,
    remaining: int,
) -> discord.Embed:
    color = discord.Color.blurple()
    if delta_matched is not None:
        if delta_matched > 0:
            color = discord.Color.green()
        elif delta_matched < 0:
            color = discord.Color.red()

    e = discord.Embed(title=title, color=color)
    e.add_field(name="Region", value=f"`{box_w}×{box_h}`", inline=True)
    e.add_field(name="Pixels", value=f"`{matched:,} / {total:,}`", inline=True)
    e.add_field(name="Completion", value=f"**{pct:.2f}%**", inline=True)

    if delta_matched is not None and delta_pct is not None:
        sign = "+" if delta_matched >= 0 else ""
        signp = "+" if delta_pct >= 0 else ""
        extra = f"{sign}{delta_matched:,} px ({signp}{delta_pct:.2f}%)"
        e.add_field(name="Change since last update", value=extra, inline=False)

    if pct_decrease is not None and pct_decrease > 0:
        e.add_field(name="Percentage decrease", value=f"**-{pct_decrease:.2f}%**", inline=False)

    e.add_field(
        name="ETA",
        value=f"**{h}h {m}m {s}s** (`{remaining:,}` px, builders={max(1,int(builders))}, {COOLDOWN_SECONDS_PER_PIXEL}s/px)",
        inline=False
    )

    e.set_image(url="attachment://template_progress.png")
    e.set_footer(text=f"Auto-updates when source channel changes • Poll: {SOURCE_POLL_SECONDS}s")
    return e

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

    hours = max(1, min(168, int(hours)))
    fps = max(1, min(15, int(fps)))
    max_frames = max(1, min(250, int(max_frames)))
    max_side = max(64, min(1024, int(max_side)))

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

# -------------------- PRESETS COMMANDS (SIMPLIFIED) --------------------
def _require_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing. Create Railway Postgres and ensure DATABASE_URL exists.")

@bot.tree.command(name="preset_save", description="Save a preset.")
@app_commands.describe(
    name="Preset name (e.g. logo1)",
    template="Template image attachment to save (used later without reupload).",
    coords="(x1,y1)(x2,y2)(x3,y3)(x4,y4)",
)
async def preset_save(
    interaction: discord.Interaction,
    name: str,
    template: discord.Attachment,
    coords: str,
):
    try:
        _require_db()
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    if not name or len(name) > 32:
        await interaction.response.send_message("Preset name must be 1–32 characters.", ephemeral=True)
        return

    if not (template.content_type or "").startswith("image/"):
        await interaction.response.send_message("That template doesn’t look like an image.", ephemeral=True)
        return

    try:
        parse_coords_4pairs(coords)
    except Exception as e:
        await interaction.response.send_message(f"❌ Invalid coords: {e}", ephemeral=True)
        return

    # Only store what you want in presets
    data = {
        "coords": coords,
        "template_url": template.url,
        "template_filename": template.filename,
        "template_content_type": template.content_type or "image/*",
    }

    guild_id = interaction.guild_id or 0
    user_id = interaction.user.id

    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        await asyncio.to_thread(db_upsert_preset, guild_id, user_id, name, data)
        await interaction.followup.send(
            f"✅ Preset saved: **{name}**\n"
            f"• Coords: `{coords}`\n"
            f"• Template: `{template.filename}`",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to save preset: `{type(e).__name__}: {e}`", ephemeral=True)

@bot.tree.command(name="preset_list", description="List your saved presets.")
async def preset_list(interaction: discord.Interaction):
    try:
        _require_db()
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    guild_id = interaction.guild_id or 0
    user_id = interaction.user.id

    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        rows = await asyncio.to_thread(db_list_presets, guild_id, user_id)
        if not rows:
            await interaction.followup.send("No presets saved yet.", ephemeral=True)
            return
        lines = [f"• **{n}**" for (n, _ts) in rows[:25]]
        await interaction.followup.send("📦 **Your presets:**\n" + "\n".join(lines), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to list presets: `{type(e).__name__}: {e}`", ephemeral=True)

@bot.tree.command(name="preset_show", description="Show what a preset contains.")
@app_commands.describe(name="Preset name")
async def preset_show(interaction: discord.Interaction, name: str):
    try:
        _require_db()
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    guild_id = interaction.guild_id or 0
    user_id = interaction.user.id
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        data = await asyncio.to_thread(db_get_preset, guild_id, user_id, name)
        if not data:
            await interaction.followup.send("Preset not found.", ephemeral=True)
            return

        msg = (
            f"🧩 **Preset `{name}`**\n"
            f"• Coords: `{data.get('coords')}`\n"
            f"• Template: `{data.get('template_filename', 'unknown')}`"
        )
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{type(e).__name__}: {e}`", ephemeral=True)

@bot.tree.command(name="preset_delete", description="Delete a saved preset.")
@app_commands.describe(name="Preset name")
async def preset_delete(interaction: discord.Interaction, name: str):
    try:
        _require_db()
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    guild_id = interaction.guild_id or 0
    user_id = interaction.user.id

    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        ok = await asyncio.to_thread(db_delete_preset, guild_id, user_id, name)
        await interaction.followup.send("🗑️ Deleted." if ok else "Preset not found.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{type(e).__name__}: {e}`", ephemeral=True)

async def _download_template_from_url(url: str) -> bytes:
    import aiohttp
    async with aiohttp.ClientSession() as session:
        return await _download_bytes(session, url, timeout_s=45)

# -------------------- /TEMPLATE (single run) --------------------
@bot.tree.command(name="progress", description="Template progresser.")
@app_commands.describe(
    source_channel="Channel with the latest canvas update image.",
    template="Template image attachment.",
    coords="(x1,y1)(x2,y2)(x3,y3)(x4,y4)",
    builders="How many people placing pixels in parallel (default 1).",
    preset="Optional: use a saved preset (fills coords + template)."
)
async def template_cmd(
    interaction: discord.Interaction,
    source_channel: discord.TextChannel | None = None,
    template: discord.Attachment | None = None,
    coords: str | None = None,
    builders: int = 1,
    preset: str | None = None,
):
    await interaction.response.defer(thinking=True)

    template_bytes: bytes | None = None

    if preset:
        if not DATABASE_URL:
            await interaction.followup.send("❌ Presets require DATABASE_URL (Railway Postgres).")
            return
        guild_id = interaction.guild_id or 0
        user_id = interaction.user.id
        pdata = await asyncio.to_thread(db_get_preset, guild_id, user_id, preset)
        if not pdata:
            await interaction.followup.send("❌ Preset not found.")
            return

        if coords is None:
            coords = pdata.get("coords")

        if template and (template.content_type or "").startswith("image/"):
            template_bytes = await template.read()
        else:
            turl = pdata.get("template_url")
            if turl:
                try:
                    template_bytes = await _download_template_from_url(turl)
                except Exception as e:
                    await interaction.followup.send(f"❌ Failed to download preset template: {e}")
                    return

        if template_bytes is None:
            await interaction.followup.send("❌ No template provided and preset has no template_url.")
            return
    else:
        if template is None or coords is None:
            await interaction.followup.send("❌ Provide `template` and `coords` (or use `preset`).")
            return
        if not (template.content_type or "").startswith("image/"):
            await interaction.followup.send("❌ That template doesn’t look like an image.")
            return
        template_bytes = await template.read()

    if source_channel is None:
        await interaction.followup.send("❌ You must provide `source_channel` (presets no longer store it).")
        return
    if coords is None:
        await interaction.followup.send("❌ Missing coords.")
        return

    try:
        png_bytes, box_w, box_h, matched, total, pct = await run_markarea_once(
            source_channel=source_channel,
            template_bytes=template_bytes,
            coords=coords,
        )

        remaining, _eta_seconds, h, m, s = _eta_from_progress(matched, total, builders)

        out = BytesIO(png_bytes)
        await interaction.followup.send(
            content=(
                f" **Template Progress**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f" **Region**: `{box_w}×{box_h}`\n"
                f" **Pixels**: `{matched:,} / {total:,}`\n"
                f" **Completion**: **{pct:.2f}%**\n"
                f" **ETA**: **{h}h {m}m {s}s**  (`{remaining:,}` px, builders={max(1,int(builders))}, {COOLDOWN_SECONDS_PER_PIXEL}s/px)"
            ),
            file=discord.File(fp=out, filename="template_progress.png")
        )
    except Exception as e:
        await interaction.followup.send(f"❌ /progress failed: `{type(e).__name__}: {e}`")

# -------------------- /LIVE_PROGRESS (infinite until stop) --------------------
_active_checks: dict[tuple[int, int], asyncio.Task] = {}

@bot.tree.command(name="live_progress", description="Live template progresser: updates ONLY when source channel posts/edits (runs until stop).")
@app_commands.describe(
    mode="start or stop",
    source_channel="Channel containing the canvas updates (required).",
    template="Template image attachment (required unless using preset).",
    coords="(x1,y1)(x2,y2)(x3,y3)(x4,y4) (required unless using preset).",
    builders="How many people placing pixels in parallel (default 1).",
    ping_role="Role to ping if progress goes backwards (optional).",
    preset="Optional: use a saved preset (fills coords + template)."
)
async def live_progress(
    interaction: discord.Interaction,
    mode: str,
    source_channel: discord.TextChannel | None = None,
    template: discord.Attachment | None = None,
    coords: str | None = None,
    builders: int = 1,
    ping_role: discord.Role | None = None,
    preset: str | None = None,
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
            await interaction.response.send_message("🛑 Live progress stopped.", ephemeral=True)
        else:
            await interaction.response.send_message("No active live progress running.", ephemeral=True)
        return

    if source_channel is None:
        await interaction.response.send_message("❌ You must provide `source_channel`.", ephemeral=True)
        return

    builders = max(1, int(builders))
    template_bytes: bytes | None = None

    if preset:
        if not DATABASE_URL:
            await interaction.response.send_message("❌ Presets require DATABASE_URL (Railway Postgres).", ephemeral=True)
            return
        pdata = await asyncio.to_thread(db_get_preset, guild_id, user_id, preset)
        if not pdata:
            await interaction.response.send_message("❌ Preset not found.", ephemeral=True)
            return

        if coords is None:
            coords = pdata.get("coords")

        if template and (template.content_type or "").startswith("image/"):
            template_bytes = await template.read()
        else:
            turl = pdata.get("template_url")
            if not turl:
                await interaction.response.send_message("❌ Preset has no template_url. Re-save preset with a template.", ephemeral=True)
                return
            try:
                template_bytes = await _download_template_from_url(turl)
            except Exception as e:
                await interaction.response.send_message(f"❌ Failed to download preset template: {e}", ephemeral=True)
                return
    else:
        if template is None or coords is None:
            await interaction.response.send_message(
                "For `mode=start`, provide `template` and `coords` (or use `preset`).",
                ephemeral=True
            )
            return
        if not (template.content_type or "").startswith("image/"):
            await interaction.response.send_message("That template doesn’t look like an image.", ephemeral=True)
            return
        template_bytes = await template.read()

    if coords is None or template_bytes is None:
        await interaction.response.send_message("❌ Missing template/coords.", ephemeral=True)
        return

    out_ch = interaction.channel
    if not isinstance(out_ch, discord.TextChannel):
        await interaction.response.send_message("This command must be used in a normal text channel.", ephemeral=True)
        return

    old = _active_checks.pop(key, None)
    if old and not old.done():
        old.cancel()

    await interaction.response.send_message(
        f"✅ Live progress started.\n"
        f"• Polling source: every **{SOURCE_POLL_SECONDS}s**\n"
        f"• Builders: **{builders}**\n"
        f"• Ping role: {ping_role.mention if ping_role else 'None'}"
        + (f"\n• Preset: **{preset}**" if preset else ""),
        ephemeral=True
    )

    async def runner():
        last_sig = (0, 0.0)
        last_matched: int | None = None
        last_pct: float | None = None
        status_msg: discord.Message | None = None

        try:
            last_sig = await _latest_message_signature(source_channel)
        except Exception:
            last_sig = (0, 0.0)

        # First post immediately
        try:
            png_bytes, box_w, box_h, matched, total, pct = await run_markarea_once(
                source_channel=source_channel,
                template_bytes=template_bytes,
                coords=coords,
            )
            remaining, _eta_seconds, h, m, s = _eta_from_progress(matched, total, builders)
            emb = _progress_embed(
                title="Live Template Progress",
                box_w=box_w, box_h=box_h,
                matched=matched, total=total, pct=pct,
                builders=builders,
                delta_matched=None, delta_pct=None, pct_decrease=None,
                h=h, m=m, s=s, remaining=remaining
            )
            out = BytesIO(png_bytes)
            f = discord.File(fp=out, filename="template_progress.png")
            status_msg = await out_ch.send(embed=emb, file=f)

            last_matched = matched
            last_pct = pct
        except Exception as e:
            await out_ch.send(f"⚠️ Live progress initial error: `{type(e).__name__}: {e}`")

        while True:
            try:
                sig = await _latest_message_signature(source_channel)

                if sig != last_sig:
                    last_sig = sig

                    png_bytes, box_w, box_h, matched, total, pct = await run_markarea_once(
                        source_channel=source_channel,
                        template_bytes=template_bytes,
                        coords=coords,
                    )

                    delta_matched = (matched - last_matched) if last_matched is not None else None
                    delta_pct = (pct - last_pct) if last_pct is not None else None
                    pct_decrease = (last_pct - pct) if (last_pct is not None and pct < last_pct) else None

                    if delta_matched is not None and delta_matched < 0 and ping_role is not None:
                        await out_ch.send(
                            f"{ping_role.mention} ⚠️ **Users may be attacking** — progress went backwards (**{delta_matched:,}** matched pixels)."
                        )

                    remaining, _eta_seconds, h, m, s = _eta_from_progress(matched, total, builders)
                    emb = _progress_embed(
                        title="Live Template Progress",
                        box_w=box_w, box_h=box_h,
                        matched=matched, total=total, pct=pct,
                        builders=builders,
                        delta_matched=delta_matched,
                        delta_pct=delta_pct,
                        pct_decrease=pct_decrease,
                        h=h, m=m, s=s, remaining=remaining
                    )

                    out = BytesIO(png_bytes)
                    f = discord.File(fp=out, filename="template_progress.png")

                    if status_msg is None:
                        status_msg = await out_ch.send(embed=emb, file=f)
                    else:
                        try:
                            await status_msg.edit(embed=emb, attachments=[], files=[f])
                        except Exception:
                            status_msg = await out_ch.send(embed=emb, file=f)

                    last_matched = matched
                    last_pct = pct

            except asyncio.CancelledError:
                raise
            except Exception as e:
                await out_ch.send(f"⚠️ Live progress error: `{type(e).__name__}: {e}`")

            await asyncio.sleep(SOURCE_POLL_SECONDS)

    task = asyncio.create_task(runner())
    _active_checks[key] = task

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)