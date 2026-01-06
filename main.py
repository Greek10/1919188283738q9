import os
import time
import asyncio
from io import BytesIO
from datetime import timedelta
import re
import math

import discord
from discord import app_commands
from discord.ext import commands

# -------------------- CONFIG --------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0") or "0")          # for /progress /live_progress /archieved + /canvas
TIMELAPSE_CHANNEL_ID = int(os.getenv("TIMELAPSE_CHANNEL_ID", "0") or "0")    # for /timelapse

# OWNER (for owner-only slash commands)
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
    """
    Returns True if denied (not owner), else False.
    """
    if _is_owner_user_id(interaction.user.id):
        return False
    msg = "❌ Owner-only command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass
    return True

# -------------------- CHANNEL RESOLUTION --------------------
async def _get_text_channel_by_id(channel_id: int) -> discord.TextChannel:
    if not channel_id:
        raise RuntimeError("Source channel ID is not set. Set SOURCE_CHANNEL_ID / TIMELAPSE_CHANNEL_ID in env vars.")

    ch = bot.get_channel(channel_id)
    if ch is None:
        ch = await bot.fetch_channel(channel_id)

    if not isinstance(ch, discord.TextChannel):
        raise RuntimeError(f"Channel {channel_id} is not a text channel or is not accessible.")
    return ch

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

async def _find_latest_image_with_sig(channel: discord.TextChannel | discord.Thread):
    """
    Returns (signature, url) for the latest image in channel history.
    Signature changes if:
      - message id changes (new message)
      - edited_timestamp changes (edit)
      - image url differs
    """
    async for msg in channel.history(limit=30, oldest_first=False):
        edited = msg.edited_at.timestamp() if msg.edited_at else 0.0

        for a in msg.attachments:
            ct = (a.content_type or "")
            if ct.startswith("image/") and a.url:
                sig = f"{msg.id}:{edited}:{a.url}"
                return sig, a.url

        for e in msg.embeds:
            if e.image and e.image.url:
                sig = f"{msg.id}:{edited}:{e.image.url}"
                return sig, e.image.url
            if e.thumbnail and e.thumbnail.url:
                sig = f"{msg.id}:{edited}:{e.thumbnail.url}"
                return sig, e.thumbnail.url

    return None, None

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

    # Template crop rules:
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

# -------------------- /CANVAS (gets recent image from SOURCE_CHANNEL_ID) --------------------
@bot.tree.command(name="canvas", description="Gets the most recent image from the owner-set source channel.")
async def canvas(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        source_channel = await _get_text_channel_by_id(SOURCE_CHANNEL_ID)
    except Exception as e:
        await interaction.followup.send(f"❌ Source channel error: `{type(e).__name__}: {e}`")
        return

    try:
        sig, url = await _find_latest_image_with_sig(source_channel)
        if not url:
            await interaction.followup.send(f"❌ No recent image found in {source_channel.mention}.")
            return

        import aiohttp
        async with aiohttp.ClientSession() as session:
            img_bytes = await _download_bytes(session, url, timeout_s=45)

        fp = BytesIO(img_bytes)
        await interaction.followup.send(
            content=f"🖼️ Latest canvas image from {source_channel.mention}:",
            file=discord.File(fp=fp, filename="canvas_latest.png"),
        )
    except Exception as e:
        await interaction.followup.send(f"❌ /canvas failed: `{type(e).__name__}: {e}`")

# -------------------- /CHECK (OWNER-ONLY) --------------------
@bot.tree.command(name="check", description="(Owner-only) Lists servers the bot is in (name + member count).")
async def check(interaction: discord.Interaction):
    if await _deny_if_not_owner_interaction(interaction):
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    lines = []
    for g in bot.guilds:
        lines.append(f"- {g.name} — Members: {g.member_count}")

    if not lines:
        await interaction.followup.send("I'm not in any servers.", ephemeral=True)
        return

    header = f"**Servers ({len(lines)}):**\n"
    msg = header
    for ln in lines:
        if len(msg) + len(ln) + 1 > 1900:
            await interaction.followup.send(msg, ephemeral=True)
            msg = ""
        msg += ln + "\n"
    if msg.strip():
        await interaction.followup.send(msg, ephemeral=True)

# -------------------- /TIMELAPSE (uses owner-set TIMELAPSE_CHANNEL_ID) --------------------
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

@bot.tree.command(name="timelapse", description="Creates a timelapse GIF from the owner-set timelapse channel.")
@app_commands.describe(hours="Hours back (default 24).", fps="FPS (default 4).", max_frames="Max frames (default 60).", max_side="Max side (default 512).")
async def timelapse(interaction: discord.Interaction, hours: int = 24, fps: int = 4, max_frames: int = 60, max_side: int = 512):
    from PIL import Image
    import aiohttp

    await interaction.response.defer(thinking=True)

    try:
        channel = await _get_text_channel_by_id(TIMELAPSE_CHANNEL_ID)
    except Exception as e:
        await interaction.followup.send(f"❌ Timelapse source channel error: `{type(e).__name__}: {e}`")
        return

    hours = max(1, min(168, int(hours)))
    fps = max(1, min(15, int(fps)))
    max_frames = max(1, min(1000, int(max_frames)))
    max_side = max(64, min(1024, int(max_side)))

    cutoff = discord.utils.utcnow() - timedelta(hours=hours)

    found: list[str] = []
    try:
        async for msg in channel.history(limit=5000, after=cutoff, oldest_first=True):
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
        await interaction.followup.send("I don’t have permission to read message history in the timelapse channel.")
        return

    seen = set()
    ordered = []
    for url in found:
        if url not in seen:
            seen.add(url)
            ordered.append(url)

    if not ordered:
        await interaction.followup.send(f"No images found in {channel.mention} in the last {hours} hour(s).")
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
            canvas_im = Image.new("RGBA", (max_w, max_h), (0, 0, 0, 0))
            canvas_im.paste(im, ((max_w - im.width)//2, (max_h - im.height)//2))
            normalized.append(canvas_im)

    out = BytesIO()
    duration_ms = int(1000 / fps)
    pal_frames = [im.convert("P", palette=Image.Palette.ADAPTIVE, colors=256) for im in normalized]
    pal_frames[0].save(out, format="GIF", save_all=True, append_images=pal_frames[1:], duration=duration_ms, loop=0, optimize=True, disposal=2)
    out.seek(0)

    await interaction.followup.send(
        content=f"GIF generated from {channel.mention} ({len(pal_frames)} frames, {fps} fps):",
        file=discord.File(fp=out, filename="timelapse.gif")
    )

# -------------------- /PROGRESS (single run, uses owner-set SOURCE_CHANNEL_ID) --------------------
@bot.tree.command(name="progress", description="Template progresser (uses owner-set source channel).")
@app_commands.describe(
    template="Template image attachment.",
    coords="(x1,y1)(x2,y2)(x3,y3)(x4,y4)",
    builders="How many people placing pixels in parallel (default 1)."
)
async def progress_cmd(
    interaction: discord.Interaction,
    template: discord.Attachment,
    coords: str,
    builders: int = 1,
):
    await interaction.response.defer(thinking=True)

    try:
        source_channel = await _get_text_channel_by_id(SOURCE_CHANNEL_ID)
    except Exception as e:
        await interaction.followup.send(f"❌ Source channel error: `{type(e).__name__}: {e}`")
        return

    if not (template.content_type or "").startswith("image/"):
        await interaction.followup.send("❌ That template doesn’t look like an image.")
        return

    try:
        parse_coords_4pairs(coords)
    except Exception as e:
        await interaction.followup.send(f"❌ Invalid coords: {e}")
        return

    template_bytes = await template.read()

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
                f" **Source**: {source_channel.mention}\n"
                f" **Region**: `{box_w}×{box_h}`\n"
                f" **Pixels**: `{matched:,} / {total:,}`\n"
                f" **Completion**: **{pct:.2f}%**\n"
                f" **ETA**: **{h}h {m}m {s}s**  (`{remaining:,}` px, builders={max(1,int(builders))}, {COOLDOWN_SECONDS_PER_PIXEL}s/px)"
            ),
            file=discord.File(fp=out, filename="template_progress.png")
        )
    except Exception as e:
        await interaction.followup.send(f"❌ /progress failed: `{type(e).__name__}: {e}`")

# -------------------- /LIVE_PROGRESS (uses owner-set SOURCE_CHANNEL_ID) --------------------
_active_checks: dict[tuple[int, int], asyncio.Task] = {}

@bot.tree.command(name="live_progress", description="Live version of progress (uses owner-set source channel).")
@app_commands.describe(
    mode="start or stop",
    template="Template image attachment (required).",
    coords="(x1,y1)(x2,y2)(x3,y3)(x4,y4) (required).",
    builders="How many people placing pixels in parallel (default 1).",
    ping_role="Role to ping if progress goes backwards (optional)."
)
async def live_progress(
    interaction: discord.Interaction,
    mode: str,
    template: discord.Attachment | None = None,
    coords: str | None = None,
    builders: int = 1,
    ping_role: discord.Role | None = None,
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
            await interaction.response.send_message("🛑 /live_progress stopped.", ephemeral=True)
        else:
            await interaction.response.send_message("No active /live_progress running.", ephemeral=True)
        return

    if template is None or coords is None:
        await interaction.response.send_message("❌ Provide `template` and `coords`.", ephemeral=True)
        return

    try:
        source_channel = await _get_text_channel_by_id(SOURCE_CHANNEL_ID)
    except Exception as e:
        await interaction.response.send_message(f"❌ Source channel error: `{type(e).__name__}: {e}`", ephemeral=True)
        return

    if not (template.content_type or "").startswith("image/"):
        await interaction.response.send_message("❌ That template doesn’t look like an image.", ephemeral=True)
        return

    try:
        parse_coords_4pairs(coords)
    except Exception as e:
        await interaction.response.send_message(f"❌ Invalid coords: {e}", ephemeral=True)
        return

    template_bytes = await template.read()
    builders = max(1, int(builders))

    out_ch = interaction.channel
    if not isinstance(out_ch, discord.TextChannel):
        await interaction.response.send_message("This command must be used in a normal text channel.", ephemeral=True)
        return

    old = _active_checks.pop(key, None)
    if old and not old.done():
        old.cancel()

    await interaction.response.send_message(
        f"✅ /live_progress started.\n"
        f"• Source: {source_channel.mention}\n"
        f"• Poll: every **{POLL_SECONDS}s**\n"
        f"• Builders: **{builders}**\n"
        f"• Ping role: {ping_role.mention if ping_role else 'None'}\n"
        f"Stop with: `/live_progress mode:stop`",
        ephemeral=True
    )

    async def runner():
        last_sig: str | None = None
        last_matched: int | None = None

        while True:
            try:
                sig, _url = await _find_latest_image_with_sig(source_channel)
                if not sig:
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                if sig != last_sig:
                    last_sig = sig

                    png_bytes, box_w, box_h, matched, total, pct = await run_markarea_once(
                        source_channel=source_channel,
                        template_bytes=template_bytes,
                        coords=coords,
                    )

                    if last_matched is not None and matched < last_matched and ping_role is not None:
                        lost = last_matched - matched
                        dec_pct = (lost / last_matched * 100.0) if last_matched > 0 else 0.0
                        await out_ch.send(
                            f"{ping_role.mention} ⚠️ **Users may be attacking** — progress went backwards "
                            f"(**-{lost:,} px**, **-{dec_pct:.2f}%**)."
                        )

                    if last_matched is not None and matched > last_matched:
                        gained = matched - last_matched
                        inc_pct = (gained / total * 100.0) if total > 0 else 0.0
                        await out_ch.send(f"✅ **Progress made**: **+{gained:,} px** (**+{inc_pct:.2f}%** of template).")

                    last_matched = matched

                    remaining, _eta_seconds, h, m, s = _eta_from_progress(matched, total, builders)

                    out = BytesIO(png_bytes)
                    msg = (
                        f" **Live Template Progress**\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f" **Source**: {source_channel.mention}\n"
                        f" **Region**: `{box_w}×{box_h}`\n"
                        f" **Pixels**: `{matched:,} / {total:,}`\n"
                        f" **Completion**: **{pct:.2f}%**\n"
                        f" **ETA**: **{h}h {m}m {s}s**  (`{remaining:,}` px, builders={builders}, {COOLDOWN_SECONDS_PER_PIXEL}s/px)\n"
                        f" **Source update detected** (new/edited image message)."
                    )
                    await out_ch.send(content=msg, file=discord.File(fp=out, filename="template_progress.png"))

            except asyncio.CancelledError:
                raise
            except Exception as e:
                try:
                    await out_ch.send(f"⚠️ /live_progress error: `{type(e).__name__}: {e}`")
                except Exception:
                    pass

            await asyncio.sleep(POLL_SECONDS)

    task = asyncio.create_task(runner())
    _active_checks[key] = task

# -------------------- /ARCHIEVED (OWNER-ONLY, LIVE IMAGE ARCHIVER, uses SOURCE_CHANNEL_ID) --------------------
_active_archives: dict[tuple[int, int], asyncio.Task] = {}

@bot.tree.command(name="archieved", description="(Owner-only) Continuously repost the latest image from the owner-set source channel.")
@app_commands.describe(
    mode="start or stop",
    output_channel="Where to post copies (defaults to where you run the command)."
)
async def archieved(
    interaction: discord.Interaction,
    mode: str,
    output_channel: discord.TextChannel | None = None
):
    if await _deny_if_not_owner_interaction(interaction):
        return

    guild_id = interaction.guild_id or 0
    user_id = interaction.user.id
    key = (guild_id, user_id)

    mode = (mode or "").lower().strip()
    if mode not in ("start", "stop"):
        await interaction.response.send_message("Mode must be `start` or `stop`.", ephemeral=True)
        return

    if mode == "stop":
        task = _active_archives.pop(key, None)
        if task and not task.done():
            task.cancel()
            await interaction.response.send_message("🛑 /archieved stopped.", ephemeral=True)
        else:
            await interaction.response.send_message("No active /archieved running.", ephemeral=True)
        return

    try:
        source_channel = await _get_text_channel_by_id(SOURCE_CHANNEL_ID)
    except Exception as e:
        await interaction.response.send_message(f"❌ Source channel error: `{type(e).__name__}: {e}`", ephemeral=True)
        return

    out_ch = output_channel or interaction.channel
    if not isinstance(out_ch, discord.TextChannel):
        await interaction.response.send_message("Output channel must be a normal text channel.", ephemeral=True)
        return

    old = _active_archives.pop(key, None)
    if old and not old.done():
        old.cancel()

    await interaction.response.send_message(
        f"✅ /archieved started (owner-only).\n"
        f"• Source: {source_channel.mention}\n"
        f"• Posting to: {out_ch.mention}\n"
        f"• Poll: every **{POLL_SECONDS} seconds**\n"
        f"Stop with: `/archieved mode:stop`",
        ephemeral=True
    )

    async def runner():
        import aiohttp
        last_sig: str | None = None

        while True:
            try:
                sig, url = await _find_latest_image_with_sig(source_channel)
                if sig and url and sig != last_sig:
                    last_sig = sig

                    async with aiohttp.ClientSession() as session:
                        img_bytes = await _download_bytes(session, url, timeout_s=45)

                    fp = BytesIO(img_bytes)
                    await out_ch.send(
                        content=f"🗂️ **Archived canvas image update** (source: {source_channel.mention})",
                        file=discord.File(fp=fp, filename="archived.png")
                    )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                try:
                    await out_ch.send(f"⚠️ /archieved error: `{type(e).__name__}: {e}`")
                except Exception:
                    pass

            await asyncio.sleep(POLL_SECONDS)

    task = asyncio.create_task(runner())
    _active_archives[key] = task

# -------------------- /ARCHIEVED_TEXT (OWNER-ONLY, SIMPLE TEXT MIRROR) --------------------
# This is a lightweight "mirror edits as messages" watcher.
# It watches the latest message in source_channel and reposts when it changes (including edits).
_active_text_archivers: dict[tuple[int, int], asyncio.Task] = {}

def _msg_fingerprint(m: discord.Message) -> str:
    edited = m.edited_at.isoformat() if m.edited_at else ""
    parts = [str(m.id), edited, (m.content or "").strip()]
    # include embed text too (bots often edit embeds)
    if m.embeds:
        for e in m.embeds:
            parts.append((e.title or "").strip())
            parts.append((e.description or "").strip())
            try:
                for f in (e.fields or []):
                    parts.append((f.name or "").strip())
                    parts.append((f.value or "").strip())
            except Exception:
                pass
    return "\n".join(parts)

def _extract_text_blob(m: discord.Message) -> str:
    chunks = []
    if m.content and m.content.strip():
        chunks.append(m.content.strip())
    for e in (m.embeds or []):
        if e.title:
            chunks.append(str(e.title).strip())
        if e.description:
            chunks.append(str(e.description).strip())
        try:
            for f in (e.fields or []):
                if f.name and f.value:
                    chunks.append(f"{str(f.name).strip()}: {str(f.value).strip()}")
        except Exception:
            pass
    return "\n".join(chunks).strip()

@bot.tree.command(name="archieved_text", description="(Owner-only) Continuously repost the latest text/embed from a channel when it changes.")
@app_commands.describe(
    mode="start or stop",
    source_channel="Channel to watch (the edited/bot-updated message lives here).",
    output_channel="Where to post copies (defaults to where you run the command).",
    poll_seconds="How often to check (default 30)."
)
async def archieved_text(
    interaction: discord.Interaction,
    mode: str,
    source_channel: discord.TextChannel | None = None,
    output_channel: discord.TextChannel | None = None,
    poll_seconds: int = 30,
):
    if await _deny_if_not_owner_interaction(interaction):
        return

    guild_id = interaction.guild_id or 0
    user_id = interaction.user.id
    key = (guild_id, user_id)

    mode = (mode or "").lower().strip()
    if mode not in ("start", "stop"):
        await interaction.response.send_message("Mode must be `start` or `stop`.", ephemeral=True)
        return

    if mode == "stop":
        task = _active_text_archivers.pop(key, None)
        if task and not task.done():
            task.cancel()
            await interaction.response.send_message("🛑 /archieved_text stopped.", ephemeral=True)
        else:
            await interaction.response.send_message("No active /archieved_text running.", ephemeral=True)
        return

    if source_channel is None:
        await interaction.response.send_message("❌ Provide `source_channel`.", ephemeral=True)
        return

    out_ch = output_channel or interaction.channel
    if not isinstance(out_ch, discord.TextChannel):
        await interaction.response.send_message("❌ Output must be a normal text channel.", ephemeral=True)
        return

    poll_seconds = max(5, min(300, int(poll_seconds)))

    old = _active_text_archivers.pop(key, None)
    if old and not old.done():
        old.cancel()

    await interaction.response.send_message(
        f"✅ /archieved_text started (owner-only).\n• Watching: {source_channel.mention}\n• Posting to: {out_ch.mention}\n• Poll: {poll_seconds}s",
        ephemeral=True
    )

    async def runner():
        last_fp: str | None = None
        while True:
            try:
                msgs = [m async for m in source_channel.history(limit=1, oldest_first=False)]
                if not msgs:
                    await asyncio.sleep(poll_seconds)
                    continue

                m = msgs[0]
                fp = _msg_fingerprint(m)
                if fp != last_fp:
                    last_fp = fp
                    blob = _extract_text_blob(m)

                    embed = discord.Embed(
                        title="Archived Text Update",
                        description=(blob[:3900] if blob else "*No text content*"),
                    )
                    embed.set_footer(text=f"Source: #{source_channel.name} • msg_id={m.id}" + (" • edited" if m.edited_at else ""))
                    await out_ch.send(embed=embed)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                try:
                    await out_ch.send(f"⚠️ /archieved_text error: `{type(e).__name__}: {e}`")
                except Exception:
                    pass

            await asyncio.sleep(poll_seconds)

    task = asyncio.create_task(runner())
    _active_text_archivers[key] = task

# -------------------- PREFIX COMMAND: !check2009 --------------------
@bot.command(name="check2009")
async def check2009(ctx: commands.Context):
    lines = []
    for g in bot.guilds:
        lines.append(f"- {g.name} | ID: {g.id} | Members: {g.member_count}")

    if not lines:
        await ctx.send("I'm not in any servers.")
        return

    header = f"**Servers I'm in ({len(lines)}):**\n"
    msg = header
    for ln in lines:
        if len(msg) + len(ln) + 1 > 1990:
            await ctx.send(msg)
            msg = ""
        msg += ln + "\n"
    if msg.strip():
        await ctx.send(msg)

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")

    if BOT_OWNER_ID == 0:
        print("⚠️ BOT_OWNER_ID is not set. Owner-only commands (/check, /archieved, /archieved_text) will deny everyone.")

    if SOURCE_CHANNEL_ID == 0:
        print("⚠️ SOURCE_CHANNEL_ID is not set. /progress /live_progress /archieved /canvas will fail until you set it.")
    if TIMELAPSE_CHANNEL_ID == 0:
        print("⚠️ TIMELAPSE_CHANNEL_ID is not set. /timelapse will fail until you set it.")

    bot.run(DISCORD_TOKEN)