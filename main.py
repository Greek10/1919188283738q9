# Discord Slash Bot (Pydroid-friendly)
# - /ask -> OpenAI rule helper (Embed output)
# - /stopmotion -> makes a stop-motion GIF from images in channel history
# - /markarea -> template progresser
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

# -------------------- SYSTEM PROMPT (UNCHANGED) --------------------
SYSTEM_PROMPT = r"""
[UNCHANGED – KEEP EXACTLY AS YOU PROVIDED]
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

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps({
            "model": MODEL,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return _extract_text_from_responses_api(json.loads(r.read().decode()))
    except Exception:
        return ""

async def call_openai_async(system_prompt, user_prompt):
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
    embed.add_field(
        name="Rule",
        value=res.get("rule") or "No exact rule provided.",
        inline=False,
    )
    if res.get("unsure") and res.get("suggestion"):
        embed.set_footer(text=res["suggestion"])
    return embed

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")

# -------------------- /ASK --------------------
@bot.tree.command(name="ask", description="Check if something is bannable.")
async def ask(interaction: discord.Interaction, message: str):
    if not cooldown_ok(interaction.user.id):
        await interaction.response.send_message("⏳ Cooldown active.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    raw = await call_openai_async(SYSTEM_PROMPT, message)
    await interaction.followup.send(embed=build_embed(safe_parse(raw)))

# -------------------- /MARKAREA --------------------
@bot.tree.command(
    name="markarea",
    description="Compare canvas vs template region and show progress."
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
    from PIL import Image
    await interaction.response.defer(thinking=True)

    # (logic unchanged – omitted here for clarity, keep your existing logic)

    # --- AFTER preview image is created ---
    await interaction.followup.send(
        content=(
            f"🧩 **Template Progress**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📐 **Region Size**\n"
            f"`{box_w} × {box_h}`\n\n"
            f"🟢 **Matched Pixels**\n"
            f"`{matched:,} / {total:,}`\n\n"
            f"📊 **Completion**\n"
            f"**{pct:.2f}%**"
        ),
        file=discord.File(fp=out_preview, filename="template_progress.png")
    )

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)