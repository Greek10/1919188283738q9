# Discord Slash Bot: Image -> size -> build-time (15s per pixel)
# - User uploads an image with the command
# - Bot reads the image size (width x height)
# - Pixels = width * height
# - Time = pixels * 15 seconds  (stack cap doesn't change total time)
#
# Requirements:
#   pip install -U discord.py pillow
#
# Env vars:
#   DISCORD_TOKEN = your bot token

import os
import io
import math
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

COOLDOWN_SECONDS_PER_PIXEL = 15

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def seconds_to_hms(total_seconds: int) -> tuple[int, int, int]:
    h = total_seconds // 3600
    total_seconds -= h * 3600
    m = total_seconds // 60
    s = total_seconds - m * 60
    return h, m, s

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        print("⚠️ Slash sync error:", e)
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")

@bot.tree.command(
    name="timefromimage",
    description="Upload an image: bot reads its size and calculates build time (15s per pixel)."
)
@app_commands.describe(
    players="Optional: number of players building in parallel (each generates 1 pixel per 15s). Default 1."
)
async def timefromimage(interaction: discord.Interaction, players: int = 1):
    # Image must be attached to the command message
    if not interaction.attachments:
        await interaction.response.send_message(
            "Attach an image when using `/timefromimage`.",
            ephemeral=True
        )
        return

    if players < 1:
        players = 1
    if players > 1000:
        players = 1000  # sanity cap

    att = interaction.attachments[0]
    if not (att.content_type or "").startswith("image/"):
        await interaction.response.send_message(
            "That attachment doesn’t look like an image.",
            ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)

    # Read bytes and open with Pillow
    data = await att.read()
    try:
        img = Image.open(io.BytesIO(data))
        width, height = img.size
    except Exception as e:
        await interaction.followup.send(f"Couldn’t read that image: {e}")
        return

    total_pixels = width * height

    # Your confirmed equation: 1 pixel takes 15 seconds to generate.
    # With multiple players, divide pixel work by players (parallel generation):
    ticks_needed = math.ceil(total_pixels / players)
    total_seconds = ticks_needed * COOLDOWN_SECONDS_PER_PIXEL

    h, m, s = seconds_to_hms(total_seconds)

    await interaction.followup.send(
        f"🖼️ **Image size:** {width}×{height}\n"
        f"🔢 **Total pixels:** {total_pixels:,}\n"
        f"👥 **Players:** {players}\n"
        f"⏱️ **Time:** ceil({total_pixels:,} / {players}) × {COOLDOWN_SECONDS_PER_PIXEL}s\n"
        f"= **{total_seconds:,} seconds** = **{h}h {m}m {s}s**"
    )

@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)