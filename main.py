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
these are the rules to a Roblox game based on a r/place clone.
it follows Roblox TOS and the rules below 
# In-game Rules

**Permanent Ban: **
Leaking personally identifiable information ("doxxing").
Deploying bot accounts.
Detailed/major not-safe-for-work drawings
Harassment occurring for more than a month.
Repeated offences.
Extremism (such as promoting, being involved in, wearing the outfits of and/or supporting groups like Al-Qaeda, the Ku Klux Klan, Nazism etc or otherwise encouraging violence)
ban evasion
Pedophilia/Zoophilia

**One-Month Ban:**
Slurs (canvas or chat).
Swastikas and other offensive symbols.
Using a macro.
Sexual in-game talk.
Very inappropriate display usernames (slurs).
Links or QR codes
Heavy/realistic gore

**One-Week Ban:**
Minor not-safe-for-work drawing.
Using more than 1 account at a time.
Inappropriate display usernames (swears).
Large female genitalia depictions.
Large male genitalia depictions or ones that are explicit for other reasons (shown as going into a character's mouth).
Breaking Roblox maturity rating (romantic themes, gambling, alcohol, drugs). 
Impersonating other players


**Three-Day Ban:**
Male genitalia depictions (upside down "T").
Female genitalia depictions.
Exploits (if not caught by anti-cheat).

**One-Day Ban:**
Bypassing swear words.

**Warnings, Kicks, Under One-Day Ban:**
"W/Sing" or giving "backshots" (chronologically warning, kick, under 1 hour ban).
Avatars impacting other player experiences (large avatars)

**Other**
Framing users - same duration as what they tried to frame the player for
Lying on a ban appeal - double the length they were originally banned for
Coordinated account usage by one person (saving pixels on multiple accounts then using them one by one, in turn starting off with more than 20 pixels) - One-week ban per extra account used. - one week ban
Abusing in-game mechanics to gain an unfair advantage over others - moderator decides punishment duration
Conspiring to break the rules -Half or full duration of what they would have done.
** what is not bannable **
chat (unless it bypasses chat)
griefing 

you are meant to read the message below and respond if that is breaking the rules. If you can not confirm entirely that something is bannable then suggest the user to contact a mod or open a report ticket, if you can figure out if it's bannable then skip that part but if you are unsure then add it.
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

@bot.tree.command(name="ask", description="Tells whether something is bannable or not - do not use this to confirm anything..")
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