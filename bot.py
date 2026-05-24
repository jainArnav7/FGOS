# ============================================================
# FG-OS Discord Bot v2
# pip install "discord.py[voice]" PyNaCl openai python-dotenv
# ffmpeg must be installed and on PATH
# ============================================================

import discord
from discord.ext import commands, tasks
from openai import OpenAI
import os
import sqlite3
import asyncio
import datetime
import re
import io
import json
import random
import struct
from collections import Counter, defaultdict
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama3-70b-8192",
    "mixtral-8x7b-32768",
    "llama3-8b-8192",
    "gemma2-9b-it",
]
WHISPER_MODEL = "whisper-large-v3-turbo"

ai_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_KEY,
)

voice_sessions: dict = {}
starter_channels = set()

DB_FILE = "fg_os_memory.db"

STOPWORDS = {
    "the","a","an","is","it","in","on","at","to","of","and","i","you","my","me",
    "he","she","they","we","do","be","was","are","for","that","this","have","not",
    "with","so","but","or","if","its","im","dont","can","just","ur","u","r","ok",
    "yeah","yes","no","like","get","go","got","idk","ill","ive","id","thats","what",
    "this","that","there","here","why","how","when","where","who","whom","which",
    "all","any","some","more","most","less","very","really","kind","kinda","sort",
    "maybe","yeah","nah","bro","bruh","fr"
}

EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0\U000024C2-\U0001F251]+",
    flags=re.UNICODE
)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS chat_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT, guild_id TEXT, username TEXT,
        message_content TEXT, hour INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS conversation_ctx (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT, role TEXT, content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT, guild_id TEXT, fact TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_nicknames (
        user_id TEXT PRIMARY KEY, nickname TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT, channel_id TEXT,
        reminder_text TEXT, due_at DATETIME, done INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS voice_transcripts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT, guild_id TEXT, username TEXT,
        transcript TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()

def log_message(user_id, guild_id, username, content):
    text = (content or "").strip()
    if len(text) < 3:
        return
    hour = datetime.datetime.now().hour
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO chat_logs (user_id, guild_id, username, message_content, hour) VALUES (?,?,?,?,?)",
        (str(user_id), str(guild_id), username, text, hour)
    )
    conn.commit()
    conn.close()

def get_user_messages(user_id, limit=40):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT message_content FROM chat_logs WHERE user_id=? ORDER BY timestamp DESC LIMIT ?",
        (str(user_id), limit)
    )
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    rows.reverse()
    return rows

def get_server_messages(guild_id, limit=60):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT username, message_content FROM chat_logs WHERE guild_id=? ORDER BY timestamp DESC LIMIT ?",
        (str(guild_id), limit)
    )
    rows = c.fetchall()
    conn.close()
    rows.reverse()
    return rows

def get_context(channel_id, max_turns=6):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM conversation_ctx WHERE channel_id=? ORDER BY timestamp DESC LIMIT ?",
        (str(channel_id), max_turns * 2)
    )
    rows = c.fetchall()
    conn.close()
    rows.reverse()
    return [{"role": r[0], "content": r[1]} for r in rows]

def save_context(channel_id, role, content, max_keep=12):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO conversation_ctx (channel_id, role, content) VALUES (?,?,?)",
        (str(channel_id), role, content)
    )
    c.execute(
        """DELETE FROM conversation_ctx WHERE channel_id=? AND id NOT IN (
            SELECT id FROM conversation_ctx WHERE channel_id=? ORDER BY timestamp DESC LIMIT ?
        )""",
        (str(channel_id), str(channel_id), max_keep)
    )
    conn.commit()
    conn.close()

def save_memory(user_id, guild_id, fact):
    fact = (fact or "").strip()
    if not fact:
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT fact FROM user_memory WHERE user_id=? AND guild_id=? ORDER BY timestamp DESC LIMIT 50",
        (str(user_id), str(guild_id))
    )
    existing = [r[0].lower() for r in c.fetchall()]
    if not any(fact.lower()[:40] in e or e[:40] in fact.lower() for e in existing):
        c.execute(
            "INSERT INTO user_memory (user_id, guild_id, fact) VALUES (?,?,?)",
            (str(user_id), str(guild_id), fact)
        )
        c.execute(
            """DELETE FROM user_memory WHERE user_id=? AND guild_id=? AND id NOT IN (
                SELECT id FROM user_memory WHERE user_id=? AND guild_id=? ORDER BY timestamp DESC LIMIT 20
            )""",
            (str(user_id), str(guild_id), str(user_id), str(guild_id))
        )
    conn.commit()
    conn.close()

def get_memories(user_id, guild_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT fact FROM user_memory WHERE user_id=? AND guild_id=? ORDER BY timestamp DESC LIMIT 10",
        (str(user_id), str(guild_id))
    )
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
    c.execute(
        "INSERT OR REPLACE INTO user_nicknames (user_id, nickname) VALUES (?,?)",
        (str(user_id), nickname)
    )
    conn.commit()
    conn.close()

def save_reminder(user_id, channel_id, text, due_at):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO reminders (user_id, channel_id, reminder_text, due_at) VALUES (?,?,?,?)",
        (str(user_id), str(channel_id), text, due_at.isoformat())
    )
    conn.commit()
    conn.close()

def get_due_reminders():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    c.execute(
        "SELECT id, user_id, channel_id, reminder_text FROM reminders WHERE due_at <= ? AND done=0",
        (now,)
    )
    rows = c.fetchall()
    conn.close()
    return rows

def mark_reminder_done(reminder_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))
    conn.commit()
    conn.close()

def save_voice_transcript(user_id, guild_id, username, transcript):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO voice_transcripts (user_id, guild_id, username, transcript) VALUES (?,?,?,?)",
        (str(user_id), str(guild_id), username, transcript)
    )
    c.execute(
        """DELETE FROM voice_transcripts WHERE user_id=? AND guild_id=? AND id NOT IN (
            SELECT id FROM voice_transcripts WHERE user_id=? AND guild_id=? ORDER BY timestamp DESC LIMIT 50
        )""",
        (str(user_id), str(guild_id), str(user_id), str(guild_id))
    )
    conn.commit()
    conn.close()

def get_voice_transcripts(user_id, guild_id, limit=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT transcript FROM voice_transcripts WHERE user_id=? AND guild_id=? ORDER BY timestamp DESC LIMIT ?",
        (str(user_id), str(guild_id), limit)
    )
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    rows.reverse()
    return rows

init_db()

def fingerprint_user(messages: list) -> str:
    if not messages:
        return "No user data yet."

    n = len(messages)
    avg_len = sum(len(m) for m in messages) / n
    lc_ratio = sum(1 for m in messages if m == m.lower()) / n
    no_punct = sum(1 for m in messages if not any(p in m for p in ".!?")) / n
    ellipsis = sum(1 for m in messages if "..." in m)
    laugh = sum(1 for m in messages if any(w in m.lower() for w in ["lol","lmao","lmfao","💀","😭","haha","bruh","fr"]))
    caps_words = [w for m in messages for w in m.split() if w.isupper() and len(w) > 1]

    all_emojis = []
    for m in messages:
        all_emojis.extend(EMOJI_RE.findall(m))
    emoji_freq = Counter(all_emojis).most_common(4)

    if emoji_freq:
        emoji_note = f"Emojis they use: {', '.join(e for e, _ in emoji_freq)}. Use at most 1."
    else:
        emoji_note = "They rarely use emojis. Avoid them."

    all_words = [
        w.lower().strip(".,!?'\"")
        for m in messages
        for w in m.split()
        if len(w) > 2
    ]
    sig_words = [(w, cnt) for w, cnt in Counter(all_words).most_common(20) if w not in STOPWORDS and cnt > 1]
    sig_str = ", ".join(f'"{w}"' for w, _ in sig_words[:8]) if sig_words else "none"

    traits = []
    if lc_ratio > 0.75:
        traits.append("mostly lowercase")
    elif lc_ratio < 0.3:
        traits.append("uses normal capitalization")
    if no_punct > 0.6:
        traits.append("skips ending punctuation often")
    if avg_len < 25:
        traits.append("very short messages")
    elif avg_len < 50:
        traits.append("short-medium messages")
    elif avg_len > 100:
        traits.append("longer messages")
    if ellipsis > 2:
        traits.append("uses ... a lot")
    if laugh > 3:
        traits.append("uses lol/lmao/bruh a lot")
    if caps_words:
        traits.append(f"uses caps for emphasis: {', '.join(set(caps_words[:5]))}")

    sample = "\n".join(f"  {m}" for m in messages[-10:])
    return (
        f"STYLE: {', '.join(traits) or 'pretty normal'}\n"
        f"EMOJI: {emoji_note}\n"
        f"SIGNATURE WORDS: {sig_str}\n"
        f"SAMPLE:\n{sample}"
    )

def fingerprint_server(server_msgs: list) -> str:
    if not server_msgs:
        return ""
    sample = "\n".join(f"  {u}: {m}" for u, m in server_msgs[-15:])
    return f"SERVER VIBE:\n{sample}"

def detect_mood(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["sad","depressed","crying","miss","alone","hurt","heartbreak","tired","lost"]):
        return "venting/sad"
    if any(w in t for w in ["angry","mad","pissed","hate","stupid","idiot","annoyed","frustrated"]):
        return "angry/frustrated"
    if any(w in t for w in ["lmao","lol","haha","funny","💀","😭","bruh","fr fr","no way"]):
        return "hype/joking"
    if any(w in t for w in ["help","how","what","why","explain","confused","idk"]):
        return "curious/asking"
    return "chill/neutral"

def extract_facts(text: str) -> list:
    t = text.strip()
    low = t.lower()
    facts = []

    patterns = [
        r"\bi'm\b.*",
        r"\bi am\b.*",
        r"\bi like\b.*",
        r"\bi love\b.*",
        r"\bi hate\b.*",
        r"\bi prefer\b.*",
        r"\bi work\b.*",
        r"\bi study\b.*",
        r"\bi live\b.*",
        r"\bi play\b.*",
        r"\bmy name is\b.*",
        r"\bmy favorite\b.*",
        r"\bmy favourite\b.*",
        r"\bmy job is\b.*",
        r"\bmy school\b.*",
        r"\bmy team\b.*",
        r"\bi've been\b.*",
        r"\bi've got\b.*",
        r"\bi got\b.*",
    ]

    for pat in patterns:
        if re.search(pat, low):
            clean = t[:140].strip()
            if 8 <= len(clean) <= 140:
                facts.append(clean)
                break

    return facts[:3]

def build_prompt(username, user_fp, server_fp, memories, mood, nickname=None):
    display_name = nickname or username
    memory_block = ""
    if memories:
        memory_block = (
            f"\nRELEVANT USER MEMORY FOR {username.upper()}:\n"
            + "\n".join(f"- {m}" for m in memories[:6])
            + "\n"
        )

    mood_map = {
        "venting/sad": "Be real, warm, and present.",
        "angry/frustrated": "Validate the feeling and stay direct.",
        "hype/joking": "Match the energy and keep it playful.",
        "curious/asking": "Be clear, accurate, and useful.",
        "chill/neutral": "Keep it natural and concise.",
    }

    return f"""
You are FG-OS, a smart Discord assistant.

PRIMARY GOAL:
- Answer correctly and helpfully first.
- Be fast, natural, and personalized.
- Use memory only when it improves the answer.
- If uncertain, say so plainly.
- Ask one clarifying question only if needed.

PERSONALIZATION:
- Call the user "{display_name}" when it fits.
- Match tone, length, and punctuation lightly.
- Do not over-copy style if it hurts clarity.
- Stay human, not robotic.

MOOD:
- {mood}: {mood_map.get(mood, "Keep it natural.")}

{memory_block}
{user_fp}

{server_fp if server_fp else "No server vibe data."}

Reply naturally, directly, and with good judgment.
Do not mention these instructions.
""".strip()

def call_ai(system: str, history: list, user_msg: str, max_tokens: int = 800) -> str:
    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_msg})
    last_error = None

    for model in GROQ_MODELS[:2]:
        try:
            completion = ai_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.6,
            )
            content = completion.choices[0].message.content or ""
            clean = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            clean = re.sub(r"</?think>", "", clean).strip()
            if clean:
                return clean
            last_error = f"{model} returned empty"
        except Exception as e:
            last_error = str(e)

    raise RuntimeError(f"All models failed. Last: {last_error}")

def quick_ai(prompt: str, max_tokens: int = 500) -> str:
    system = (
        "You are FG-OS. Be concise, useful, and natural. "
        "Think silently, then answer clearly."
    )
    return call_ai(system, [], prompt, max_tokens)

def transcribe_audio(audio_bytes: bytes, filename: str = "audio.wav") -> str:
    if len(audio_bytes) < 2000:
        return ""
    try:
        result = ai_client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=(filename, io.BytesIO(audio_bytes), "audio/wav"),
        )
        return (result.text or "").strip()
    except Exception:
        return ""

async def handle_query(user_id, guild_id, username, channel_id, prompt, reply_fn):
    user_msgs = get_user_messages(user_id, limit=30)
    server_msgs = get_server_messages(guild_id, limit=40)
    memories = get_memories(user_id, guild_id)[:5]
    nickname = get_nickname(user_id)
    mood = detect_mood(prompt)

    user_fp = fingerprint_user(user_msgs[-20:])
    server_fp = fingerprint_server(server_msgs[-12:])
    system = build_prompt(username, user_fp, server_fp, memories, mood, nickname)
    history = get_context(channel_id, max_turns=4)

    response = call_ai(system, history, prompt, max_tokens=700)

    save_context(channel_id, "user", prompt)
    save_context(channel_id, "assistant", response)

    for fact in extract_facts(prompt):
        save_memory(user_id, guild_id, fact)

    for chunk in [response[i:i+1990] for i in range(0, len(response), 1990)]:
        await reply_fn(chunk)

try:
    SinkBase = discord.AudioSink
except AttributeError:
    SinkBase = object

class FGOSSink(SinkBase):
    SAMPLE_RATE = 48000
    CHANNELS = 2
    BYTES_PER_SAMPLE = 2
    FLUSH_BYTES = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * 20

    def __init__(self, guild_id: str, text_channel, bot_ref):
        if SinkBase is not object:
            super().__init__()
        self.guild_id = guild_id
        self.text_channel = text_channel
        self.bot = bot_ref
        self._buffers = {}

    def write(self, user, data):
        if user is None:
            return
        uid = user.id
        self._buffers.setdefault(uid, bytearray()).extend(data.pcm)
        if len(self._buffers[uid]) >= self.FLUSH_BYTES:
            audio_copy = bytes(self._buffers[uid])
            self._buffers[uid].clear()
            asyncio.create_task(self._process(user, audio_copy))

    async def _process(self, user, pcm_bytes: bytes):
        try:
            wav_bytes = pcm_to_wav(pcm_bytes, self.SAMPLE_RATE, self.CHANNELS)
            loop = asyncio.get_event_loop()
            transcript = await loop.run_in_executor(
                None, transcribe_audio, wav_bytes, f"user_{user.id}.wav"
            )
            if not transcript or len(transcript.split()) < 3:
                return

            username = user.display_name if hasattr(user, "display_name") else str(user)
            save_voice_transcript(str(user.id), self.guild_id, username, transcript)

            session = voice_sessions.get(self.guild_id)
            if session:
                uid_str = str(user.id)
                session["transcripts"].setdefault(uid_str, [])
                session["transcripts"][uid_str].append(transcript)
                session["transcripts"][uid_str] = session["transcripts"][uid_str][-15:]

            if len(transcript.split()) >= 8 and random.random() < 0.25:
                await auto_roast_vc(self.guild_id, str(user.id), username, transcript, self.text_channel, user)
        except Exception:
            pass

    def cleanup(self):
        self._buffers.clear()

    @property
    def wants_opus(self):
        return False

def pcm_to_wav(pcm: bytes, rate: int = 48000, channels: int = 2) -> bytes:
    bits = 16
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, channels, rate,
        byte_rate, block_align, bits,
        b"data", data_size
    )
    return header + pcm

async def auto_roast_vc(guild_id, user_id, username, transcript, channel, member):
    msgs = get_user_messages(user_id, limit=15)
    msg_sample = "\n".join(f'  "{m}"' for m in msgs[-8:]) if msgs else "  (no text messages yet)"
    prompt = (
        f"Write a short, funny, not-cruel roast of {username} based on what they said in voice chat.\n\n"
        f"VOICE:\n{transcript}\n\n"
        f"TEXT STYLE:\n{msg_sample}\n\n"
        f"1-2 sentences. Specific. Gen Z. No slurs. No hate.\n"
    )
    try:
        roast = quick_ai(prompt, max_tokens=120)
        mention = member.mention if hasattr(member, "mention") else f"@{username}"
        await channel.send(f"🎤 {mention} {roast}")
    except Exception:
        pass

@tasks.loop(minutes=1)
async def check_reminders():
    for reminder_id, user_id, channel_id, text in get_due_reminders():
        try:
            channel = bot.get_channel(int(channel_id))
            if channel:
                await channel.send(f"⏰ <@{user_id}> reminder: **{text}**")
            mark_reminder_done(reminder_id)
        except Exception:
            pass

@tasks.loop(minutes=45)
async def conversation_starter():
    for channel_id in list(starter_channels):
        try:
            channel = bot.get_channel(int(channel_id))
            if not channel:
                continue
            guild_id = str(channel.guild.id) if channel.guild else "dm"
            server_msgs = get_server_messages(guild_id, limit=20)
            server_fp = fingerprint_server(server_msgs[-12:])
            prompt = (
                "Drop ONE casual conversation starter that fits this server. "
                "Short, natural, not cringe.\n\n"
                f"{server_fp}"
            )
            msg = quick_ai(prompt, max_tokens=60)
            await channel.send(msg[:500])
        except Exception:
            pass

@bot.event
async def setup_hook():
    await bot.tree.sync()

@bot.event
async def on_ready():
    if not check_reminders.is_running():
        check_reminders.start()
    if not conversation_starter.is_running():
        conversation_starter.start()
    print(f"FG-OS LIVE: {bot.user.name}")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    guild_id = str(member.guild.id)
    session = voice_sessions.get(guild_id)
    if not session:
        return
    if not getattr(before, "self_stream", False) and getattr(after, "self_stream", False):
        channel = session["channel"]
        msgs = get_user_messages(str(member.id), limit=10)
        vibe = "\n".join(msgs[-5:]) if msgs else ""
        prompt = (
            f"{member.display_name} just went LIVE on Discord. "
            f"React in 1 sentence. Short, funny, natural.\n\n{vibe}"
        )
        try:
            reaction = quick_ai(prompt, max_tokens=60)
            await channel.send(f"📺 {member.mention} {reaction}")
        except Exception:
            pass

@bot.hybrid_command(name="help", description="Show all FG-OS commands")
async def help_command(ctx):
    await ctx.defer()
    cmds = sorted([cmd for cmd in bot.commands if not cmd.hidden and cmd.name != "help"], key=lambda c: c.name)
    lines = ["**FG-OS Commands** — use `/command` or `!command`", ""]
    for cmd in cmds:
        lines.append(f"`/{cmd.name}` — {cmd.description or 'no description'}")
    content = "\n".join(lines)
    for chunk in [content[i:i+1900] for i in range(0, len(content), 1900)]:
        await ctx.send(chunk)

@bot.hybrid_command(name="sync", description="Force sync slash commands")
async def sync_cmd(ctx):
    await bot.tree.sync()
    await ctx.send("slash commands synced!")

@bot.hybrid_command(name="ask", description="Ask FG-OS anything")
async def ask_fg_os(ctx, *, question: str):
    await ctx.defer()
    try:
        guild_id = ctx.guild.id if ctx.guild else "dm"
        await handle_query(
            ctx.author.id,
            guild_id,
            ctx.author.display_name,
            ctx.channel.id,
            question,
            ctx.send
        )
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
    await ctx.send(f"```text\n{fp[:1800]}\n```", ephemeral=True)

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
    await ctx.send(f"```text\nWHAT I KNOW ABOUT YOU:\n{out}\n```", ephemeral=True)

@bot.hybrid_command(name="clearmemory", description="Wipe FG-OS's memory of you")
async def clear_memory(ctx):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    c.execute("DELETE FROM user_memory WHERE user_id=? AND guild_id=?", (str(ctx.author.id), guild_id))
    conn.commit()
    conn.close()
    await ctx.send("cleared everything i knew about u")

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
    c.execute(
        "SELECT hour, COUNT(*) as cnt FROM chat_logs WHERE user_id=? AND guild_id=? GROUP BY hour ORDER BY cnt DESC LIMIT 1",
        (str(target.id), guild_id)
    )
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
    c.execute(
        "SELECT username, COUNT(*) as cnt FROM chat_logs WHERE guild_id=? GROUP BY user_id ORDER BY cnt DESC LIMIT 10",
        (guild_id,)
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await ctx.send("no data yet, people gotta talk first")
        return

    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines = [f"{medals[i]} **{row[0]}** — {row[1]} messages" for i, row in enumerate(rows)]
    embed = discord.Embed(title="🏆 Server Leaderboard", description="\n".join(lines), color=0xFFD700)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="servervibe", description="AI reads the server's current mood")
async def servervibe(ctx):
    await ctx.defer()
    guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    server_msgs = get_server_messages(guild_id, limit=50)
    if len(server_msgs) < 5:
        await ctx.send("not enough messages logged yet")
        return
    sample = "\n".join(f"{u}: {m}" for u, m in server_msgs[-30:])
    prompt = (
        "Analyze the vibe of this Discord server in 3-4 casual sentences. "
        "Talk about energy, topics, and mood.\n\n"
        f"{sample}"
    )
    try:
        vibe = quick_ai(prompt, max_tokens=180)
        embed = discord.Embed(title="🌡️ Server Vibe Check", description=vibe, color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="summarize", description="Summarize the last N messages in this channel")
async def summarize(ctx, count: int = 20):
    await ctx.defer()
    count = max(5, min(count, 50))
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
        prompt = f"Summarize this Discord convo in 3-5 casual sentences. Key points only.\n\n{convo}"
        summary = quick_ai(prompt, max_tokens=180)
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
        server_fp = fingerprint_server(server_msgs[-12:])
        prompt = (
            "TLDR in 2-3 casual sentences. No bullets.\n\n"
            f"{server_fp}\n\nTEXT:\n{text[:2000]}"
        )
        result = quick_ai(prompt, max_tokens=120)
        await ctx.send(f"**TLDR:** {result}")
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="poll", description="AI generates a poll from your question")
async def poll(ctx, *, question: str):
    await ctx.defer()
    try:
        prompt = (
            f"Make a Discord poll for: '{question}'\n"
            "Return ONLY a numbered list of max 4 short options under 30 chars. No other text."
        )
        result = quick_ai(prompt, max_tokens=100)
        lines = [l.strip() for l in result.strip().split("\n") if l.strip()]
        options = [re.sub(r"^[\d]+[.)]\s*", "", l).strip() for l in lines]
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
            "Return ONLY JSON: {\"task\": \"...\", \"minutes\": 60}\n"
            "Convert time to minutes. Default 60. No other text."
        )
        result = quick_ai(prompt, max_tokens=60)
        match = re.search(r'\{.*?\}', result, re.DOTALL)
        if not match:
            await ctx.send("couldn't parse that — try 'remind me to X in 2 hours'", ephemeral=True)
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

@bot.hybrid_command(name="roast", description="Gen Z roast someone based on how they actually text")
async def roast(ctx, member: discord.Member):
    await ctx.defer()
    target_msgs = get_user_messages(member.id, limit=35)
    if len(target_msgs) < 5:
        await ctx.send(f"not enough msgs from {member.display_name} to roast them, they gotta talk more")
        return
    sample = "\n".join(target_msgs[-20:])
    prompt = (
        f"Write a funny, not-cruel roast of {member.display_name} using ONLY their actual messages.\n\n"
        f"MESSAGES:\n{sample}\n\n"
        f"2-3 sentences max. Specific. Natural. No hate."
    )
    try:
        roast_text = quick_ai(prompt, max_tokens=120)
        await ctx.send(f"🔥 {member.mention} {roast_text}")
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="impersonate", description="FG-OS responds as if it IS that person")
async def impersonate(ctx, member: discord.Member, *, situation: str):
    await ctx.defer()
    target_msgs = get_user_messages(member.id, limit=50)
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
    sig_words = [(w, cnt) for w, cnt in Counter(all_words).most_common(20) if w not in STOPWORDS and cnt > 1]
    sig_str = ", ".join(f'"{w}"({cnt}x)' for w, cnt in sig_words[:10]) or "not enough data"

    patterns = []
    if lc_ratio > 0.8:
        patterns.append("mostly lowercase")
    elif lc_ratio < 0.2:
        patterns.append("mostly normal capitalization")
    else:
        patterns.append("mixed capitalization")

    if no_punct > 0.7:
        patterns.append("rarely uses ending punctuation")
    elif no_punct < 0.3:
        patterns.append("uses punctuation consistently")

    if avg_len < 20:
        patterns.append("very short messages")
    elif avg_len < 45:
        patterns.append("short punchy messages")
    else:
        patterns.append("longer messages sometimes")

    if ellipsis_count > 3:
        patterns.append("uses ... a lot")
    if laugh_count > 5:
        patterns.append("uses lol/lmao/bruh a lot")
    if caps_words:
        patterns.append(f"caps emphasis: {', '.join(set(caps_words[:5]))}")

    sample_display = "\n".join(f'  "{m}"' for m in msgs[-15:])
    prompt = (
        f"You are responding as {member.display_name}. Keep the same overall vibe, not a perfect clone.\n\n"
        f"HARD STYLE NOTES:\n- " + "\n- ".join(patterns) +
        f"\n\nSIGNATURE WORDS: {sig_str}\n\n"
        f"THEIR MESSAGES:\n{sample_display}\n\n"
        f"SITUATION: {situation}\n\n"
        f"Write one response that sounds like them, but stays clear."
    )

    try:
        response = quick_ai(prompt, max_tokens=160)
        await ctx.send(f"**{member.display_name}:** {response}")
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="debate", description="Two AI personalities debate a topic")
async def debate(ctx, *, topic: str):
    await ctx.defer()
    try:
        prompt = (
            f"Two people debating: '{topic}'\n"
            "2 turns each. Label as Side A and Side B.\n"
            "Casual Discord tone. Real points. 1-2 sentences per turn."
        )
        result = quick_ai(prompt, max_tokens=250)
        embed = discord.Embed(title=f"⚔️ Debate: {topic}", description=result, color=0xED4245)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="starterchannel", description="Toggle auto conversation starters in this channel")
async def starterchannel(ctx):
    channel_id = str(ctx.channel.id)
    if channel_id in starter_channels:
        starter_channels.discard(channel_id)
        await ctx.send("convo starters OFF")
    else:
        starter_channels.add(channel_id)
        await ctx.send("convo starters ON — dropping one every ~45 min")

@bot.hybrid_command(name="joinvc", description="Join your VC and start listening for trash talk material")
async def joinvc(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("you're not in a VC")
        return
    await ctx.defer()
    guild_id = str(ctx.guild.id)

    existing = voice_sessions.pop(guild_id, None)
    if existing:
        try:
            if hasattr(existing["vc"], "stop_listening"):
                existing["vc"].stop_listening()
            await existing["vc"].disconnect()
        except Exception:
            pass

    try:
        vc = await ctx.author.voice.channel.connect()
    except Exception as e:
        await ctx.send(f"couldn't connect to VC: {e}")
        return

    sink = None
    joined_message = f"👂 joined **{ctx.author.voice.channel.name}**"

    if hasattr(vc, "listen"):
        sink = FGOSSink(guild_id, ctx.channel, bot)
        vc.listen(sink)
        joined_message += " — listening and collecting receipts"
    else:
        joined_message += " — joined, but this discord.py version does not support voice receive"

    voice_sessions[guild_id] = {
        "vc": vc,
        "sink": sink,
        "channel": ctx.channel,
        "transcripts": {}
    }

    await ctx.send(joined_message)

@bot.hybrid_command(name="leavevc", description="Make FG-OS leave the voice channel")
async def leavevc(ctx):
    guild_id = str(ctx.guild.id)
    session = voice_sessions.pop(guild_id, None)
    if not session:
        await ctx.send("i'm not in a VC")
        return
    try:
        if hasattr(session["vc"], "stop_listening"):
            session["vc"].stop_listening()
        await session["vc"].disconnect()
    except Exception:
        pass
    await ctx.send("left the VC, saved all the receipts tho")

@bot.hybrid_command(name="trashtalk", description="Trash talk someone using their VC speech + message history")
async def trashtalk(ctx, member: discord.Member):
    await ctx.defer()
    guild_id = str(ctx.guild.id)
    session = voice_sessions.get(guild_id)
    session_transcripts = []
    if session:
        session_transcripts = session["transcripts"].get(str(member.id), [])
    db_transcripts = get_voice_transcripts(str(member.id), guild_id, limit=10)
    all_vc = db_transcripts + session_transcripts
    msgs = get_user_messages(member.id, limit=20)
    if not all_vc and len(msgs) < 3:
        await ctx.send(f"no receipts on {member.display_name} yet — need them to talk in VC or send more messages")
        return
    vc_sample = "\n".join(f'  "{t}"' for t in all_vc[-8:]) if all_vc else "  (no VC data yet)"
    msg_sample = "\n".join(f'  "{m}"' for m in msgs[-10:]) if msgs else "  (no text messages yet)"
    prompt = (
        f"Write a short, funny, not-mean roast of {member.display_name}.\n\n"
        f"VOICE:\n{vc_sample}\n\n"
        f"TEXT:\n{msg_sample}\n\n"
        f"2-4 sentences max. Specific. Dry. Natural."
    )
    try:
        trash = quick_ai(prompt, max_tokens=140)
        await ctx.send(f"🗑️ {member.mention} {trash}")
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="vctranscript", description="See what FG-OS heard from someone in VC")
async def vctranscript(ctx, member: discord.Member = None):
    await ctx.defer(ephemeral=True)
    guild_id = str(ctx.guild.id)
    target = member or ctx.author
    session = voice_sessions.get(guild_id)
    session_transcripts = []
    if session:
        session_transcripts = session["transcripts"].get(str(target.id), [])
    db_transcripts = get_voice_transcripts(str(target.id), guild_id, limit=10)
    all_t = db_transcripts + session_transcripts
    if not all_t:
        await ctx.send(f"no VC transcript for {target.display_name} yet", ephemeral=True)
        return
    out = "\n".join(f"[{i+1}] {t}" for i, t in enumerate(all_t[-10:]))
    await ctx.send(f"```text\n🎤 VC TRANSCRIPT ({target.display_name}):\n{out[:1800]}\n```", ephemeral=True)

@bot.event
async def setup_hook():
    await bot.tree.sync()

@bot.event
async def on_ready():
    if not check_reminders.is_running():
        check_reminders.start()
    if not conversation_starter.is_running():
        conversation_starter.start()
    print(f"FG-OS LIVE: {bot.user.name}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content = (message.content or "").strip()
    guild_id = str(message.guild.id) if message.guild else "dm"

    if content and not content.startswith("/") and not content.startswith("!"):
        log_message(message.author.id, guild_id, message.author.display_name, content)
        for fact in extract_facts(content):
            save_memory(message.author.id, guild_id, fact)

    if bot.user.mentioned_in(message):
        async with message.channel.typing():
            try:
                prompt = re.sub(rf"<@!?{bot.user.id}>", "", message.content or "").strip() or "yo"
                await handle_query(
                    message.author.id,
                    guild_id,
                    message.author.display_name,
                    message.channel.id,
                    prompt,
                    message.reply
                )
            except Exception as e:
                await message.reply(f"⚠️ Error: {e}")

    await bot.process_commands(message)

bot.run(DISCORD_TOKEN)