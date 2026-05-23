import discord
from discord.ext import commands, tasks
from openai import OpenAI
import os
import sqlite3
import asyncio
import datetime
import re
import json
from collections import Counter
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GROQ MODELS — actual Groq model IDs, tried in order
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GROQ_MODELS = [
    "llama-3.3-70b-versatile",   # smartest, best reasoning
    "llama3-70b-8192",           # solid fallback
    "mixtral-8x7b-32768",        # good for longer context
    "llama3-8b-8192",            # fast lightweight fallback
    "gemma2-9b-it",              # last resort
]

# ✅ FIXED: Groq's correct base URL is /openai/v1, NOT /v1
ai_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_KEY,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DATABASE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DB_FILE = "fg_os_memory.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chat_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT, guild_id TEXT, username TEXT,
        message_content TEXT, hour INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS conversation_ctx (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT, role TEXT, content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT, guild_id TEXT, fact TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_nicknames (
        user_id TEXT PRIMARY KEY, nickname TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT, channel_id TEXT,
        reminder_text TEXT, due_at DATETIME, done INTEGER DEFAULT 0
    )''')
    conn.commit()
    conn.close()

def log_message(user_id, guild_id, username, content):
    if len(content.strip()) < 3:
        return
    hour = datetime.datetime.now().hour
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO chat_logs (user_id, guild_id, username, message_content, hour) VALUES (?,?,?,?,?)",
        (str(user_id), str(guild_id), username, content, hour)
    )
    conn.commit()
    conn.close()

def get_user_messages(user_id, limit=40):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT message_content FROM chat_logs WHERE user_id=? ORDER BY timestamp DESC LIMIT ?", (str(user_id), limit))
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    rows.reverse()
    return rows

def get_server_messages(guild_id, limit=60):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username, message_content FROM chat_logs WHERE guild_id=? ORDER BY timestamp DESC LIMIT ?", (str(guild_id), limit))
    rows = c.fetchall()
    conn.close()
    rows.reverse()
    return rows

def get_context(channel_id, max_turns=6):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT role, content FROM conversation_ctx WHERE channel_id=? ORDER BY timestamp DESC LIMIT ?", (str(channel_id), max_turns * 2))
    rows = c.fetchall()
    conn.close()
    rows.reverse()
    return [{"role": r[0], "content": r[1]} for r in rows]

def save_context(channel_id, role, content, max_keep=12):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO conversation_ctx (channel_id, role, content) VALUES (?,?,?)", (str(channel_id), role, content))
    c.execute('''DELETE FROM conversation_ctx WHERE channel_id=? AND id NOT IN (
        SELECT id FROM conversation_ctx WHERE channel_id=? ORDER BY timestamp DESC LIMIT ?
    )''', (str(channel_id), str(channel_id), max_keep))
    conn.commit()
    conn.close()

def save_memory(user_id, guild_id, fact):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT fact FROM user_memory WHERE user_id=? AND guild_id=?", (str(user_id), str(guild_id)))
    existing = [r[0].lower() for r in c.fetchall()]
    if not any(fact.lower()[:30] in e for e in existing):
        c.execute("INSERT INTO user_memory (user_id, guild_id, fact) VALUES (?,?,?)", (str(user_id), str(guild_id), fact))
        c.execute('''DELETE FROM user_memory WHERE user_id=? AND guild_id=? AND id NOT IN (
            SELECT id FROM user_memory WHERE user_id=? AND guild_id=? ORDER BY timestamp DESC LIMIT 20
        )''', (str(user_id), str(guild_id), str(user_id), str(guild_id)))
    conn.commit()
    conn.close()

def get_memories(user_id, guild_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT fact FROM user_memory WHERE user_id=? AND guild_id=? ORDER BY timestamp DESC LIMIT 10", (str(user_id), str(guild_id)))
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

def get_nickname(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT nickname FROM user_nicknames WHERE user_id=?", (str(user_id),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_nickname(user_id, nickname):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_nicknames (user_id, nickname) VALUES (?,?)", (str(user_id), nickname))
    conn.commit()
    conn.close()

def save_reminder(user_id, channel_id, text, due_at):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO reminders (user_id, channel_id, reminder_text, due_at) VALUES (?,?,?,?)",
              (str(user_id), str(channel_id), text, due_at.isoformat()))
    conn.commit()
    conn.close()

def get_due_reminders():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    c.execute("SELECT id, user_id, channel_id, reminder_text FROM reminders WHERE due_at <= ? AND done=0", (now,))
    rows = c.fetchall()
    conn.close()
    return rows

def mark_reminder_done(reminder_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))
    conn.commit()
    conn.close()

init_db()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STYLE FINGERPRINTING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STOPWORDS = {
    "the","a","an","is","it","in","on","at","to","of","and","i","you","my","me",
    "he","she","they","we","do","be","was","are","for","that","this","have","not",
    "with","so","but","or","if","its","im","dont","can","just","ur","u","r","ok",
    "yeah","yes","no","like","get","go","got","idk","ill","ive","id","its","thats"
}

def fingerprint_user(messages: list) -> str:
    if not messages:
        return "No user data yet — be natural and casual."
    n = len(messages)
    avg_len = sum(len(m) for m in messages) / n
    lc_ratio = sum(1 for m in messages if m == m.lower()) / n
    no_punct = sum(1 for m in messages if not any(p in m for p in ".!?")) / n
    ellipsis = sum(1 for m in messages if "..." in m)
    laugh = sum(1 for m in messages if any(w in m.lower() for w in ["lol","lmao","lmfao","💀","😭","haha","bruh"]))
    caps_words = sum(1 for m in messages for w in m.split() if w.isupper() and len(w) > 1)

    # Detect emojis they actually use
    emoji_pattern = re.compile(
        "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF"
        "\U00002702-\U000027B0\U000024C2-\U0001F251]+", flags=re.UNICODE
    )
    all_emojis = []
    for m in messages:
        all_emojis.extend(emoji_pattern.findall(m))
    emoji_freq = Counter(all_emojis).most_common(5)
    emoji_note = ""
    if emoji_freq:
        emoji_note = f"Their actual emojis: {', '.join(e for e, _ in emoji_freq)}"
    else:
        emoji_note = "They rarely/never use emojis — don't use them either"

    traits = []
    if lc_ratio > 0.75: traits.append("mostly lowercase")
    elif lc_ratio < 0.3: traits.append("often uses caps")
    if no_punct > 0.6: traits.append("skips punctuation")
    if avg_len < 35: traits.append("short punchy messages")
    elif avg_len > 100: traits.append("writes longer messages")
    if ellipsis > 2: traits.append("uses ... a lot")
    if laugh > 3: traits.append("uses lol/lmao/bruh frequently")
    if caps_words > 4: traits.append("uses ALL CAPS for emphasis")

    sample = "\n".join(f"  {m}" for m in messages[-15:])
    return (
        f"USER STYLE TRAITS: {', '.join(traits) if traits else 'pretty normal'}\n"
        f"EMOJI BEHAVIOR: {emoji_note}\n"
        f"RECENT MESSAGES FROM THIS USER:\n{sample}"
    )

def fingerprint_server(server_msgs: list) -> str:
    if not server_msgs:
        return ""
    sample = "\n".join(f"  {u}: {m}" for u, m in server_msgs[-20:])
    return f"SERVER VIBE (recent messages from the whole server):\n{sample}"

def detect_mood(text: str) -> str:
    text_lower = text.lower()
    if any(w in text_lower for w in ["sad","depressed","crying","miss","alone","hurt","heartbreak","tired","lost"]):
        return "venting/sad"
    if any(w in text_lower for w in ["angry","mad","pissed","hate","stupid","idiot","annoyed","frustrated"]):
        return "angry/frustrated"
    if any(w in text_lower for w in ["lmao","lol","haha","funny","💀","😭","bruh","fr fr","no way"]):
        return "hype/joking"
    if any(w in text_lower for w in ["help","how","what","why","explain","confused","idk"]):
        return "curious/asking"
    return "chill/neutral"

def extract_facts(text: str) -> list:
    facts = []
    patterns = [
        r"i (play|love|hate|like|work|study|go to|live|am|was|have|own|use|watch|listen)",
        r"my (name|age|job|school|team|favorite|hobby|pet|car|girlfriend|boyfriend|mom|dad)",
        r"i'm (a|an|the|into|good at|bad at|from)",
        r"i've (been|done|played|watched|worked)",
    ]
    for pat in patterns:
        if re.search(pat, text.lower()):
            facts.append(text[:120])
            break
    return facts


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SYSTEM PROMPT — smarter, better word choice, emoji control
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def build_prompt(username: str, user_fp: str, server_fp: str, memories: list, mood: str, nickname: str = None) -> str:
    display_name = nickname or username
    memory_section = ""
    if memories:
        memory_section = f"\nTHINGS YOU REMEMBER ABOUT {username.upper()}:\n" + "\n".join(f"  - {m}" for m in memories) + "\n"
    mood_instruction = {
        "venting/sad":      "They seem down. Be real and supportive. Don't be hype or dismissive. Acknowledge what they said.",
        "angry/frustrated": "They're frustrated. Match their energy a little, don't be preachy or calm them down.",
        "hype/joking":      "They're in a joking/hype mood. Match that energy. Be fun.",
        "curious/asking":   "They want a real answer. Think it through properly then give it to them in their style.",
        "chill/neutral":    "Chill vibe. Keep it casual and natural.",
    }.get(mood, "Keep it casual.")
    server_section = f"\n{server_fp}\n" if server_fp else ""

    return f"""You are FG-OS — a Discord AI that is both genuinely intelligent AND a perfect style mirror.

═══ RULE 1: THINK FIRST, THEN ANSWER ═══
Before writing your response, silently reason through:
- What is this person actually asking or saying?
- What's the smartest, most accurate answer?
- What slang/words would THEY use to say this?
Write ALL your thinking inside <think></think> tags. Only your final message goes after.

═══ RULE 2: MIRROR {username.upper()}'S EXACT STYLE ═══
You must sound like THEM, not like an AI. Study the fingerprint below carefully.
- Match their capitalization exactly (mostly lowercase? you're lowercase. they capitalize? you capitalize.)
- Match their punctuation habits (no periods? you use no periods. they use ... ? you use ... )
- Match their message length — don't write a paragraph if they text in 5 words
- Use THEIR actual slang and words, not generic AI casual speak
- Word choice matters: pick words they would actually say. If they say "bro" not "buddy", you say "bro".
- Slang accuracy: understand context (e.g. "bricked up" is a sexual/hype term, not a broken phone)

═══ RULE 3: EMOJI CONTROL ═══
This is critical — the old bot spammed the same emojis constantly. You must NOT do this.
- ONLY use emojis that this user actually uses themselves (see their emoji behavior below)
- NEVER use the same emoji twice in one response
- If they don't use emojis, you don't use emojis. Period.
- Max 1-2 emojis per response, only if it genuinely fits

═══ CURRENT CONTEXT ═══
Mood detected: {mood} → {mood_instruction}
{memory_section}
{user_fp}
{server_section}
Call them "{display_name}". Be smart. Sound like them. Think first."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AI CALL — Groq with proper model IDs + think tag stripping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def call_ai(system: str, history: list, user_msg: str, max_tokens: int = 800) -> str:
    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_msg})
    last_error = None

    for model in GROQ_MODELS:
        try:
            print(f"Trying: {model}")
            completion = ai_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.75,
            )
            content = completion.choices[0].message.content
            if content and content.strip():
                print(f"✓ {model}")
                # Strip <think>...</think> reasoning block — user never sees this
                clean = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                # Also strip any leftover think tags if model did it weirdly
                clean = re.sub(r'</?think>', '', clean).strip()
                return clean if clean else content.strip()
            last_error = f"{model} returned empty"
        except Exception as e:
            print(f"✗ {model}: {e}")
            last_error = str(e)

    raise RuntimeError(f"All models failed. Last error: {last_error}")

def quick_ai(prompt: str, max_tokens: int = 500) -> str:
    """Single-turn AI call — used for commands like roast, poll, tldr etc."""
    system = (
        "You are FG-OS. Think through your response carefully inside <think></think> tags first, "
        "then write your final output after. Be concise and accurate."
    )
    return call_ai(system, [], prompt, max_tokens)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SHARED CHAT HANDLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def handle_query(user_id, guild_id, username, channel_id, prompt, reply_fn):
    user_msgs   = get_user_messages(user_id, limit=40)
    server_msgs = get_server_messages(guild_id, limit=60)
    memories    = get_memories(user_id, guild_id)
    nickname    = get_nickname(user_id)
    mood        = detect_mood(prompt)
    user_fp     = fingerprint_user(user_msgs)
    server_fp   = fingerprint_server(server_msgs)
    system      = build_prompt(username, user_fp, server_fp, memories, mood, nickname)
    history     = get_context(channel_id)

    response = call_ai(system, history, prompt)

    save_context(channel_id, "user", prompt)
    save_context(channel_id, "assistant", response)

    for fact in extract_facts(prompt):
        save_memory(user_id, guild_id, fact)

    for chunk in [response[i:i+1990] for i in range(0, len(response), 1990)]:
        await reply_fn(chunk)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKGROUND TASKS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@tasks.loop(minutes=1)
async def check_reminders():
    due = get_due_reminders()
    for reminder_id, user_id, channel_id, text in due:
        try:
            channel = bot.get_channel(int(channel_id))
            if channel:
                await channel.send(f"⏰ <@{user_id}> reminder: **{text}**")
            mark_reminder_done(reminder_id)
        except Exception as e:
            print(f"Reminder error: {e}")

starter_channels = set()

@tasks.loop(minutes=45)
async def conversation_starter():
    if not starter_channels:
        return
    for channel_id in list(starter_channels):
        try:
            channel = bot.get_channel(int(channel_id))
            if not channel:
                continue
            guild_id = str(channel.guild.id) if channel.guild else "dm"
            server_msgs = get_server_messages(guild_id, limit=30)
            server_fp = fingerprint_server(server_msgs)
            prompt = (
                f"Drop ONE casual conversation starter that fits this server's vibe. "
                f"Short, feels like a real person texting. No AI talk. Natural not forced.\n{server_fp}"
            )
            msg = quick_ai(prompt, max_tokens=80)
            await channel.send(msg)
        except Exception as e:
            print(f"Starter error: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BOT LIFECYCLE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.event
async def setup_hook():
    await bot.tree.sync()

@bot.event
async def on_ready():
    check_reminders.start()
    conversation_starter.start()
    print("━" * 50)
    print(f"FG-OS LIVE: {bot.user.name}")
    print(f"Backend: Groq | Models: {GROQ_MODELS}")
    print("━" * 50)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CORE COMMANDS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.hybrid_command(name="help", description="Show all FG-OS commands")
async def help_command(ctx):
    await ctx.defer()
    cmds = sorted([cmd for cmd in bot.commands if not cmd.hidden and cmd.name != "help"], key=lambda c: c.name)
    lines = ["**FG-OS Commands**", "Use `!command` or `/command`", ""]
    for cmd in cmds:
        lines.append(f"`{cmd.name}` — {cmd.description or 'No description'}")
    content = "\n".join(lines)
    for chunk in [content[i:i+1900] for i in range(0, len(content), 1900)]:
        await ctx.send(chunk)

@bot.hybrid_command(name="ask", description="Ask FG-OS anything")
async def ask_fg_os(ctx, *, question: str):
    await ctx.defer()
    try:
        guild_id = ctx.guild.id if ctx.guild else "dm"
        await handle_query(ctx.author.id, guild_id, ctx.author.display_name, ctx.channel.id, question, ctx.send)
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="clear", description="Wipe this channel's convo memory")
async def clear_ctx(ctx):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM conversation_ctx WHERE channel_id=?", (str(ctx.channel.id),))
    conn.commit()
    conn.close()
    await ctx.send("memory cleared")

@bot.hybrid_command(name="mystyle", description="See your style fingerprint")
async def my_style(ctx):
    await ctx.defer(ephemeral=True)
    msgs = get_user_messages(ctx.author.id, limit=40)
    fp = fingerprint_user(msgs)
    out = fp[:1800] if len(fp) > 1800 else fp
    await ctx.send(f"```\nYOUR STYLE:\n{out}\n```", ephemeral=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MEMORY & PERSONALITY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.hybrid_command(name="setnick", description="Set what FG-OS calls you")
async def setnick(ctx, *, nickname: str):
    set_nickname(ctx.author.id, nickname.strip())
    await ctx.send(f"got it, calling you {nickname.strip()} from now on")

@bot.hybrid_command(name="mymemory", description="See what FG-OS remembers about you")
async def my_memory(ctx):
    await ctx.defer(ephemeral=True)
    memories = get_memories(ctx.author.id, str(ctx.guild.id) if ctx.guild else "dm")
    if not memories:
        await ctx.send("don't remember anything specific yet, talk more", ephemeral=True)
        return
    out = "\n".join(f"- {m}" for m in memories)
    await ctx.send(f"```\nWHAT I KNOW ABOUT YOU:\n{out}\n```", ephemeral=True)

@bot.hybrid_command(name="clearmemory", description="Wipe FG-OS's memory of you")
async def clear_memory(ctx):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    c.execute("DELETE FROM user_memory WHERE user_id=? AND guild_id=?", (str(ctx.author.id), guild_id))
    conn.commit()
    conn.close()
    await ctx.send("cleared everything i knew about u")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SERVER STATS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.hybrid_command(name="stats", description="See stats for a user")
async def stats(ctx, member: discord.Member = None):
    await ctx.defer()
    target = member or ctx.author
    guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM chat_logs WHERE user_id=? AND guild_id=?", (str(target.id), guild_id))
    total = c.fetchone()[0]
    c.execute("SELECT message_content FROM chat_logs WHERE user_id=? AND guild_id=?", (str(target.id), guild_id))
    all_msgs = [r[0] for r in c.fetchall()]
    c.execute("SELECT hour, COUNT(*) as cnt FROM chat_logs WHERE user_id=? AND guild_id=? GROUP BY hour ORDER BY cnt DESC LIMIT 1", (str(target.id), guild_id))
    peak_row = c.fetchone()
    conn.close()
    if total == 0:
        await ctx.send(f"no messages logged for {target.display_name} yet")
        return
    words = []
    for m in all_msgs:
        words.extend(w.lower().strip(".,!?") for w in m.split() if len(w) > 2 and w.lower() not in STOPWORDS)
    top_words = Counter(words).most_common(8)
    top_str = ", ".join(f"{w}({cnt})" for w, cnt in top_words) if top_words else "not enough data"
    peak_hour = f"{peak_row[0]}:00-{peak_row[0]+1}:00" if peak_row else "unknown"
    avg_len = sum(len(m) for m in all_msgs) / len(all_msgs) if all_msgs else 0
    embed = discord.Embed(title=f"📊 Stats for {target.display_name}", color=0x5865F2)
    embed.add_field(name="Messages Logged", value=str(total), inline=True)
    embed.add_field(name="Avg Msg Length", value=f"{avg_len:.0f} chars", inline=True)
    embed.add_field(name="Most Active Hour", value=peak_hour, inline=True)
    embed.add_field(name="Top Words/Slang", value=top_str, inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="leaderboard", description="Who's been most active in this server")
async def leaderboard(ctx):
    await ctx.defer()
    guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username, COUNT(*) as cnt FROM chat_logs WHERE guild_id=? GROUP BY user_id ORDER BY cnt DESC LIMIT 10", (guild_id,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await ctx.send("no data yet, people gotta talk first")
        return
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines = [f"{medals[i]} **{row[0]}** — {row[1]} messages" for i, row in enumerate(rows)]
    embed = discord.Embed(title="🏆 Server Leaderboard", description="\n".join(lines), color=0xFFD700)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="servervibe", description="AI analysis of the server's current mood")
async def servervibe(ctx):
    await ctx.defer()
    guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    server_msgs = get_server_messages(guild_id, limit=80)
    if len(server_msgs) < 5:
        await ctx.send("not enough messages logged yet")
        return
    sample = "\n".join(f"{u}: {m}" for u, m in server_msgs[-40:])
    prompt = (
        f"Analyze the vibe of this Discord server based on recent messages. "
        f"3-4 casual sentences. Cover overall energy, recurring topics, general mood. "
        f"Sound like a real person observing, not a report.\n\nRECENT MESSAGES:\n{sample}"
    )
    try:
        vibe = quick_ai(prompt, max_tokens=200)
        embed = discord.Embed(title="🌡️ Server Vibe Check", description=vibe, color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TOOLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.hybrid_command(name="summarize", description="Summarize the last N messages in this channel")
async def summarize(ctx, count: int = 20):
    await ctx.defer()
    if count > 50: count = 50
    try:
        messages = []
        async for msg in ctx.channel.history(limit=count + 1):
            if msg.author != bot.user and msg.content:
                messages.append(f"{msg.author.display_name}: {msg.content}")
        messages.reverse()
        if not messages:
            await ctx.send("nothing to summarize")
            return
        convo = "\n".join(messages[-count:])
        prompt = f"Summarize this Discord conversation in 3-5 casual sentences. Key points and decisions only. Sound natural:\n\n{convo}"
        summary = quick_ai(prompt, max_tokens=200)
        embed = discord.Embed(title=f"📝 Last {count} Messages", description=summary, color=0x5865F2)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="tldr", description="Paste a wall of text and get a short summary")
async def tldr(ctx, *, text: str):
    await ctx.defer()
    try:
        guild_id = str(ctx.guild.id) if ctx.guild else "dm"
        server_msgs = get_server_messages(guild_id, limit=20)
        server_fp = fingerprint_server(server_msgs)
        prompt = f"TLDR in 2-3 casual sentences. No bullet points. Match the server tone.\n\n{server_fp}\n\nTEXT:\n{text[:2000]}"
        result = quick_ai(prompt, max_tokens=150)
        await ctx.send(f"**TLDR:** {result}")
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="poll", description="AI generates a poll from your question")
async def poll(ctx, *, question: str):
    await ctx.defer()
    try:
        prompt = (
            f"Make a Discord poll for: '{question}'\n"
            f"Return ONLY a numbered list of max 4 short options (under 30 chars each). No other text.\n"
            f"Like:\n1. Option A\n2. Option B"
        )
        result = quick_ai(prompt, max_tokens=100)
        lines = [l.strip() for l in result.strip().split("\n") if l.strip()]
        options = [re.sub(r"^[\d]+[.)]\s*", "", l).strip() for l in lines if l]
        options = [o for o in options if o][:4]
        if not options:
            await ctx.send("couldn't generate options, try rephrasing")
            return
        emojis = ["1️⃣","2️⃣","3️⃣","4️⃣"]
        poll_text = f"**📊 {question}**\n\n" + "\n".join(f"{emojis[i]} {o}" for i, o in enumerate(options))
        poll_msg = await ctx.send(poll_text)
        for i in range(len(options)):
            await poll_msg.add_reaction(emojis[i])
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="remind", description='Set a reminder. Example: "do homework in 2 hours"')
async def remind(ctx, *, text: str):
    await ctx.defer(ephemeral=True)
    try:
        prompt = (
            f"Extract task and time from: '{text}'\n"
            f"Return ONLY JSON like: {{\"task\": \"do homework\", \"minutes\": 120}}\n"
            f"Convert time to minutes. Default 60 if not found. No other text."
        )
        result = quick_ai(prompt, max_tokens=60)
        match = re.search(r'\{.*?\}', result, re.DOTALL)
        if not match:
            await ctx.send("couldn't parse that — try 'remind me to do X in 2 hours'", ephemeral=True)
            return
        data = json.loads(match.group())
        task = data.get("task", text)
        minutes = max(1, min(int(data.get("minutes", 60)), 10080))
        due_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
        save_reminder(ctx.author.id, ctx.channel.id, task, due_at)
        time_str = f"{minutes} min" if minutes < 60 else (f"{minutes//60}h {minutes%60}m" if minutes % 60 else f"{minutes//60}h")
        await ctx.send(f"got it, reminding you about **{task}** in {time_str}", ephemeral=True)
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}", ephemeral=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FUN COMMANDS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.hybrid_command(name="roast", description="Gen Z roast someone based on how they actually text")
async def roast(ctx, member: discord.Member):
    await ctx.defer()
    guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    target_msgs = get_user_messages(member.id, limit=40)
    if len(target_msgs) < 5:
        await ctx.send(f"not enough msgs from {member.display_name} to roast them, they gotta talk more")
        return
    sample = "\n".join(target_msgs[-25:])
    prompt = (
        f"You are a ruthless Gen Z Discord roast bot. Roast {member.display_name} using ONLY evidence from their actual messages.\n\n"
        f"RULES:\n"
        f"- Find the most roastable specific thing in their messages: weird phrasing, cringe energy, how they type, something they overshared\n"
        f"- Gen Z delivery: dry, deadpan, brutally specific. Think 'bro types like his autocorrect has beef with him' not 'you're dumb'\n"
        f"- 2-3 sentences MAX. Shorter is more devastating.\n"
        f"- PERSONALIZED only — if you can't tie it to their actual messages, you failed\n"
        f"- Funny, not actually offensive or about appearance/identity\n"
        f"- End with one Gen Z closer: 'fr 💀' / 'no cap' / 'bro i'm done' / 'on god' etc\n\n"
        f"THEIR MESSAGES:\n{sample}\n\n"
        f"Write the roast. Nothing else."
    )
    try:
        roast_text = quick_ai(prompt, max_tokens=150)
        await ctx.send(f"🔥 {member.mention} {roast_text}")
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="impersonate", description="FG-OS responds as if it IS that person — their digital twin")
async def impersonate(ctx, member: discord.Member, *, situation: str):
    await ctx.defer()
    guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    target_msgs = get_user_messages(member.id, limit=60)
    if len(target_msgs) < 5:
        await ctx.send(f"not enough messages from {member.display_name}, they gotta talk more first")
        return

    msgs = target_msgs
    n = len(msgs)
    avg_len = sum(len(m) for m in msgs) / n
    lc_ratio = sum(1 for m in msgs if m == m.lower()) / n
    no_punct = sum(1 for m in msgs if not any(p in m for p in ".!?")) / n
    ellipsis_count = sum(1 for m in msgs if "..." in m)
    laugh_count = sum(1 for m in msgs if any(w in m.lower() for w in ["lol","lmao","lmfao","💀","😭","haha","bruh","ngl","fr","bro"]))
    caps_words = [w for m in msgs for w in m.split() if w.isupper() and len(w) > 1]
    all_words = [w.lower().strip(".,!?'\"") for m in msgs for w in m.split() if len(w) > 2]
    word_counts = Counter(all_words)
    sig_words = [(w, cnt) for w, cnt in word_counts.most_common(30) if w not in STOPWORDS and cnt > 1]
    sig_str = ", ".join(f'"{w}"({cnt}x)' for w, cnt in sig_words[:12])

    patterns = []
    if lc_ratio > 0.8: patterns.append("writes in almost ALL lowercase — never capitalize unless their messages show it")
    elif lc_ratio < 0.2: patterns.append("uses normal capitalization most of the time")
    else: patterns.append("mixed capitalization — copy exactly what their messages show")
    if no_punct > 0.7: patterns.append("almost never uses periods or ending punctuation — don't add any")
    elif no_punct < 0.3: patterns.append("actually uses punctuation consistently")
    if avg_len < 20: patterns.append("sends extremely short messages, like 1-4 words")
    elif avg_len < 45: patterns.append("short punchy messages, rarely goes long")
    else: patterns.append("comfortable writing longer messages")
    if ellipsis_count > 3: patterns.append("uses ... a lot for trailing off")
    if laugh_count > 5: patterns.append("reacts with lol/lmao/bruh/💀 very often")
    if caps_words: patterns.append(f"uses ALL CAPS for emphasis: {', '.join(set(caps_words[:6]))}")

    sample_display = "\n".join(f'  "{m}"' for m in msgs[-20:])

    prompt = (
        f"You ARE {member.display_name}. You are their digital twin texting in Discord.\n\n"
        f"HARD RULES (break these = failure):\n"
        + "\n".join(f"  • {p}" for p in patterns) +
        f"\n\nSIGNATURE WORDS (use naturally if relevant):\n  {sig_str or 'not enough data'}\n\n"
        f"THEIR REAL MESSAGES — internalize the rhythm, personality, energy:\n{sample_display}\n\n"
        f"SITUATION: {situation}\n\n"
        f"Write ONE response as them. Same caps, same punctuation (or lack of it), same length, same vibe. "
        f"Someone who knows them should think it's actually them. No AI tone. No extra formality. Just their message."
    )
    try:
        response = quick_ai(prompt, max_tokens=200)
        await ctx.send(f"**{member.display_name}:** {response}")
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="debate", description="Two AI personalities debate a topic")
async def debate(ctx, *, topic: str):
    await ctx.defer()
    try:
        prompt = (
            f"Two people debating: '{topic}'\n"
            f"Short back-and-forth, 2 turns each. Label 'Side A:' and 'Side B:'. "
            f"Casual and punchy like a Discord argument. 1-2 sentences per turn. Both sides make real points."
        )
        result = quick_ai(prompt, max_tokens=300)
        embed = discord.Embed(title=f"⚔️ Debate: {topic}", description=result, color=0xED4245)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="starterchannel", description="Toggle conversation starters in this channel (~every 45 min)")
async def starterchannel(ctx):
    channel_id = str(ctx.channel.id)
    if channel_id in starter_channels:
        starter_channels.discard(channel_id)
        await ctx.send("convo starters turned OFF for this channel")
    else:
        starter_channels.add(channel_id)
        await ctx.send("convo starters ON — dropping one every ~45 min")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MESSAGE WATCHER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content = (message.content or "").strip()
    guild_id = message.guild.id if message.guild else "dm"

    if content and not content.startswith("/") and not content.startswith("!"):
        log_message(message.author.id, guild_id, message.author.display_name, content)
        for fact in extract_facts(content):
            save_memory(message.author.id, str(guild_id), fact)

    if bot.user.mentioned_in(message):
        async with message.channel.typing():
            try:
                prompt = (message.content or "").replace(f"<@{bot.user.id}>", "").strip() or "yo"
                await handle_query(
                    message.author.id, guild_id, message.author.display_name,
                    message.channel.id, prompt, message.reply
                )
            except Exception as e:
                await message.reply(f"⚠️ Error: {e}")

    await bot.process_commands(message)


bot.run(DISCORD_TOKEN)