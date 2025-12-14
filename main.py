# Discord Slash Bot (Pydroid-friendly)
# - Hardcoded pre-prompt (characteristics) in code
# - /ask sends user message to ChatGPT with that system prompt
# - Bot formats response into a clean Discord Embed
#
# Requirements:
#   pip install -U discord.py
#
# Env vars you MUST set:
#   DISCORD_TOKEN   = your Discord bot token
#   OPENAI_API_KEY  = your OpenAI API key

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

MODEL = "gpt-4.1-mini"
MAX_OUTPUT_TOKENS = 450

COOLDOWN_S = 6
_last_used = {}

SHOW_DEBUG_PREVIEW = False

# ✅ SYSTEM PROMPT (formatted + forces JSON output)
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

# Task
Given the user's report, decide if it breaks the rules.

If you are NOT completely sure it is bannable, you MUST recommend contacting a moderator / opening a report ticket.
If you ARE sure, do NOT include that recommendation.

# Output format (STRICT)
Return ONLY valid JSON (no markdown, no extra text) in exactly this schema:

{
  "ban_title": "string",            // e.g. "One-Week Ban" or "Not bannable"
  "ban_length": "string",           // e.g. "7 days", "Permanent", "None"
  "description": "string",          // explain which rule(s) apply and why
  "is_bannable": true/false,
  "unsure": true/false,
  "suggestion": "string"            // empty if not unsure; otherwise tell them to contact a mod / open a ticket
}

Keep description concise and informative.
""".strip()


# -------------------- OPENAI (Responses API) --------------------
def _extract_text_from_responses_api(json_obj: dict) -> str:
    out_text = []
    for item in json_obj.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text" and "text" in c:
                out_text.append(c["text"])
    return ("\n".join(out_text)).strip()

def call_openai(system_prompt: str, user_prompt: str) -> str:
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
        return text or ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return f'{{"ban_title":"Error","ban_length":"None","description":"OpenAI HTTP error {e.code}: {body[:400]}","is_bannable":false,"unsure":true,"suggestion":"Try again or contact a moderator."}}'
    except Exception as e:
        return f'{{"ban_title":"Error","ban_length":"None","description":"OpenAI request failed: {e}","is_bannable":false,"unsure":true,"suggestion":"Try again or contact a moderator."}}'

async def call_openai_async(system_prompt: str, user_prompt: str) -> str:
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

def safe_parse_model_json(text: str) -> dict:
    """
    Model should return JSON only. This function tries to parse it safely.
    If parsing fails, returns a fallback structure and includes raw text.
    """
    text = (text or "").strip()
    try:
        obj = json.loads(text)
        # Minimal schema defaults
        return {
            "ban_title": str(obj.get("ban_title", "Unknown")),
            "ban_length": str(obj.get("ban_length", "Unknown")),
            "description": str(obj.get("description", "")).strip(),
            "is_bannable": bool(obj.get("is_bannable", False)),
            "unsure": bool(obj.get("unsure", False)),
            "suggestion": str(obj.get("suggestion", "")).strip(),
        }
    except Exception:
        return {
            "ban_title": "Unparsed response",
            "ban_length": "Unknown",
            "description": f"Could not parse model output.\nRaw:\n{text[:1500]}",
            "is_bannable": False,
            "unsure": True,
            "suggestion": "I couldn’t confidently parse this. Please contact a moderator / open a report ticket.",
        }

def build_embed(result: dict, user_prompt: str) -> discord.Embed:
    title = f"{result['ban_title']} — {result['ban_length']}"
    desc = result["description"] or "No description provided."

    embed = discord.Embed(
        title=title,
        description=desc[:4096],
    )

    # Add the user's report as a field (trim to fit)
    report_text = user_prompt.strip()
    if len(report_text) > 900:
        report_text = report_text[:900] + "…"
    embed.add_field(name="Report", value=report_text or "(empty)", inline=False)

    # Footer used as “small text”
    if result.get("unsure") and result.get("suggestion"):
        embed.set_footer(text=result["suggestion"][:2048])

    return embed

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        print("⚠️ Slash sync error:", e)
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")

@bot.tree.command(
    name="ask",
    description="Check if something is bannable under the game rules (not official)."
)
@app_commands.describe(message="Describe what happened / what was drawn / what was said.")
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

    await interaction.response.defer(thinking=True)

    if SHOW_DEBUG_PREVIEW:
        preview = f"SYSTEM:\n{SYSTEM_PROMPT}\n\nUSER:\n{user_prompt}"
        await interaction.followup.send(f"```text\n{preview[:1800]}\n```", ephemeral=True)

    raw = await call_openai_async(SYSTEM_PROMPT, user_prompt)
    result = safe_parse_model_json(raw)
    embed = build_embed(result, user_prompt)

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)