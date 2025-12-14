# Discord Slash Bot (Pydroid-friendly)
# - Hardcoded pre-prompt (characteristics) in code
# - /ask checks if something is bannable under your rules
# - Output: clean Discord markdown (NO embeds, NO "Report" field)
#
# Requirements:
#   pip install -U discord.py
#
# Env vars:
#   DISCORD_TOKEN
#   OPENAI_API_KEY

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
MAX_OUTPUT_TOKENS = 650

COOLDOWN_S = 6
_last_used = {}

SHOW_DEBUG_PREVIEW = False

# ✅ SYSTEM PROMPT (forces exact rule matching + multi-reason ban-length)
SYSTEM_PROMPT = r"""
You are a moderation helper for a Roblox r/place-clone game.
You must follow Roblox TOS and the in-game rules below.

# In-game Rules

Permanent Ban:
- Leaking personally identifiable information ("doxxing")
- Deploying bot accounts
- Detailed/major not-safe-for-work drawings
- Harassment occurring for more than a month
- Repeated offences
- Extremism (such as promoting, being involved in, wearing the outfits of and/or supporting groups like Al-Qaeda, the Ku Klux Klan, Nazism etc or otherwise encouraging violence)
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
- Minor not-safe-for-work drawing
- Using more than 1 account at a time
- Inappropriate display usernames (swears)
- Large female genitalia depictions
- Large male genitalia depictions or ones that are explicit for other reasons (shown as going into a character's mouth)
- Breaking Roblox maturity rating (romantic themes, gambling, alcohol, drugs)
- Impersonating other players

Three-Day Ban:
- Male genitalia depictions (upside down "T")
- Female genitalia depictions
- Exploits (if not caught by anti-cheat)

One-Day Ban:
- Bypassing swear words

Warnings, Kicks, Under One-Day Ban:
- "W/Sing" or giving "backshots" (chronologically warning, kick, under 1 hour ban)
- Avatars impacting other player experiences (large avatars)

Other:
- Framing users - same duration as what they tried to frame the player for
- Lying on a ban appeal - double the length they were originally banned for
- Coordinated account usage by one person (saving pixels on multiple accounts then using them one by one, in turn starting off with more than 20 pixels) - One-week ban per extra account used
- Abusing in-game mechanics to gain an unfair advantage over others - moderator decides punishment duration
- Conspiring to break the rules - Half or full duration of what they would have done

What is NOT bannable:
- Chat (unless it bypasses chat filter)
- Griefing

# Task
Given the user's message, decide if it breaks the rules.

CRITICAL REQUIREMENTS:
1) You MUST locate the exact reason(s) by referencing the closest matching bullet(s) from the rules above.
2) If there are multiple reasons, you MUST include ALL of them in the ban length output (combine categories).
   Example ban_length: "One-Month + One-Week" or "Permanent + One-Month".
3) If you cannot confirm entirely that it is bannable, you must mark unsure=true and recommend contacting a moderator / opening a report ticket.
4) If you are sure, do NOT include that recommendation.

# Output format (STRICT)
Return ONLY valid JSON (no markdown, no extra text) exactly matching this schema:

{
  "is_bannable": true/false,
  "unsure": true/false,
  "ban_length": "string",          // combined if multiple reasons, e.g. "One-Month + One-Week"
  "title": "string",               // short headline
  "reasons": [
    {
      "category": "string",        // e.g. "One-Week Ban"
      "rule_text": "string",       // exact or near-exact bullet text from rules
      "why": "string"              // short explanation
    }
  ],
  "note": "string"                 // empty if sure; otherwise your mod-contact suggestion
}

Rules for content:
- reasons MUST NOT be empty if is_bannable=true.
- If is_bannable=false and unsure=false, you can set reasons=[].
- Keep everything concise.
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

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {OPENAI_API_KEY}")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
        j = json.loads(raw)
        return _extract_text_from_responses_api(j) or ""
    except Exception:
        return ""

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
    Parse the model JSON. If it fails, fallback to an 'unsure' response.
    """
    text = (text or "").strip()
    try:
        obj = json.loads(text)
        # Normalize fields
        is_bannable = bool(obj.get("is_bannable", False))
        unsure = bool(obj.get("unsure", False))
        ban_length = str(obj.get("ban_length", "Unknown")).strip() or "Unknown"
        title = str(obj.get("title", "Decision")).strip() or "Decision"
        reasons = obj.get("reasons", [])
        if not isinstance(reasons, list):
            reasons = []

        cleaned_reasons = []
        for r in reasons:
            if not isinstance(r, dict):
                continue
            cleaned_reasons.append({
                "category": str(r.get("category", "")).strip(),
                "rule_text": str(r.get("rule_text", "")).strip(),
                "why": str(r.get("why", "")).strip(),
            })

        note = str(obj.get("note", "")).strip()

        # If bannable but no reasons, treat as unsure (so we don't mislead)
        if is_bannable and len(cleaned_reasons) == 0:
            return {
                "is_bannable": False,
                "unsure": True,
                "ban_length": "Unknown",
                "title": "Unsure",
                "reasons": [],
                "note": "I couldn’t match an exact rule confidently. Please contact a moderator / open a report ticket.",
            }

        # If unsure, ensure note exists
        if unsure and not note:
            note = "I’m not fully sure. Please contact a moderator / open a report ticket."

        return {
            "is_bannable": is_bannable,
            "unsure": unsure,
            "ban_length": ban_length,
            "title": title,
            "reasons": cleaned_reasons,
            "note": note,
        }
    except Exception:
        return {
            "is_bannable": False,
            "unsure": True,
            "ban_length": "Unknown",
            "title": "Unsure",
            "reasons": [],
            "note": "I couldn’t parse the response reliably. Please contact a moderator / open a report ticket.",
        }

def build_pretty_message(result: dict) -> str:
    """
    Formats output as Discord markdown, no embed, no report.
    """
    status = "✅ Bannable" if result["is_bannable"] else ("⚠️ Unsure" if result["unsure"] else "❌ Not bannable")

    # Top line: **Title**: Ban length
    top = f"**{result['title']}**: {result['ban_length']}  \n{status}"

    parts = [top]

    # Reasons list (exact rule text)
    if result["reasons"]:
        parts.append("\n**Exact rule match(es):**")
        for idx, r in enumerate(result["reasons"], start=1):
            cat = r["category"] or "Rule"
            rule_text = r["rule_text"] or "(no rule text provided)"
            why = r["why"] or ""
            # Make rule text pop but keep it readable
            line = f"{idx}. **{cat}** — `{rule_text}`"
            if why:
                line += f"\n   {why}"
            parts.append(line)

    # Small footer-like note (Discord doesn't have true small text; we use italic + parentheses)
    if result["unsure"] and result["note"]:
        parts.append(f"\n*(Suggestion: {result['note']})*")

    msg = "\n".join(parts).strip()

    # Safety: Discord message limit 2000 chars
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(trimmed)"
    return msg

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
    description="Checks if something is bannable under the game rules (not official)."
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

    # If OpenAI failed, do an unsure fallback
    if not raw:
        await interaction.followup.send(
            "**Unsure**: Unknown\n⚠️ Unsure\n\n*(Suggestion: I couldn’t reach the checker. Please contact a moderator / open a report ticket.)*"
        )
        return

    result = safe_parse_model_json(raw)
    pretty = build_pretty_message(result)
    await interaction.followup.send(pretty)

@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)