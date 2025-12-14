# Discord Slash Bot (Pydroid-friendly)
# - Hardcoded pre-prompt (characteristics) in code
# - /ask sends user message to ChatGPT with that system prompt
# - Optional: shows a debug “what was sent” preview (ephemeral) toggle
#
# Requirements:
#   pip install -U discord.py
#
# Env vars you MUST set:
#   DISCORD_TOKEN   = your Discord bot token
#   OPENAI_API_KEY  = your OpenAI API key
#
# Notes:
# - Pydroid/mobile bots won’t stay online if Android kills the app.
# - Keep keys private. Don’t paste tokens in code if you’ll share it.

import os
import time
import json
import asyncio
import urllib.request
import urllib.error

import discord
from discord import app_commands
from discord.ext import commands

# -------------------- CONFIG --------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# ✅ SET YOUR CHARACTERISTICS / PRE-PROMPT HERE (hardcoded)
SYSTEM_PROMPT = """
You are NeonBot — a sharp, friendly assistant inside a Discord server.

Behavior:
- Be helpful and accurate.
- If unsure, say you’re unsure and ask a short clarifying question.
- Keep answers concise by default, but expand if the user asks.

Style:
- Slightly playful, not cringe.
- Prefer bullet points for steps or lists.

Safety:
- Refuse illegal, harmful, or disallowed requests.
- Offer safe alternatives when refusing.
""".strip()

# OpenAI model (adjust if you want)
MODEL = "gpt-4.1-mini"
MAX_OUTPUT_TOKENS = 600

# Basic per-user cooldown (seconds)
COOLDOWN_S = 6
_last_used = {}  # user_id -> last timestamp

# Optional: show a debug preview of what gets sent (ephemeral)
SHOW_DEBUG_PREVIEW = False

# -------------------- OPENAI (Responses API) --------------------
def _extract_text_from_responses_api(json_obj: dict) -> str:
    """
    Extract output text from OpenAI Responses API response.
    """
    out_text = []
    for item in json_obj.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text" and "text" in c:
                out_text.append(c["text"])
    return ("\n".join(out_text)).strip()

def call_openai(system_prompt: str, user_prompt: str) -> str:
    """
    Calls OpenAI Responses API via HTTPS (no SDK dependency).
    """
    if not OPENAI_API_KEY:
        return "Missing OPENAI_API_KEY env var."

    url = "https://api.openai.com/v1/responses"
    payload = {
        "model": MODEL,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {OPENAI_API_KEY}")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        j = json.loads(raw)
        text = _extract_text_from_responses_api(j)
        return text or "No output text returned."
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return f"OpenAI HTTP error {e.code}: {body[:800]}"
    except Exception as e:
        return f"OpenAI request failed: {e}"

async def call_openai_async(system_prompt: str, user_prompt: str) -> str:
    # Run the blocking HTTPS call in a thread so Discord stays responsive
    return await asyncio.to_thread(call_openai, system_prompt, user_prompt)

# -------------------- DISCORD BOT --------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def cooldown_ok(user_id: int) -> bool:
    now = time.time()
    last = _last_used.get(user_id, 0)
    if now - last < COOLDOWN_S:
        return False
    _last_used[user_id] = now
    return True

def chunk_for_discord(text: str, limit: int = 1900) -> list[str]:
    """
    Split long text into Discord-safe chunks.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else ["(empty response)"]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        chunks.append(text[start:end])
        start = end
    return chunks

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        print("⚠️ Slash sync error:", e)
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")

@bot.tree.command(name="ask", description="Ask ChatGPT (uses the built-in characteristics pre-prompt).")
@app_commands.describe(message="What you want to ask the assistant.")
async def ask(interaction: discord.Interaction, message: str):
    if not cooldown_ok(interaction.user.id):
        await interaction.response.send_message(
            f"⏳ Slow down — try again in {COOLDOWN_S}s.",
            ephemeral=True
        )
        return

    user_prompt = (message or "").strip()
    if not user_prompt:
        await interaction.response.send_message("Type a message to ask.", ephemeral=True)
        return

    # Defer to avoid Discord 3-second timeout while we call OpenAI
    await interaction.response.defer(thinking=True)

    if SHOW_DEBUG_PREVIEW:
        preview = f"SYSTEM (characteristics):\n{SYSTEM_PROMPT}\n\nUSER:\n{user_prompt}"
        # Send ephemeral debug preview (won't spam the channel)
        for part in chunk_for_discord(preview, limit=1800):
            await interaction.followup.send(f"```text\n{part}\n```", ephemeral=True)

    reply = await call_openai_async(SYSTEM_PROMPT, user_prompt)

    # Send reply in chunks if needed
    for i, part in enumerate(chunk_for_discord(reply, limit=1900)):
        await interaction.followup.send(part)

@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    # OPENAI_API_KEY can be missing if you just want /ping to work.
    bot.run(DISCORD_TOKEN)