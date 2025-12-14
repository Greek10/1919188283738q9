# Discord Slash Bot (Pydroid-friendly)
# - Hardcoded pre-prompt (characteristics) in code
# - /ask sends user message to ChatGPT with that system prompt
# - Bot formats response into a clean Discord Embed
# - NEW:
#   /add       -> upload a template image + title + author (saved to templates.json)
#   /templates -> browse templates in a GRID-style embed page with Prev/Next buttons
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
from typing import Optional, List, Dict

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

# -------------------- TEMPLATE STORAGE --------------------
TEMPLATES_FILE = "templates.json"
_templates_lock = asyncio.Lock()
TEMPLATES_PER_PAGE = 6  # grid size per page (2 columns-ish due to inline fields)

def _now_unix() -> int:
    return int(time.time())

def clamp_text(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"

async def load_templates() -> List[Dict]:
    async with _templates_lock:
        try:
            if not os.path.exists(TEMPLATES_FILE):
                return []
            with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

async def save_templates(items: List[Dict]) -> None:
    async with _templates_lock:
        with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

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

remember you MAY look into the actual roblox rules and see if the thing being described is against the rules. you MAY tell the user that and see what they think about it.

# Task
Given the user's report, decide if it breaks the rules.

If you are NOT completely sure it is bannable, you MUST recommend contacting a moderator / opening a report ticket.
If you ARE sure, do NOT include that recommendation.

you MUST add onto the ban length if multiple bannable things are being mentioned. assume 1 month is 31 days.
an example of this would be: a user mentions that a penis and swastika is being drawn, you would say 34 days (3 for pp, 31 for swastika)

# Output format (STRICT)
Return ONLY valid JSON (no markdown, no extra text) in exactly this schema:

{
  "ban_title": "string",            // e.g. "One-Week Ban" or "Not bannable"
  "ban_length": "string",           // e.g. "7 days", "Permanent", "None"
  "description": "string",          // explain which rule(s) apply and why
  "is_bannable": true/false,
  "unsure": true/false,
  "suggestion": "string",           // empty if not unsure; otherwise tell them to contact a mod / open a ticket
  "rule": "string"                  // REQUIRED: the exact rule bullet text that was broken (or multiple, separated by '; ')
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
        return f'{{"ban_title":"Error","ban_length":"None","description":"OpenAI HTTP error {e.code}: {body[:400]}","is_bannable":false,"unsure":true,"suggestion":"Try again or contact a moderator.","rule":""}}'
    except Exception as e:
        return f'{{"ban_title":"Error","ban_length":"None","description":"OpenAI request failed: {e}","is_bannable":false,"unsure":true,"suggestion":"Try again or contact a moderator.","rule":""}}'

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
        return {
            "ban_title": str(obj.get("ban_title", "Unknown")),
            "ban_length": str(obj.get("ban_length", "Unknown")),
            "description": str(obj.get("description", "")).strip(),
            "is_bannable": bool(obj.get("is_bannable", False)),
            "unsure": bool(obj.get("unsure", False)),
            "suggestion": str(obj.get("suggestion", "")).strip(),
            "rule": str(obj.get("rule", "")).strip(),
        }
    except Exception:
        return {
            "ban_title": "Unparsed response",
            "ban_length": "Unknown",
            "description": f"Could not parse model output.\nRaw:\n{text[:1500]}",
            "is_bannable": False,
            "unsure": True,
            "suggestion": "I couldn’t confidently parse this. Please contact a moderator / open a report ticket.",
            "rule": "",
        }

def build_embed(result: dict) -> discord.Embed:
    title = f"{result['ban_title']} — {result['ban_length']}"
    desc = result["description"] or "No description provided."

    embed = discord.Embed(
        title=title,
        description=desc[:4096],
    )

    # "Rule" field (exact rule bullet text broken)
    rule_text = (result.get("rule") or "").strip()
    if not rule_text:
        rule_text = "No exact rule provided."
    if len(rule_text) > 900:
        rule_text = rule_text[:900] + "…"
    embed.add_field(name="Rule", value=rule_text, inline=False)

    # Footer used as “small text”
    if result.get("unsure") and result.get("suggestion"):
        embed.set_footer(text=result["suggestion"][:2048])

    return embed

# -------------------- TEMPLATE GRID UI --------------------
def make_templates_grid_embed(templates: List[Dict], page: int) -> discord.Embed:
    total = len(templates)
    total_pages = max(1, (total + TEMPLATES_PER_PAGE - 1) // TEMPLATES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * TEMPLATES_PER_PAGE
    end = min(start + TEMPLATES_PER_PAGE, total)
    chunk = templates[start:end]

    e = discord.Embed(
        title=f"🧩 Templates — Page {page+1}/{total_pages}",
        description=f"Showing **{start+1}–{end}** of **{total}** templates.",
    )

    for i, tpl in enumerate(chunk, start=start + 1):
        title = tpl.get("title", "Untitled")
        author = tpl.get("author", "Unknown")
        img_url = tpl.get("image_url", "")
        added_by = tpl.get("added_by_name", "Unknown")
        created_at = int(tpl.get("created_at", 0) or 0)

        link = f"[Open image]({img_url})" if img_url else "No image"
        added = f"<t:{created_at}:R>" if created_at else "Unknown time"

        value = (
            f"**Author:** {clamp_text(author, 80)}\n"
            f"**Added by:** {clamp_text(added_by, 80)} • **{added}**\n"
            f"{link}"
        )

        # inline=True makes it appear like a grid (2 columns on most clients)
        e.add_field(name=f"{i}) {clamp_text(title, 70)}", value=value, inline=True)

    e.set_footer(text="Use Prev/Next to browse pages.")
    return e

class TemplateGridPager(discord.ui.View):
    def __init__(self, templates: List[Dict], owner_id: int, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.templates = templates
        self.owner_id = owner_id
        self.page = 0
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the command user can control this menu.", ephemeral=True)
            return False
        return True

    def _total_pages(self) -> int:
        total = len(self.templates)
        return max(1, (total + TEMPLATES_PER_PAGE - 1) // TEMPLATES_PER_PAGE)

    def _update_buttons(self) -> None:
        tp = self._total_pages()
        self.prev_btn.disabled = (self.page <= 0)
        self.next_btn.disabled = (self.page >= tp - 1)

    def current_embed(self) -> discord.Embed:
        return make_templates_grid_embed(self.templates, self.page)

    @discord.ui.button(label="⬅ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self._total_pages() - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

# -------------------- EVENTS --------------------
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        print("⚠️ Slash sync error:", e)
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")

# -------------------- COMMANDS --------------------
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
    embed = build_embed(result)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")

# NEW: /add — upload template image + title + author
@bot.tree.command(name="add", description="Add a new template (image + title + author).")
@app_commands.describe(
    image="Upload the template image",
    title="Template title",
    author="Template author"
)
async def add_template(interaction: discord.Interaction, image: discord.Attachment, title: str, author: str):
    # best-effort type check
    if image.content_type and not image.content_type.startswith("image/"):
        await interaction.response.send_message("That file doesn’t look like an image.", ephemeral=True)
        return

    title = clamp_text(title, 80) or "Untitled"
    author = clamp_text(author, 80) or "Unknown"

    await interaction.response.defer(thinking=True)

    templates = await load_templates()
    templates.append({
        "id": f"{_now_unix()}-{interaction.user.id}",
        "title": title,
        "author": author,
        "image_url": image.url,  # Discord CDN URL
        "added_by_id": interaction.user.id,
        "added_by_name": str(interaction.user),
        "created_at": _now_unix(),
    })
    await save_templates(templates)

    e = discord.Embed(
        title="✅ Template Added",
        description=f"**Title:** {title}\n**Author:** {author}",
    )
    e.set_image(url=image.url)
    await interaction.followup.send(embed=e)

# NEW: /templates — browse templates as a grid page (6 per page)
@bot.tree.command(name="templates", description="Browse saved templates (grid pages).")
async def templates_cmd(interaction: discord.Interaction):
    tpls = await load_templates()
    if not tpls:
        await interaction.response.send_message("No templates have been added yet. Use `/add` first.", ephemeral=True)
        return

    view = TemplateGridPager(tpls, owner_id=interaction.user.id, timeout=180.0)
    await interaction.response.send_message(embed=view.current_embed(), view=view)

# -------------------- START --------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    bot.run(DISCORD_TOKEN)