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

# ------------------- CONFIG --------------------
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
@bot.tree.command(name="canvas", description="Gets the most recent canvas from pixel place")
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
            content=f" Latest canvas image from {source_channel.mention}:",
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

@bot.tree.command(name="timelapse", description="Creates a timelapse for pixel place")
@app_commands.describe(hours="Hours back (default 12).", fps="FPS (default 4).", max_frames="Max frames (default 60).", max_side="Max side (default 600).")
async def timelapse(interaction: discord.Interaction, hours: int = 12, fps: int = 4, max_frames: int = 60, max_side: int = 600):
    from PIL import Image
    import aiohttp

    await interaction.response.defer(thinking=True)

    try:
        channel = await _get_text_channel_by_id(TIMELAPSE_CHANNEL_ID)
    except Exception as e:
        await interaction.followup.send(f"❌ Timelapse source channel error: `{type(e).__name__}: {e}`")
        return

    hours = max(1, min(24, int(hours)))
    fps = max(1, min(30, int(fps)))
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
        content=f"Timelapse generated from {channel.mention} ({len(pal_frames)} frames, {fps} fps):",
        file=discord.File(fp=out, filename="timelapse.gif")
    )

# -------------------- /PROGRESS (single run, uses owner-set SOURCE_CHANNEL_ID) --------------------
@bot.tree.command(name="progress", description="Template progresser")
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

# ===================== /LIVE_PROGRESS (FULL BLOCK) =====================
# Drops "mode=start/stop". It STARTS when you run /live_progress.
# Stop/Pause/Extract/Change are done via buttons.
#
# Requires from your script:
# - SOURCE_CHANNEL_ID, POLL_SECONDS, COOLDOWN_SECONDS_PER_PIXEL
# - _get_text_channel_by_id, parse_coords_4pairs, run_markarea_once, _eta_from_progress
# - bot, _active_checks dict
#
# Paste this whole block over your existing /live_progress section.

_live_sessions: dict[tuple[int, int], dict] = {}

class BuildersModal(discord.ui.Modal, title="Set builders"):
    builders = discord.ui.TextInput(
        label="Builders",
        placeholder="e.g. 1, 2, 5",
        required=True,
        max_length=6,
    )

    def __init__(self, session_key: tuple[int, int]):
        super().__init__()
        self.session_key = session_key

    async def on_submit(self, interaction: discord.Interaction):
        s = _live_sessions.get(self.session_key)
        if not s:
            await interaction.response.send_message("❌ No active live progress session.", ephemeral=True)
            return

        try:
            v = int(str(self.builders.value).strip())
            if v < 1:
                raise ValueError()
            if v > 500:
                v = 500
        except Exception:
            await interaction.response.send_message("❌ Invalid builders number.", ephemeral=True)
            return

        s["builders"] = v
        await interaction.response.send_message(f"✅ Builders set to **{v}**.", ephemeral=True)


class RoleSelectView(discord.ui.View):
    def __init__(self, session_key: tuple[int, int], *, timeout: float | None = 60):
        super().__init__(timeout=timeout)
        self.session_key = session_key

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Select a role to ping…",
        min_values=0,
        max_values=1,
    )
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        s = _live_sessions.get(self.session_key)
        if not s:
            await interaction.response.send_message("❌ No active session.", ephemeral=True)
            return

        role = select.values[0] if select.values else None
        s["ping_role_id"] = role.id if role else None
        await interaction.response.send_message(
            f"✅ Ping role set to: {role.mention if role else '**None**'}",
            ephemeral=True
        )

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.secondary)
    async def clear_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        s = _live_sessions.get(self.session_key)
        if not s:
            await interaction.response.send_message("❌ No active session.", ephemeral=True)
            return
        s["ping_role_id"] = None
        await interaction.response.send_message("✅ Ping role cleared (None).", ephemeral=True)


class LiveProgressControls(discord.ui.View):
    """
    Main buttons:
      - Extract (sends template as file)
      - Pause/Resume
      - Stop
      - Change  -> submenu: Builders / Template / Role / Back
    """
    def __init__(self, session_key: tuple[int, int], *, timeout: float | None = None):
        super().__init__(timeout=timeout)
        self.session_key = session_key
        self.mode = "main"
        self._sync_buttons()

    def _sync_buttons(self):
        self.clear_items()

        if self.mode == "main":
            self.add_item(self.ExtractButton(self.session_key))
            self.add_item(self.PauseButton(self.session_key))
            self.add_item(self.StopButton(self.session_key))
            self.add_item(self.ChangeButton(self.session_key))
        else:
            self.add_item(self.BuildersButton(self.session_key))
            self.add_item(self.TemplateButton(self.session_key))
            self.add_item(self.RoleButton(self.session_key))
            self.add_item(self.BackButton(self.session_key))

        # keep Pause label correct
        s = _live_sessions.get(self.session_key) or {}
        paused = bool(s.get("paused", False))
        for item in self.children:
            if isinstance(item, LiveProgressControls.PauseButton):
                item.label = "Resume" if paused else "Pause"
                item.style = discord.ButtonStyle.success if paused else discord.ButtonStyle.primary

    # ---------- MAIN ----------
    class ExtractButton(discord.ui.Button):
        def __init__(self, session_key):
            super().__init__(label="Extract", style=discord.ButtonStyle.secondary)
            self.session_key = session_key

        async def callback(self, interaction: discord.Interaction):
            s = _live_sessions.get(self.session_key)
            if not s:
                await interaction.response.send_message("❌ No active live progress session.", ephemeral=True)
                return

            fp = BytesIO(s["template_bytes"])
            await interaction.response.send_message(
                content="📌 Template used for this live progress:",
                file=discord.File(fp=fp, filename=s.get("template_filename", "template.png")),
                ephemeral=True
            )

    class PauseButton(discord.ui.Button):
        def __init__(self, session_key):
            super().__init__(label="Pause", style=discord.ButtonStyle.primary)
            self.session_key = session_key

        async def callback(self, interaction: discord.Interaction):
            s = _live_sessions.get(self.session_key)
            if not s:
                await interaction.response.send_message("❌ No active live progress session.", ephemeral=True)
                return

            s["paused"] = not bool(s.get("paused", False))
            paused = bool(s["paused"])

            view: LiveProgressControls = self.view  # type: ignore
            # update pause button label/style
            for item in view.children:
                if isinstance(item, LiveProgressControls.PauseButton):
                    item.label = "Resume" if paused else "Pause"
                    item.style = discord.ButtonStyle.success if paused else discord.ButtonStyle.primary
                    break

            await interaction.response.edit_message(view=view)

    class StopButton(discord.ui.Button):
        def __init__(self, session_key):
            super().__init__(label="Stop", style=discord.ButtonStyle.danger)
            self.session_key = session_key

        async def callback(self, interaction: discord.Interaction):
            # cancel task
            task = _active_checks.pop(self.session_key, None)
            s = _live_sessions.pop(self.session_key, None)

            if task and not task.done():
                task.cancel()

            # try delete last status message
            if s and s.get("last_status_msg_id"):
                try:
                    ch = interaction.channel
                    if isinstance(ch, discord.TextChannel):
                        msg = await ch.fetch_message(int(s["last_status_msg_id"]))
                        await msg.delete()
                except Exception:
                    pass

            # disable buttons
            view: LiveProgressControls = self.view  # type: ignore
            for child in view.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True

            await interaction.response.edit_message(content="🛑 Live progress stopped.", view=view)

    class ChangeButton(discord.ui.Button):
        def __init__(self, session_key):
            super().__init__(label="Change", style=discord.ButtonStyle.secondary)
            self.session_key = session_key

        async def callback(self, interaction: discord.Interaction):
            view: LiveProgressControls = self.view  # type: ignore
            view.mode = "change"
            view._sync_buttons()
            await interaction.response.edit_message(view=view)

    # ---------- CHANGE MENU ----------
    class BuildersButton(discord.ui.Button):
        def __init__(self, session_key):
            super().__init__(label="Builders", style=discord.ButtonStyle.primary)
            self.session_key = session_key

        async def callback(self, interaction: discord.Interaction):
            if self.session_key not in _live_sessions:
                await interaction.response.send_message("❌ No active session.", ephemeral=True)
                return
            await interaction.response.send_modal(BuildersModal(self.session_key))

    class TemplateButton(discord.ui.Button):
        def __init__(self, session_key):
            super().__init__(label="Template", style=discord.ButtonStyle.primary)
            self.session_key = session_key

        async def callback(self, interaction: discord.Interaction):
            s = _live_sessions.get(self.session_key)
            if not s:
                await interaction.response.send_message("❌ No active session.", ephemeral=True)
                return

            await interaction.response.send_message(
                "📎 Upload the **new template image** as your next message in this channel (within 60s).",
                ephemeral=True
            )

            channel = interaction.channel
            if channel is None:
                return

            def check(m: discord.Message) -> bool:
                if m.author.id != interaction.user.id:
                    return False
                if m.channel.id != channel.id:
                    return False
                if not m.attachments:
                    return False
                a0 = m.attachments[0]
                return (a0.content_type or "").startswith("image/")

            try:
                msg = await bot.wait_for("message", timeout=60.0, check=check)
            except asyncio.TimeoutError:
                try:
                    await interaction.followup.send("⏳ Timed out. Try again and upload within 60 seconds.", ephemeral=True)
                except Exception:
                    pass
                return

            a = msg.attachments[0]
            try:
                new_bytes = await a.read()
            except Exception as e:
                try:
                    await interaction.followup.send(f"❌ Failed to read attachment: `{e}`", ephemeral=True)
                except Exception:
                    pass
                return

            s["template_bytes"] = new_bytes
            s["template_filename"] = a.filename or "template.png"

            try:
                await interaction.followup.send("✅ Template updated.", ephemeral=True)
            except Exception:
                pass

    class RoleButton(discord.ui.Button):
        def __init__(self, session_key):
            super().__init__(label="Role", style=discord.ButtonStyle.primary)
            self.session_key = session_key

        async def callback(self, interaction: discord.Interaction):
            s = _live_sessions.get(self.session_key)
            if not s:
                await interaction.response.send_message("❌ No active session.", ephemeral=True)
                return

            if interaction.guild is None:
                await interaction.response.send_message("❌ Roles can only be set in a server.", ephemeral=True)
                return

            await interaction.response.send_message(
                "Choose a ping role (or Clear):",
                view=RoleSelectView(self.session_key, timeout=60),
                ephemeral=True
            )

    class BackButton(discord.ui.Button):
        def __init__(self, session_key):
            super().__init__(label="Back", style=discord.ButtonStyle.secondary)
            self.session_key = session_key

        async def callback(self, interaction: discord.Interaction):
            view: LiveProgressControls = self.view  # type: ignore
            view.mode = "main"
            view._sync_buttons()
            await interaction.response.edit_message(view=view)


# One active live_progress per user per guild
_active_checks: dict[tuple[int, int], asyncio.Task] = {}

@bot.tree.command(name="live_progress", description="Live template progress with buttons (pause/stop/change).")
@app_commands.describe(
    template="Template image attachment (required).",
    coords="(x1,y1)(x2,y2)(x3,y3)(x4,y4) (required).",
    builders="How many people placing pixels in parallel (default 1).",
    ping_role="Role to ping if attacks are detected (optional)."
)
async def live_progress(
    interaction: discord.Interaction,
    template: discord.Attachment,
    coords: str,
    builders: int = 1,
    ping_role: discord.Role | None = None,
):
    # Basic channel validation
    out_ch = interaction.channel
    if not isinstance(out_ch, discord.TextChannel):
        await interaction.response.send_message("This command must be used in a normal text channel.", ephemeral=True)
        return

    # Validate coords and template
    if not (template.content_type or "").startswith("image/"):
        await interaction.response.send_message("❌ That template doesn’t look like an image.", ephemeral=True)
        return

    try:
        parse_coords_4pairs(coords)
    except Exception as e:
        await interaction.response.send_message(f"❌ Invalid coords: {e}", ephemeral=True)
        return

    try:
        source_channel = await _get_text_channel_by_id(SOURCE_CHANNEL_ID)
    except Exception as e:
        await interaction.response.send_message(f"❌ Source channel error: `{type(e).__name__}: {e}`", ephemeral=True)
        return

    builders = max(1, int(builders))
    template_bytes = await template.read()

    guild_id = interaction.guild_id or 0
    user_id = interaction.user.id
    key = (guild_id, user_id)

    # stop old session if exists
    old = _active_checks.pop(key, None)
    if old and not old.done():
        old.cancel()
    _live_sessions.pop(key, None)

    # create session state
    _live_sessions[key] = {
        "paused": False,
        "template_bytes": template_bytes,
        "template_filename": template.filename or "template.png",
        "builders": builders,
        "ping_role_id": ping_role.id if ping_role else None,
        "coords": coords,
        "last_status_msg_id": None,
        "output_channel_id": out_ch.id,
    }

    # Send control message (buttons live here)
    view = LiveProgressControls(key, timeout=None)
    await interaction.response.send_message(
        content=(
            f"✅ **Live progress started**\n"
            f"• Source: {source_channel.mention}\n"
            f"• Poll: every **{POLL_SECONDS}s**\n"
            f"Use the buttons below to pause/stop/change."
        ),
        view=view,
        ephemeral=False
    )

    async def runner():
        last_matched: int | None = None

        while True:
            s = _live_sessions.get(key)
            if not s:
                return

            try:
                if s.get("paused"):
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                # ALWAYS run + post every poll (even if source image didn’t change)
                # (This is what you asked for.)
                template_bytes_local: bytes = s["template_bytes"]
                builders_local: int = max(1, int(s.get("builders") or 1))
                coords_local: str = str(s.get("coords") or coords)

                ping_role_local = None
                prid = s.get("ping_role_id")
                if prid and interaction.guild:
                    try:
                        ping_role_local = interaction.guild.get_role(int(prid))
                    except Exception:
                        ping_role_local = None

                png_bytes, box_w, box_h, matched, total, pct = await run_markarea_once(
                    source_channel=source_channel,
                    template_bytes=template_bytes_local,
                    coords=coords_local,
                )

                # regression ping
                if last_matched is not None and matched < last_matched and ping_role_local is not None:
                    lost = last_matched - matched
                    dec_pct = (lost / last_matched * 100.0) if last_matched > 0 else 0.0
                    await out_ch.send(
                        f"{ping_role_local.mention} ⚠️ **Users may be attacking** — progress went backwards "
                        f"(**-{lost:,} px**, **-{dec_pct:.2f}%**)."
                    )

                # progress info (no ping)
                if last_matched is not None and matched > last_matched:
                    gained = matched - last_matched
                    inc_pct = (gained / total * 100.0) if total > 0 else 0.0
                    await out_ch.send(f"✅ **Progress made**: **+{gained:,} px** (**+{inc_pct:.2f}%** of template).")

                last_matched = matched

                remaining, _eta_seconds, h, m, sss = _eta_from_progress(matched, total, builders_local)

                embed = discord.Embed(
                    title="Live Template Progress",
                    description=(
                        f"**Source**: {source_channel.mention}\n"
                        f"**Region**: `{box_w}×{box_h}`\n"
                        f"**Pixels**: `{matched:,} / {total:,}`\n"
                        f"**Completion**: **{pct:.2f}%**\n"
                        f"**ETA**: **{h}h {m}m {sss}s** (`{remaining:,}` px, builders={builders_local}, {COOLDOWN_SECONDS_PER_PIXEL}s/px)\n"
                        f"**Update**: periodic refresh (every {POLL_SECONDS}s)."
                    ),
                )

                fp = BytesIO(png_bytes)
                file = discord.File(fp=fp, filename="template_progress.png")
                embed.set_image(url="attachment://template_progress.png")

                new_msg = await out_ch.send(embed=embed, file=file)

                # delete previous status message (keeps channel clean)
                prev_id = _live_sessions.get(key, {}).get("last_status_msg_id")
                if prev_id:
                    try:
                        prev = await out_ch.fetch_message(int(prev_id))
                        await prev.delete()
                    except Exception:
                        pass

                # save latest status msg id
                if key in _live_sessions:
                    _live_sessions[key]["last_status_msg_id"] = new_msg.id

            except asyncio.CancelledError:
                # cleanup last status msg
                try:
                    sid = _live_sessions.get(key, {}).get("last_status_msg_id")
                    if sid:
                        msg = await out_ch.fetch_message(int(sid))
                        await msg.delete()
                except Exception:
                    pass
                raise
            except Exception as e:
                try:
                    await out_ch.send(f"⚠️ /live_progress error: `{type(e).__name__}: {e}`")
                except Exception:
                    pass

            await asyncio.sleep(POLL_SECONDS)

    task = asyncio.create_task(runner())
    _active_checks[key] = task
# ===================== END /LIVE_PROGRESS BLOCK =====================

# -------------------- /ARCHIEVED (OWNER-ONLY, LIVE IMAGE ARCHIVER, uses SOURCE_CHANNEL_ID) --------------------
_active_archives: dict[tuple[int, int], asyncio.Task] = {}

@bot.tree.command(name="archieved", description="(Owner-only)")
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