# Minimal Discord Slash Command Bot (GLOBAL sync)
# /hello -> Hi
#
# Requirements:
#   pip install -U discord.py
#
# Env vars required:
#   DISCORD_TOKEN = your bot token

import os
import discord
from discord.ext import commands

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    # Global sync (no guild id)
    synced = await bot.tree.sync()
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")
    print(f"✅ Synced {len(synced)} command(s): {[c.name for c in synced]}")

@bot.tree.command(name="hello", description="Say hello")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message("Hi")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)
```0