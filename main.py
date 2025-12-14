# Discord Slash Bot (Pydroid / Railway friendly)
# Commands:
# - /ask  → rule checker with clean formatted output
# - /ping → health check
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

# -------------------- SYSTEM PROMPT --------------------
SYSTEM_PROMPT = r"""
You are a moderation helper for a Roblox r/place-clone game.

You must decide whether the described action is bannable under the rules.
You MUST cite the exact rule(s) that apply.

If multiple rules apply, combine ban lengths.

If you are unsure, mark unsure=true.

Return ONLY valid JSON.
""".strip()

# -------------------- OPENAI --------------------
def call_openai(system_prompt: str, user_prompt: str) -> str:
    if not OPENAI_API_KEY:
        return ""

    payload = {
        "model": MODEL,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.loads(r.read().decode("utf-8"))
        for item in j.get("output", []):
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    return c.get("text", "")
    except Exception:
        return ""

async def call_openai_async(system_prompt: str, user_prompt: str) -> str:
    return await asyncio.to_thread(call_openai, system_prompt, user_prompt)

# -------------------- DISCORD BOT --------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def cooldown_ok(uid: int) -> bool:
    now = time.time()
    last = _last_used.get(uid, 0)
    if now - last < COOLDOWN_S:
        return False
    _last_used[uid] = now
    return True

# -------------------- FORMATTER --------------------
def build_pretty_message(result: dict) -> str:
    divider = "━━━━━━━━━━━━━━━━━━━━"

    if result.get("is_bannable"):
        header = "🚫 BAN RESULT"
        decision = "**Decision:** Bannable"
        ban = f"**Ban Length:** {result.get('ban_length', 'Unknown')}"
    elif result.get("unsure"):
        header = "⚠️ REVIEW NEEDED"
        decision = "**Decision:** Unclear"
        ban = f"**Potential Ban Length:** {result.get('ban_length', 'Unknown')}"
    else:
        header = "✅ RESULT"
        decision = "**Decision:** Not Bannable"
        ban = ""

    lines = [divider, header, divider, "", decision]
    if ban:
        lines.append(ban)

    reasons = result.get("reasons") or []
    if reasons:
        lines.append("")
        lines.append("**Reasons Identified:**")
        for r in reasons:
            lines.append(f"• **{r.get('category','Rule')}**")
            if r.get("rule_text"):
                lines.append(f"  └ {r['rule_text']}")
            if r.get("why"):
                lines.append(f"  └ {r['why']}")

    if not result.get("is_bannable") and not result.get("unsure"):
        lines += ["", "**Explanation:**", "• No rule violations were matched."]

    if result.get("unsure") and result.get("note"):
        lines += ["", f"*Suggestion: {result['note']}*"]

    lines += ["", divider]
    msg = "\n".join(lines)

    return msg[:1900]

# -------------------- JSON SAFETY --------------------
def parse_result(text: str) -> dict:
    try:
        obj = json.loads(text)
        return {
            "is_bannable": bool(obj.get("is_bannable")),
            "unsure": bool(obj.get("unsure")),
            "ban_length": obj.get("ban_length", "Unknown"),
            "reasons": obj.get("reasons", []),
            "note": obj.get("note", ""),
        }
    except Exception:
        return {
            "is_bannable": False,
            "unsure": True,
            "ban_length": "Unknown",
            "reasons": [],
            "note": "Could not confidently determine the outcome.",
        }

# -------------------- EVENTS --------------------
@bot.event
async def on_ready():
    synced = await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")
    print(f"✅ Synced commands: {[c.name for c in synced]}")

# -------------------- COMMANDS --------------------
@bot.tree.command(name="ask", description="Check if something is bannable under the game rules.")
@app_commands.describe(message="Describe what happened.")
async def ask(interaction: discord.Interaction, message: str):
    if not cooldown_ok(interaction.user.id):
        await interaction.response.send_message("⏳ Slow down.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    raw = await call_openai_async(SYSTEM_PROMPT, message)
    result = parse_result(raw)

    await interaction.followup.send(build_pretty_message(result))

@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN")
    bot.run(DISCORD_TOKEN)