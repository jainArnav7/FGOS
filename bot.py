# ============================================================
# FG-OS Discord Bot — FIXED Voice Receive + Smarter AI
#
# pip install "discord.py[voice]" discord-ext-voice-recv PyNaCl
#             openai python-dotenv edge-tts
# ffmpeg must be installed and on PATH
# ============================================================

import discord
from fastapi import FastAPI
import uvicorn
import threading
from discord.ext import commands, tasks
from discord import app_commands
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
import tempfile
import edge_tts
from collections import Counter, defaultdict
from dotenv import load_dotenv
from battleship import create_game, get_game, get_game_by_id, end_game
from core.ai import ask_ai
from core.birthday import (
    get_all_birthday_profiles,
    get_birthday_profile,
    get_interview_progress,
    has_birthday_day_reply,
    note_birthday_day_reply,
)
from core.birthday_commands import birthday_group
from core.birthday_tasks import birthday_clock, handle_birthday_dm_answer

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

def run_api():
    uvicorn.run(app, host="0.0.0.0", port=8000)

threading.Thread(target=run_api, daemon=True).start()

async def safe_defer(ctx, ephemeral: bool = False):
    try:
        await ctx.defer(ephemeral=ephemeral)
    except Exception as e:
        text = str(e)
        if isinstance(e, discord.NotFound) or 'Unknown interaction' in text or 'Unknown interaction' in getattr(e, 'text', ''):
            return
        # Some hybrid/autonomous contexts may not support defer.
        if 'interaction' in text.lower() and 'none' in text.lower():
            return
        raise

async def safe_send(destination, content: str = None, **kwargs):
    try:
        if content is None:
            return await destination.send(**kwargs)

        if isinstance(content, str) and len(content) > 1990:
            buffer = io.BytesIO(content.encode('utf-8'))
            filename = kwargs.pop('filename', 'board.txt')
            return await destination.send(file=discord.File(buffer, filename=filename), **kwargs)

        return await destination.send(content, **kwargs)
    except Exception as e:
        # Fallback: some Context/Interaction objects may no longer have a valid interaction
        try:
            # If destination has a channel attribute, send there
            channel = getattr(destination, 'channel', None) or getattr(destination, 'guild', None)
            if channel and hasattr(channel, 'send'):
                if isinstance(content, str) and len(content) > 1990:
                    buffer = io.BytesIO(content.encode('utf-8'))
                    filename = kwargs.pop('filename', 'board.txt')
                    return await channel.send(file=discord.File(buffer, filename=filename), **kwargs)
                return await channel.send(content, **kwargs)
        except Exception:
            pass
        raise

# Recently handled message IDs to avoid duplicate replies from on_message
recently_handled_messages = set()

# ── voice-recv import (install: pip install discord-ext-voice-recv) ──
# NOTE: Voice receive requires Opus codec which is complex to set up on Windows.
# Voice features are OPTIONAL. The bot works fine without voice input.
# To enable: 
#   1. Install ffmpeg with opus support: https://ffmpeg.org/download.html
#   2. Set ENABLE_VOICE_RECV = True below
#   3. Run: pip install discord-ext-voice-recv

ENABLE_VOICE_RECV = True  # Change from False to True

try:
    from discord.ext.voice_recv import VoiceRecvClient, BasicSink
    VOICE_RECV_AVAILABLE = ENABLE_VOICE_RECV
    if ENABLE_VOICE_RECV:
        print("[VC] Voice receive support enabled")
    else:
        print("[VC] Voice receive disabled (set ENABLE_VOICE_RECV=True if opus codec is installed)")
except ImportError:
    VOICE_RECV_AVAILABLE = False
    if ENABLE_VOICE_RECV:
        print("⚠️  discord-ext-voice-recv not installed.")
        print("    Run: pip install discord-ext-voice-recv")
    else:
        print("[VC] Voice receive not installed (optional)")

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GROQ_KEY      = os.getenv("GROQ_API_KEY")

# ── Try to load Opus codec from FFmpeg ──
try:
    import discord
    if not discord.opus.is_loaded():
        # Try to load opus from common locations on Windows
        opus_paths = [
            'opus.dll',                                    # Current dir or PATH
            'libopus-0.dll',                              # Alternative name
            r'C:\Program Files\ffmpeg\bin\opus.dll',      # FFmpeg default
            r'C:\Program Files (x86)\ffmpeg\bin\opus.dll',# 32-bit FFmpeg
        ]
        
        loaded = False
        for path in opus_paths:
            try:
                if os.path.exists(path) or '\\' not in path:  # Try PATH first
                    discord.opus.load_opus(path)
                    if discord.opus.is_loaded():
                        print(f"✓ Opus codec loaded from: {path}")
                        loaded = True
                        break
            except Exception:
                pass
        
        if not loaded and discord.opus.is_loaded():
            print("✓ Opus codec loaded (system default)")
        elif not loaded:
            print("⚠️  Opus codec not found - voice input will not work")
            print("   Ensure FFmpeg is in PATH or set ENABLE_VOICE_RECV = False")
except Exception as e:
    print(f"Opus codec check: {e}")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states    = True
intents.members         = True

# ── Use VoiceRecvClient so the bot can actually RECEIVE audio ──
if VOICE_RECV_AVAILABLE:
    bot = commands.Bot(
        command_prefix="!",
        intents=intents,
        help_command=None,
    )
else:
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODELS  — smarter primary, tighter fallback list
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GROQ_MODELS = [
    "llama-3.3-70b-versatile",   # primary — smartest & fastest on Groq
    "llama-3.1-70b-versatile",   # solid fallback
    "llama3-70b-8192",           # last resort
]
WHISPER_MODEL = "whisper-large-v3-turbo"

ai_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_KEY,
)

# guild_id -> {vc, sink, channel, transcripts, speaking_lock}
voice_sessions: dict = {}
starter_channels: set = set()

# Per-guild cooldown so the bot doesn't respond to every single sentence
vc_response_cooldown: dict = {}   # guild_id -> datetime of last VC response
VC_COOLDOWN_SECONDS = 8           # wait at least 8s between VC AI responses

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EDGE-TTS VOICE MAP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MOOD_VOICE = {
    "hype/joking":      "en-US-GuyNeural",
    "angry/frustrated": "en-US-DavisNeural",
    "venting/sad":      "en-US-JennyNeural",
    "curious/asking":   "en-US-AriaNeural",
    "chill/neutral":    "en-US-ChristopherNeural",
}

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
        channel_id TEXT, user_id TEXT, role TEXT, content TEXT,
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
    c.execute('''CREATE TABLE IF NOT EXISTS voice_transcripts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT, guild_id TEXT, username TEXT,
        transcript TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

# ── Logging ────────────────────────────────────────
def log_message(user_id, guild_id, username, content):
    text = (content or "").strip()
    if len(text) < 3:
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO chat_logs (user_id, guild_id, username, message_content, hour) VALUES (?,?,?,?,?)",
        (str(user_id), str(guild_id), username, text, datetime.datetime.now().hour)
    )
    conn.commit()
    conn.close()

def get_user_messages(user_id, limit=50):
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

# ── Per-user conversation context ─────────────────
def get_context(channel_id, user_id, max_turns=6):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM conversation_ctx WHERE channel_id=? AND user_id=? ORDER BY timestamp DESC LIMIT ?",
        (str(channel_id), str(user_id), max_turns * 2)
    )
    rows = c.fetchall()
    conn.close()
    rows.reverse()
    return [{"role": r[0], "content": r[1]} for r in rows]

def save_context(channel_id, user_id, role, content, max_keep=12):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO conversation_ctx (channel_id, user_id, role, content) VALUES (?,?,?,?)",
        (str(channel_id), str(user_id), role, content)
    )
    c.execute('''DELETE FROM conversation_ctx WHERE channel_id=? AND user_id=? AND id NOT IN (
        SELECT id FROM conversation_ctx WHERE channel_id=? AND user_id=?
        ORDER BY timestamp DESC LIMIT ?
    )''', (str(channel_id), str(user_id), str(channel_id), str(user_id), max_keep))
    conn.commit()
    conn.close()

# ── Memory ─────────────────────────────────────────
def save_memory(user_id, guild_id, fact):
    fact = (fact or "").strip()
    if not fact or len(fact) < 8:
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
        c.execute('''DELETE FROM user_memory WHERE user_id=? AND guild_id=? AND id NOT IN (
            SELECT id FROM user_memory WHERE user_id=? AND guild_id=?
            ORDER BY timestamp DESC LIMIT 20
        )''', (str(user_id), str(guild_id), str(user_id), str(guild_id)))
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

# ── Nicknames ──────────────────────────────────────
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
    c.execute("INSERT OR REPLACE INTO user_nicknames (user_id, nickname) VALUES (?,?)",
              (str(user_id), nickname))
    conn.commit()
    conn.close()

# ── Reminders ──────────────────────────────────────
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
    c.execute("SELECT id, user_id, channel_id, reminder_text FROM reminders WHERE due_at<=? AND done=0", (now,))
    rows = c.fetchall()
    conn.close()
    return rows

def mark_reminder_done(rid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE reminders SET done=1 WHERE id=?", (rid,))
    conn.commit()
    conn.close()

# ── Voice transcripts ──────────────────────────────
def save_voice_transcript(user_id, guild_id, username, transcript):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO voice_transcripts (user_id, guild_id, username, transcript) VALUES (?,?,?,?)",
              (str(user_id), str(guild_id), username, transcript))
    c.execute('''DELETE FROM voice_transcripts WHERE user_id=? AND guild_id=? AND id NOT IN (
        SELECT id FROM voice_transcripts WHERE user_id=? AND guild_id=?
        ORDER BY timestamp DESC LIMIT 50
    )''', (str(user_id), str(guild_id), str(user_id), str(guild_id)))
    conn.commit()
    conn.close()

def get_voice_transcripts(user_id, guild_id, limit=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT transcript FROM voice_transcripts WHERE user_id=? AND guild_id=? ORDER BY timestamp DESC LIMIT ?",
              (str(user_id), str(guild_id), limit))
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    rows.reverse()
    return rows

init_db()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STYLE FINGERPRINTING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STOPWORDS = {
    "the","a","an","is","it","in","on","at","to","of","and","i","you","my","me",
    "he","she","they","we","do","be","was","are","for","that","this","have","not",
    "with","so","but","or","if","its","im","dont","can","just","ur","u","r","ok",
    "yeah","yes","no","get","go","got","ill","ive","id","thats","what","there",
    "here","why","how","when","where","who","all","any","some","more","very","really",
}

EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0\U000024C2-\U0001F251]+",
    flags=re.UNICODE
)

def build_user_fingerprint(messages: list, username: str) -> dict:
    if not messages:
        return {"username": username, "enough_data": False}
    n = len(messages)
    avg_len    = sum(len(m) for m in messages) / n
    lc_ratio   = sum(1 for m in messages if m == m.lower()) / n
    no_punct   = sum(1 for m in messages if not any(p in m for p in ".!?")) / n
    ellipsis_n = sum(1 for m in messages if "..." in m)
    laugh_n    = sum(1 for m in messages if any(w in m.lower() for w in
                   ["lol","lmao","lmfao","💀","😭","haha","bruh","fr","kek"]))
    caps_words = [w for m in messages for w in m.split() if w.isupper() and len(w) > 1]
    q_marks    = sum(1 for m in messages if "?" in m)
    excl       = sum(1 for m in messages if "!" in m)
    all_emojis = []
    for m in messages:
        all_emojis.extend(EMOJI_RE.findall(m))
    emoji_counts = Counter(all_emojis)
    top_emojis   = [e for e, _ in emoji_counts.most_common(5)]
    all_words  = [w.lower().strip(".,!?'\"") for m in messages for w in m.split() if len(w) >= 2]
    word_freq  = Counter(all_words)
    sig_words  = [(w, c) for w, c in word_freq.most_common(40) if w not in STOPWORDS and c >= 2]
    fillers    = [w for m in messages for w in m.split()
                  if re.match(r'^([a-zA-Z])\1{2,}$', w) or w.lower() in
                  {"omg","omgg","omfg","istg","ngl","ong","ight","aight","fasho","fax","periodt","slay"}]
    filler_counts = Counter(f.lower() for f in fillers)
    top_fillers   = [f for f, _ in filler_counts.most_common(6)]
    starters      = [m.split()[0].lower() for m in messages if m.split()]
    starter_counts = Counter(starters)
    top_starters  = [s for s, c in starter_counts.most_common(5) if c >= 2 and s not in {"i","the","a","so","but"}]
    return {
        "username":    username,
        "enough_data": n >= 5,
        "sample_count": n,
        "avg_len":     avg_len,
        "lc_ratio":    lc_ratio,
        "no_punct":    no_punct,
        "ellipsis":    ellipsis_n,
        "laugh":       laugh_n,
        "caps_words":  list(set(caps_words[:8])),
        "top_emojis":  top_emojis,
        "sig_words":   sig_words[:15],
        "top_fillers": top_fillers,
        "top_starters": top_starters,
        "q_ratio":     q_marks / n,
        "excl_ratio":  excl / n,
        "raw_sample":  messages[-12:],
    }

def fingerprint_to_prompt(fp: dict) -> str:
    if not fp.get("enough_data"):
        return f"No style data for {fp['username']} yet. Be natural."
    u = fp["username"]
    rules = []
    if fp["lc_ratio"] > 0.80:
        rules.append(f"Write almost entirely lowercase — {u} almost never capitalizes.")
    elif fp["lc_ratio"] > 0.55:
        rules.append("Mixed caps — follow what their messages show.")
    else:
        rules.append(f"Use normal capitalization like {u} does.")
    if fp["no_punct"] > 0.70:
        rules.append("Skip ending punctuation — no periods unless they use them.")
    elif fp["no_punct"] < 0.30:
        rules.append("Use punctuation consistently like they do.")
    if fp["avg_len"] < 20:
        rules.append("Keep responses very short — 1 to 5 words is normal for them.")
    elif fp["avg_len"] < 45:
        rules.append("Keep it short and punchy — they don't write paragraphs.")
    elif fp["avg_len"] > 100:
        rules.append("Longer responses are okay — they write detailed messages.")
    if fp["top_emojis"]:
        rules.append(f"Only use these emojis: {' '.join(fp['top_emojis'])} — max 1 per message.")
    else:
        rules.append("Never use emojis — they don't use them.")
    if fp["top_fillers"]:
        rules.append(f"They say things like: {', '.join(fp['top_fillers'])} — use naturally.")
    if fp["sig_words"]:
        top = ", ".join(f'"{w}"({c}x)' for w, c in fp["sig_words"][:10])
        rules.append(f"Their most-used words: {top}")
    if fp["caps_words"]:
        rules.append(f"They use ALL CAPS for emphasis on: {', '.join(fp['caps_words'][:5])}")
    if fp["ellipsis"] > 3:
        rules.append("They use ... a lot to trail off.")
    if fp["laugh"] > 4:
        rules.append("They react with lol/lmao/bruh/💀 frequently.")
    sample = "\n".join(f"  {m}" for m in fp["raw_sample"])
    rules_text = "\n".join(f"• {r}" for r in rules)
    return f"""━━ STYLE PROFILE FOR {u.upper()} ━━
{rules_text}

THEIR ACTUAL MESSAGES:
{sample}"""

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
    low = text.lower().strip()
    patterns = [
        r"\bi(?:'m| am)\b.{4,}",
        r"\bi (?:like|love|hate|prefer|play|work|study|live|own|watch|use|listen)\b.{3,}",
        r"\bmy (?:name|age|job|school|team|favorite|favourite|pet|car|hobby|sister|brother|mom|dad)\b.{3,}",
        r"\bi've (?:been|got|done|played|watched|worked).{3,}",
    ]
    for pat in patterns:
        m = re.search(pat, low)
        if m:
            snippet = text[m.start():m.start()+140].strip()
            if 8 <= len(snippet) <= 140:
                return [snippet]
    return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SYSTEM PROMPT — smarter, more knowledgeable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def build_prompt(username, fp_text, server_fp, memories, mood, nickname=None, is_voice=False):
    display = nickname or username
    memory_block = ""
    if memories:
        memory_block = (
            f"\nTHINGS YOU REMEMBER ABOUT {username.upper()}:\n"
            + "\n".join(f"- {m}" for m in memories[:6]) + "\n"
        )

    mood_guide = {
        "venting/sad":      "Be warm and real. Don't fix it, just acknowledge.",
        "angry/frustrated": "Validate and match the energy. Don't lecture.",
        "hype/joking":      "Match the hype. Be quick and fun.",
        "curious/asking":   "Give the REAL, accurate, thorough answer. Think step by step.",
        "chill/neutral":    "Keep it easy and natural.",
    }.get(mood, "Keep it natural.")

    voice_note = ""
    if is_voice:
        voice_note = """
═══ VOICE MODE ═══
You are speaking out loud in a Discord voice channel. Keep replies SHORT — 1 to 3 sentences max.
No markdown, no bullet points, no emojis. Just natural spoken sentences.
If someone asks a question, answer it conversationally and directly.
"""

    return f"""You are FG-OS, a smart Discord AI assistant with deep knowledge across many topics.

═══ KNOWLEDGE & ACCURACY ═══
You have broad, reliable knowledge in:
- Science, math, coding, history, geography, current events (up to early 2025)
- Pop culture, music, sports, gaming, anime, movies, TV
- Relationships, mental health, life advice
- Slang, memes, Gen Z/millennial culture

RULES:
- Answer confidently when you know. Don't hedge with "as an AI I can't..." — just answer.
- If genuinely unsure, say so in the person's style ("idk tbh" not "I cannot determine...")
- For factual/how-to questions: be accurate and thorough enough to actually help.
- For opinion/advice: be genuine and specific to their situation.
- Slang: understand context. "bricked up" = aroused. "cooked" = in trouble/exhausted.
  "no cap" = for real. "slay" = did well. Use and understand slang correctly.
- Never make up facts. Never pad responses with filler.
- Math: work it out step by step before answering.
- Coding: give working code, not pseudocode, unless they ask otherwise.

═══ PERSONALITY ═══
You're sharp, funny, and real. You roast when appropriate, hype people up when needed,
give actual advice when asked. You're not a corporate AI — you're their smart friend.

MOOD: {mood} → {mood_guide}
{memory_block}
{voice_note}
═══ STYLE (for {username.upper()} ONLY) ═══
{fp_text}

{server_fp if server_fp else ""}

Call them "{display}"."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AI CALLS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def call_ai(system: str, history: list, user_msg: str, max_tokens: int = 800) -> str:
    class _UserProxy:
        def __init__(self, prompt: str):
            self.id = None
            self.name = "Discord"
            self.prompt = prompt

    try:
        return ask_ai(_UserProxy(user_msg), system_prompt=system, history=history, max_tokens=max_tokens)
    except Exception as e:
        print(f"AI routing error: {e}")
        raise

def quick_ai(prompt: str, max_tokens: int = 500) -> str:
    system = "You are FG-OS. Be concise, accurate, and natural. Answer directly without corporate hedging."
    try:
        return call_ai(system, [], prompt, max_tokens)
    except Exception:
        return "AI is currently unavailable."

def transcribe_audio(audio_bytes: bytes, filename: str = "audio.wav") -> str:
    """Transcribe audio bytes with validation and error handling."""
    if not audio_bytes or len(audio_bytes) < 2000:
        return ""
    try:
        # Validate WAV header
        if not audio_bytes.startswith(b'RIFF'):
            print(f"Invalid WAV header in {filename}")
            return ""
        
        result = ai_client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=(filename, io.BytesIO(audio_bytes), "audio/wav"),
        )
        text = (result.text or "").strip()
        return text if text else ""
    except Exception as e:
        print(f"Whisper error ({filename}): {e}")
        return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TTS — edge-tts → ffmpeg → VC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def speak_in_vc(vc: discord.VoiceClient, text: str, mood: str = "chill/neutral"):
    if not vc or not vc.is_connected():
        return

    voice = MOOD_VOICE.get(mood, "en-US-ChristopherNeural")

    # Strip markdown/emojis so TTS sounds natural
    tts_text = re.sub(r'[*_`~>#]', '', text)
    tts_text = EMOJI_RE.sub('', tts_text).strip()
    # Remove Discord mentions
    tts_text = re.sub(r'<@!?\d+>', '', tts_text).strip()
    if not tts_text:
        return

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp_path = f.name

        communicate = edge_tts.Communicate(tts_text, voice)
        await communicate.save(tmp_path)

        # Wait for current audio to finish before speaking
        while vc.is_playing():
            await asyncio.sleep(0.3)

        def after_play(error):
            if error:
                print(f"TTS playback error: {error}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        vc.play(discord.FFmpegPCMAudio(tmp_path), after=after_play)
    except Exception as e:
        print(f"TTS error: {e}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SHARED CHAT HANDLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def handle_query(user_id, guild_id, username, channel_id, prompt,
                       reply_fn, voice_client=None, is_voice=False):
    user_msgs   = get_user_messages(user_id, limit=50)
    server_msgs = get_server_messages(guild_id, limit=40)
    memories    = get_memories(user_id, guild_id)
    nickname    = get_nickname(user_id)
    mood        = detect_mood(prompt)

    fp_dict   = build_user_fingerprint(user_msgs, username)
    fp_text   = fingerprint_to_prompt(fp_dict)
    server_fp = fingerprint_server(server_msgs)

    system  = build_prompt(username, fp_text, server_fp, memories, mood, nickname, is_voice=is_voice)
    history = get_context(channel_id, user_id)

    # Voice replies should be shorter
    max_tok = 120 if is_voice else 800

    response = call_ai(system, history, prompt, max_tokens=max_tok)

    save_context(channel_id, user_id, "user", prompt)
    save_context(channel_id, user_id, "assistant", response)

    for fact in extract_facts(prompt):
        save_memory(user_id, guild_id, fact)

    if not is_voice:
        for chunk in [response[i:i+1990] for i in range(0, len(response), 1990)]:
            await reply_fn(chunk)

    if voice_client and voice_client.is_connected():
        await speak_in_vc(voice_client, response, mood)

    return response


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# VOICE SINK  — FIXED to use discord-ext-voice-recv
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def pcm_to_wav(pcm: bytes, rate: int = 48000, channels: int = 2) -> bytes:
    """Convert raw PCM bytes to WAV format with validation."""
    if not pcm or len(pcm) == 0:
        raise ValueError("PCM data is empty")
    
    bits        = 16
    byte_rate   = rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size   = len(pcm)
    
    # Validate PCM size is multiple of frame size
    frame_size = channels * bits // 8
    if data_size % frame_size != 0:
        raise ValueError(f"PCM size {data_size} not multiple of frame size {frame_size}")
    
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, channels, rate,
        byte_rate, block_align, bits,
        b"data", data_size
    )
    return header + pcm


if VOICE_RECV_AVAILABLE:
    class FGOSSink(BasicSink):
        """
        Receives per-user PCM audio from discord-ext-voice-recv,
        transcribes it with Whisper, and responds via TTS.
        """
        SAMPLE_RATE       = 48000
        CHANNELS          = 2
        BYTES_PER_SAMPLE  = 2
        # Flush after ~3 seconds of audio per user
        FLUSH_BYTES = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * 3
        # Minimum valid packet size (avoid noise)
        MIN_PACKET_SIZE = 480  # ~10ms at 48kHz

        def __init__(self, guild_id: str, text_channel, bot_ref):
            super().__init__(self._on_audio)
            self.guild_id     = guild_id
            self.text_channel = text_channel
            self.bot          = bot_ref
            self._buffers: dict = {}
            self._silence_tasks: dict = {}  # user_id -> asyncio.Task
            self._processing: dict = {}  # user_id -> is_processing flag

        def _on_audio(self, user, data):
            """Called by voice-recv for every audio packet."""
            try:
                if user is None or user.bot:
                    return
                if data is None or not hasattr(data, 'pcm') or not data.pcm:
                    return
                
                # Validate PCM data length (must be multiple of CHANNELS * BYTES_PER_SAMPLE)
                if len(data.pcm) % (self.CHANNELS * self.BYTES_PER_SAMPLE) != 0:
                    print(f"[VC] Warning: PCM data size invalid for {user}: {len(data.pcm)} bytes")
                    return
                
                # Ignore packets that are too small (noise/artifacts)
                if len(data.pcm) < self.MIN_PACKET_SIZE:
                    return
                    
                uid = user.id
                self._buffers.setdefault(uid, bytearray()).extend(data.pcm)

                # Cancel existing silence timer and restart it
                if uid in self._silence_tasks and self._silence_tasks[uid]:
                    try:
                        self._silence_tasks[uid].cancel()
                    except Exception:
                        pass

                # Schedule a flush after 1.2 seconds of silence — thread-safe
                loop = self.bot.loop
                task = loop.call_later(1.2, lambda uid=uid, user=user: asyncio.run_coroutine_threadsafe(
                    self._flush(user), loop
                ))
                self._silence_tasks[uid] = task
            except Exception as e:
                print(f"[VC] Error in _on_audio for {user}: {e}")

        async def _flush(self, user):
            """Called after a user stops speaking for 1.2 seconds."""
            try:
                uid = user.id
                self._silence_tasks.pop(uid, None)
                
                buf = self._buffers.pop(uid, None)
                if not buf or len(buf) < self.FLUSH_BYTES // 10:
                    return  # too short, ignore (breathing / background noise)

                # Check if already processing to prevent duplicate transcriptions
                if self._processing.get(uid, False):
                    return
                
                self._processing[uid] = True
                try:
                    audio_copy = bytes(buf)
                    await self._process(user, audio_copy)
                finally:
                    self._processing[uid] = False
            except Exception as e:
                print(f"[VC] Error in _flush for {user}: {e}")

        async def _process(self, user, pcm_bytes: bytes):
            try:
                # Validate PCM data before processing
                if not pcm_bytes or len(pcm_bytes) == 0:
                    return
                
                # Ensure PCM is multiple of frame size
                frame_size = self.CHANNELS * self.BYTES_PER_SAMPLE
                if len(pcm_bytes) % frame_size != 0:
                    print(f"[VC] Skipping invalid PCM for {user}: size {len(pcm_bytes)} not multiple of {frame_size}")
                    return
                
                try:
                    wav_bytes  = pcm_to_wav(pcm_bytes)
                except Exception as wav_err:
                    print(f"[VC] WAV conversion error for {user}: {wav_err}")
                    return
                
                loop       = asyncio.get_event_loop()
                try:
                    transcript = await asyncio.wait_for(
                        loop.run_in_executor(None, transcribe_audio, wav_bytes, f"user_{user.id}.wav"),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    print(f"[VC] Transcription timeout for {user}")
                    return

                # Filter out empty / noise transcripts
                if not transcript or len(transcript.split()) < 2:
                    return
                # Filter common Whisper hallucinations on silence
                if transcript.lower().strip() in {
                    "you", "thank you", "thanks", ".", "..", "...", "okay", "ok",
                    "um", "uh", "hmm", "bye", "goodbye", "thanks for watching"
                }:
                    return

                username = getattr(user, "display_name", str(user))
                print(f"[VC] {username}: {transcript}")

                save_voice_transcript(str(user.id), self.guild_id, username, transcript)

                session = voice_sessions.get(self.guild_id)
                if not session:
                    return

                uid_str = str(user.id)
                session["transcripts"].setdefault(uid_str, [])
                session["transcripts"][uid_str].append(transcript)
                session["transcripts"][uid_str] = session["transcripts"][uid_str][-15:]

                # ── Cooldown check — avoid spam responses ──
                now = datetime.datetime.now()
                last_resp = vc_response_cooldown.get(self.guild_id)
                if last_resp and (now - last_resp).total_seconds() < VC_COOLDOWN_SECONDS:
                    return
                vc_response_cooldown[self.guild_id] = now

                # ── Don't respond while the bot itself is speaking ──
                vc = session["vc"]
                if vc.is_playing():
                    return

                # ── Generate and speak the reply ──
                async def send_to_channel(text):
                    await self.text_channel.send(f"🎤 **{username}:** {transcript}\n💬 {text}")

                await handle_query(
                    str(user.id), self.guild_id, username,
                    str(self.text_channel.id), transcript,
                    send_to_channel,
                    voice_client=vc,
                    is_voice=True,   # short spoken reply
                )

            except Exception as e:
                print(f"[VC] Process error for {user}: {e}")

        def cleanup(self):
            """Clean up all resources."""
            try:
                # Cancel all pending silence tasks
                for uid, task in self._silence_tasks.items():
                    if task:
                        try:
                            task.cancel()
                        except Exception:
                            pass
                self._silence_tasks.clear()
                self._buffers.clear()
                self._processing.clear()
            except Exception as e:
                print(f"[VC] Cleanup error: {e}")

else:
    # Dummy class when voice-recv isn't installed
    class FGOSSink:
        def __init__(self, *a, **kw):
            pass
        def cleanup(self):
            pass


async def auto_roast_vc(guild_id, user_id, username, transcript, channel, member):
    msgs = get_user_messages(user_id, limit=15)
    msg_sample = "\n".join(f'  "{m}"' for m in msgs[-8:]) if msgs else "  (no text yet)"
    prompt = (
        f"Write a 1-2 sentence Gen Z roast of {username} based on what they JUST SAID in VC.\n\n"
        f"VC: \"{transcript}\"\nTEXT STYLE:\n{msg_sample}\n\n"
        f"Specific, dry, deadpan, funny not mean. End with gen Z closer (fr 💀 / no cap / bro what)."
    )
    try:
        roast   = quick_ai(prompt, max_tokens=100)
        mention = getattr(member, "mention", f"@{username}")
        await channel.send(f"🎤 {mention} {roast}")
        session = voice_sessions.get(guild_id)
        if session and session["vc"].is_connected():
            await speak_in_vc(session["vc"], roast, "hype/joking")
    except Exception as e:
        print(f"Auto-roast error: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKGROUND TASKS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@tasks.loop(seconds=30)
async def check_voice_health():
    """Check if voice routers are still alive and restart if needed."""
    for guild_id, session in list(voice_sessions.items()):
        try:
            vc = session.get("vc")
            if not vc or not vc.is_connected():
                continue
            
            # Check if router thread is still alive (if using voice_recv)
            if hasattr(vc, 'router') and vc.router:
                if hasattr(vc.router, 'is_alive'):
                    if not vc.router.is_alive():
                        print(f"[VC] Router died for guild {guild_id}, attempting reconnect...")
                        # Try to restart the sink
                        if session.get("sink"):
                            try:
                                session["sink"].cleanup()
                            except:
                                pass
                        # Note: Full reconnect would need to be triggered by user
                        # For now just log it
        except Exception as e:
            print(f"[VC] Health check error for {guild_id}: {e}")

@tasks.loop(minutes=1)
async def check_reminders():
    for rid, user_id, channel_id, text in get_due_reminders():
        try:
            ch = bot.get_channel(int(channel_id))
            if ch:
                await ch.send(f"⏰ <@{user_id}> reminder: **{text}**")
            mark_reminder_done(rid)
        except Exception as e:
            print(f"Reminder error: {e}")

@tasks.loop(minutes=45)
async def conversation_starter():
    for channel_id in list(starter_channels):
        try:
            channel = bot.get_channel(int(channel_id))
            if not channel:
                continue
            guild_id    = str(channel.guild.id) if channel.guild else "dm"
            server_msgs = get_server_messages(guild_id, limit=20)
            server_fp   = fingerprint_server(server_msgs)
            prompt = f"Drop ONE casual conversation starter that fits this server. Short, natural, not cringe.\n{server_fp}"
            msg = quick_ai(prompt, max_tokens=60)
            await channel.send(msg[:500])
        except Exception as e:
            print(f"Starter error: {e}")

@tasks.loop(hours=1)
async def check_birthdays():
    await birthday_clock(bot)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BOT LIFECYCLE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.event
async def setup_hook():
    bot.tree.add_command(birthday_group)
    await bot.tree.sync()
    print("Slash commands synced!")

@bot.event
async def on_ready():
    if not check_reminders.is_running():
        check_reminders.start()
    if not conversation_starter.is_running():
        conversation_starter.start()
    if not check_birthdays.is_running():
        check_birthdays.start()
    await birthday_clock(bot)
    print("=" * 50)
    print(f"FG-OS LIVE: {bot.user.name}")
    print(f"Backend: Groq | TTS: edge-tts | Whisper: {WHISPER_MODEL}")
    print(f"Voice recv: {'ENABLED' if VOICE_RECV_AVAILABLE else 'DISABLED (pip install discord-ext-voice-recv)'}")
    print("=" * 50)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    guild_id = str(member.guild.id)
    session  = voice_sessions.get(guild_id)
    if not session:
        return
    if not getattr(before, "self_stream", False) and getattr(after, "self_stream", False):
        channel = session["channel"]
        msgs    = get_user_messages(str(member.id), limit=10)
        vibe    = "\n".join(msgs[-5:]) if msgs else ""
        prompt  = f"{member.display_name} just went LIVE on Discord. React in 1 sentence. Short, funny, natural.\n{vibe}"
        try:
            reaction = quick_ai(prompt, max_tokens=60)
            await channel.send(f"📺 {member.mention} {reaction}")
            if session["vc"].is_connected():
                await speak_in_vc(session["vc"], reaction, "hype/joking")
        except Exception as e:
            print(f"Go Live react error: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMANDS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.hybrid_command(name="help", description="Show all FG-OS commands")
async def help_command(ctx):
    await ctx.defer()
    cmds  = sorted([cmd for cmd in bot.commands if not cmd.hidden and cmd.name != "help"], key=lambda c: c.name)
    lines = ["**FG-OS Commands** — `/command` or `!command`", ""]
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
        guild_id = str(ctx.guild.id) if ctx.guild else "dm"
        vc = None
        if ctx.guild:
            session = voice_sessions.get(guild_id)
            if session and ctx.author.voice and ctx.author.voice.channel == session["vc"].channel:
                vc = session["vc"]
        await handle_query(
            ctx.author.id, guild_id, ctx.author.display_name,
            ctx.channel.id, question, ctx.send, voice_client=vc
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
    msgs = get_user_messages(ctx.author.id, limit=50)
    fp   = build_user_fingerprint(msgs, ctx.author.display_name)
    text = fingerprint_to_prompt(fp)
    await ctx.send(f"```\n{text[:1800]}\n```", ephemeral=True)

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

@bot.hybrid_command(name="stats", description="See stats for a user")
async def stats(ctx, member: discord.Member = None):
    await ctx.defer()
    target   = member or ctx.author
    guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM chat_logs WHERE user_id=? AND guild_id=?", (str(target.id), guild_id))
    total = c.fetchone()[0]
    c.execute("SELECT message_content FROM chat_logs WHERE user_id=? AND guild_id=?", (str(target.id), guild_id))
    all_msgs = [r[0] for r in c.fetchall()]
    c.execute("SELECT hour, COUNT(*) as cnt FROM chat_logs WHERE user_id=? AND guild_id=? GROUP BY hour ORDER BY cnt DESC LIMIT 1",
              (str(target.id), guild_id))
    peak_row = c.fetchone()
    conn.close()
    if total == 0:
        await ctx.send(f"no messages logged for {target.display_name} yet")
        return
    words    = []
    for m in all_msgs:
        words.extend(w.lower().strip(".,!?") for w in m.split() if len(w) > 2 and w.lower() not in STOPWORDS)
    top_words = Counter(words).most_common(8)
    top_str   = ", ".join(f"{w}({cnt})" for w, cnt in top_words) if top_words else "not enough data"
    peak_hour = f"{peak_row[0]}:00-{peak_row[0]+1}:00" if peak_row else "unknown"
    avg_len   = sum(len(m) for m in all_msgs) / len(all_msgs) if all_msgs else 0
    embed = discord.Embed(title=f"📊 Stats for {target.display_name}", color=0x5865F2)
    embed.add_field(name="Messages Logged",  value=str(total),              inline=True)
    embed.add_field(name="Avg Msg Length",   value=f"{avg_len:.0f} chars",  inline=True)
    embed.add_field(name="Most Active Hour", value=peak_hour,               inline=True)
    embed.add_field(name="Top Words/Slang",  value=top_str,                 inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="leaderboard", description="Who's been most active in this server")
async def leaderboard(ctx):
    await ctx.defer()
    guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username, COUNT(*) as cnt FROM chat_logs WHERE guild_id=? GROUP BY user_id ORDER BY cnt DESC LIMIT 10",
              (guild_id,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await ctx.send("no data yet, people gotta talk first")
        return
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines  = [f"{medals[i]} **{row[0]}** — {row[1]} messages" for i, row in enumerate(rows)]
    embed  = discord.Embed(title="🏆 Server Leaderboard", description="\n".join(lines), color=0xFFD700)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="servervibe", description="AI reads the server's current mood")
@commands.cooldown(1, 60, commands.BucketType.guild)
async def servervibe(ctx):
    await ctx.defer()
    guild_id    = str(ctx.guild.id) if ctx.guild else "dm"
    server_msgs = get_server_messages(guild_id, limit=50)
    if len(server_msgs) < 5:
        await ctx.send("not enough messages logged yet")
        return
    sample = "\n".join(
        f"{u}: {m[:200]}"
        for u, m in server_msgs[-30:]
    )
    prompt = (
        "Analyze this Discord server's current vibe. Mention:\n"
        "- overall mood\n"
        "- main topics\n"
        "- funniest patterns\n"
        "- community energy\n\n"
        "Keep it casual and friendly. Do not mention usernames unless important.\n"
        "Write 3-5 sentences.\n\n"
        f"{sample}"
    )
    try:
        vibe  = quick_ai(prompt, max_tokens=180)
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
        prompt  = f"Summarize this Discord convo in 3-5 casual sentences. Key points only.\n\n{chr(10).join(messages[-count:])}"
        summary = quick_ai(prompt, max_tokens=180)
        embed   = discord.Embed(title=f"📝 Last {count} Messages", description=summary, color=0x5865F2)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="tldr", description="Paste a wall of text and get a short summary")
async def tldr(ctx, *, text: str):
    await ctx.defer()
    try:
        prompt = f"TLDR in 2-3 casual sentences. No bullets.\n\nTEXT:\n{text[:2000]}"
        result = quick_ai(prompt, max_tokens=120)
        await ctx.send(f"**TLDR:** {result}")
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="poll", description="AI generates a poll from your question")
async def poll(ctx, *, question: str):
    await ctx.defer()
    try:
        prompt  = f"Make a Discord poll for: '{question}'\nReturn ONLY a numbered list of max 4 short options under 30 chars. No other text."
        result  = quick_ai(prompt, max_tokens=100)
        lines   = [l.strip() for l in result.strip().split("\n") if l.strip()]
        options = [re.sub(r"^[\d]+[.)]\s*", "", l).strip() for l in lines]
        options = [o for o in options if o][:4]
        if not options:
            await ctx.send("couldn't generate options, try rephrasing")
            return
        emojis    = ["1️⃣","2️⃣","3️⃣","4️⃣"]
        poll_text = f"**📊 {question}**\n\n" + "\n".join(f"{emojis[i]} {o}" for i, o in enumerate(options))
        poll_msg  = await ctx.send(poll_text)
        for i in range(len(options)):
            await poll_msg.add_reaction(emojis[i])
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="remind", description='Set a reminder. Example: "do homework in 2 hours"')
async def remind(ctx, *, text: str):
    await ctx.defer(ephemeral=True)
    try:
        prompt = f"Extract task and time from: '{text}'\nReturn ONLY JSON: {{\"task\": \"...\", \"minutes\": 60}}\nDefault 60 if unclear."
        result = quick_ai(prompt, max_tokens=60)
        match  = re.search(r'\{.*?\}', result, re.DOTALL)
        if not match:
            await ctx.send("couldn't parse — try 'remind me to X in 2 hours'", ephemeral=True)
            return
        data    = json.loads(match.group())
        task    = data.get("task", text)
        minutes = max(1, min(int(data.get("minutes", 60)), 10080))
        due_at  = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
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
        f"Ruthless Gen Z roast of {member.display_name} using ONLY their actual messages.\n\n"
        f"MESSAGES:\n{sample}\n\n"
        f"2-3 sentences max. Specific. Dry deadpan delivery. Funny not mean. "
        f"End with gen Z closer (fr 💀 / no cap / bro i'm done / on god)."
    )
    try:
        roast_text = quick_ai(prompt, max_tokens=130)
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
    fp   = build_user_fingerprint(target_msgs, member.display_name)
    fp_t = fingerprint_to_prompt(fp)
    prompt = (
        f"You ARE {member.display_name}. Respond to this situation exactly as them.\n\n"
        f"{fp_t}\n\n"
        f"SITUATION: {situation}\n\n"
        f"One message. Their exact style. Sound like them, not an AI."
    )
    try:
        response = quick_ai(prompt, max_tokens=180)
        await ctx.send(f"**{member.display_name}:** {response}")
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="debate", description="Two AI personalities debate a topic")
async def debate(ctx, *, topic: str):
    await ctx.defer()
    try:
        prompt = f"Two people debating: '{topic}'\n2 turns each. Side A and Side B. Casual Discord energy. 1-2 sentences per turn. Both make real points."
        result = quick_ai(prompt, max_tokens=250)
        embed  = discord.Embed(title=f"⚔️ Debate: {topic}", description=result, color=0xED4245)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="starterchannel", description="Toggle auto conversation starters in this channel")
async def starterchannel(ctx):
    cid = str(ctx.channel.id)
    if cid in starter_channels:
        starter_channels.discard(cid)
        await ctx.send("convo starters OFF")
    else:
        starter_channels.add(cid)
        await ctx.send("convo starters ON — dropping one every ~45 min")

# ── BATTLESHIP COMMANDS ─────────────────────────────
@bot.hybrid_command(name="battleship", description="Challenge the AI or another player to Battleship")
async def battleship(ctx, opponent: discord.Member = None):
    """Start a battleship game"""
    await safe_defer(ctx)
    
    if opponent and opponent.bot and opponent.id != bot.user.id:
        await safe_send(ctx, "can't play against bots (only the AI)")
        return
    
    is_ai = opponent is None
    
    if is_ai:
        game_id = create_game(ctx.author.id, ctx.author.display_name, is_ai=True, channel=ctx.channel)
        game = get_game_by_id(game_id)
        
        embed = discord.Embed(
            title="🚢 BATTLESHIP 🚢",
            description="",
            color=0x0099FF
        )
        embed.add_field(
            name="👤 Players",
            value=f"🟦 **{ctx.author.display_name}** vs 🤖 **FG-OS AI** (Very Smart)",
            inline=False
        )
        embed.add_field(
            name="✅ Status",
            value="Both fleets deployed randomly\n**Game Started!**",
            inline=False
        )
        embed.add_field(
            name="📋 Commands",
            value="`/fire A 5` — Attack coordinate\n`/gameboard` — View your boards\n`/quitgame` — Surrender",
            inline=False
        )
        embed.set_footer(text=f"🎮 Game #{game_id} | {ctx.author.display_name}'s Turn")
        
        msg = await ctx.send(embed=embed)
        
        # Show the board
        board_str = game.get_board_string(for_player1=True)
        await safe_send(ctx, f"```\n{board_str}\n```")
    else:
        if opponent.id == ctx.author.id:
            await ctx.send("you can't play against yourself, goofball")
            return
        
        game_id = create_game(ctx.author.id, ctx.author.display_name, opponent.id, opponent.display_name, channel=ctx.channel)
        game = get_game_by_id(game_id)
        
        embed = discord.Embed(
            title="🚢 BATTLESHIP 🚢",
            description="",
            color=0x0099FF
        )
        embed.add_field(
            name="👥 Players",
            value=f"🟦 **{ctx.author.display_name}** vs 🟥 **{opponent.display_name}**",
            inline=False
        )
        embed.add_field(
            name="✅ Status",
            value="Both fleets deployed randomly\n**Game Started!**",
            inline=False
        )
        embed.add_field(
            name="📋 Commands",
            value="`/fire A 5` — Attack coordinate\n`/gameboard` — View your boards\n`/quitgame` — Surrender",
            inline=False
        )
        embed.set_footer(text=f"🎮 Game #{game_id} | {ctx.author.display_name}'s Turn")
        
        msg = await ctx.send(embed=embed)
        
        # Send boards to each player
        board_str = game.get_board_string(for_player1=True)
        try:
            await safe_send(ctx.author, f"```\n{board_str}\n```")
        except:
            pass
        
        board_str = game.get_board_string(for_player1=False)
        try:
            await safe_send(opponent, f"```\n{board_str}\n```")
        except:
            pass

@bot.hybrid_command(name="fire", description="Take a shot! Example: /fire A 5")
async def fire(ctx, column: str, row: str):
    """Fire at opponent"""
    await safe_defer(ctx)
    
    game = get_game(ctx.author.id)
    if not game:
        await ctx.send("you're not in an active battleship game. start one with `/battleship`")
        return
    
    if game.game_state == "finished":
        await ctx.send("game already finished. start a new one with `/battleship`")
        return
    
    # Parse coordinates
    try:
        col_idx = ord(column.upper()) - 65
        row_idx = int(row) - 1
        
        if not (0 <= col_idx < 10 and 0 <= row_idx < 10):
            await ctx.send("❌ Coordinates out of bounds! Use A-J for columns, 1-10 for rows")
            return
    except:
        await ctx.send("❌ Invalid format! Use: `/fire A 5`")
        return
    
    # Check whose turn
    is_player1 = ctx.author.id == game.player1_id
    is_player2 = ctx.author.id == game.player2_id
    
    if not (is_player1 or is_player2):
        await ctx.send("❌ You're not in this game!")
        return
    
    # Validate turn
    if game.is_ai:
        if not is_player1:
            await ctx.send("❌ This is a single-player game!")
            return
        if game.current_turn != 1:
            await ctx.send("🤔 AI is calculating next move...")
            return
    else:
        current_player_name = game.player1_name if game.current_turn == 1 else game.player2_name
        if (is_player1 and game.current_turn != 1) or (is_player2 and game.current_turn != 2):
            await ctx.send(f"⏳ It's {current_player_name}'s turn!")
            return
    
    # Execute shot
    result, ship, is_sunk, all_sunk = game.shoot(is_player1, row_idx, col_idx)
    
    if result == "already_shot":
        await ctx.send(f"💧 Already shot {column.upper()}{row}! Try another spot.")
        return
    
    # Build result message
    coord_str = f"{column.upper()}{row}"
    color = 0xFF0000 if result == "hit" else 0x0099FF
    
    embed = discord.Embed(color=color)
    
    if result == "hit":
        embed.title = "💥 HIT!"
        embed.description = f"Direct hit at **{coord_str}**"
        if ship:
            embed.add_field(name="Target", value=ship.name, inline=False)
        if is_sunk:
            embed.description += f"\n\n🚨 **{ship.name} SUNK!** 🚨"
        if all_sunk:
            embed.description += f"\n\n🏆 **YOU WIN!** 🏆\nAll enemy ships destroyed!"
            game.game_state = "finished"
            game.winner_id = ctx.author.id
            embed.color = 0x00FF00
    else:
        embed.title = "💧 MISS"
        embed.description = f"Shot at **{coord_str}** missed!"
        embed.color = 0x0099FF
    
    await ctx.send(embed=embed)
    
    # AI turn (ONLY if AI game and not finished)
    if game.is_ai and game.game_state != "finished":
        await asyncio.sleep(0.8)
        
        # Get AI target
        try:
            ai_row, ai_col = game.ai_get_target()
        except Exception as e:
            print(f"[BATTLESHIP] AI targeting error: {e}")
            await ctx.send("⚠️ AI had a moment... trying again")
            return
        
        # Execute AI shot
        try:
            ai_result, ai_ship, ai_sunk, ai_all_sunk = game.shoot(False, ai_row, ai_col)
        except Exception as e:
            print(f"[BATTLESHIP] AI shoot error: {e}")
            await ctx.send("⚠️ Something went wrong with the AI turn")
            return
        
        ai_coord = f"{chr(65 + ai_col)}{ai_row + 1}"
        
        embed = discord.Embed(color=0xFF0000 if ai_result == "hit" else 0x0099FF)
        embed.title = "🤖 AI COUNTERATTACK"
        
        if ai_result == "hit":
            embed.title = "🤖 AI ATTACK - 💥 HIT!"
            embed.description = f"AI fires at **{ai_coord}**...\n**DIRECT HIT!**"
            embed.color = 0xFF0000
            
            if ai_ship:
                embed.add_field(name="Your Ship Hit", value=ai_ship.name, inline=False)
            
            if ai_sunk:
                embed.add_field(name="⚠️ WARNING", value=f"**{ai_ship.name} SUNK!**", inline=False)
            
            game.ai_last_hit = (ai_row, ai_col)
            game.ai_in_hunt = True
            
            if ai_all_sunk:
                embed.title = "☠️ GAME OVER"
                embed.description = f"AI sank your last ship at **{ai_coord}**!\n\n🤖 **AI WINS!** ☠️"
                embed.color = 0xFF0000
                game.game_state = "finished"
                game.winner_id = game.player2_id
        else:
            embed.title = "🤖 AI ATTACK - 💧 MISS"
            embed.description = f"AI fires at **{ai_coord}**...\n**MISS!**"
            embed.color = 0x0099FF
            game.ai_in_hunt = False
        
        await ctx.send(embed=embed)
        
        # Show player their board after AI turn
        if game.game_state != "finished":
            await asyncio.sleep(0.5)
            board_str = game.get_board_string(for_player1=True)
            status_embed = game.get_game_status_embed()
            
            try:
                player = await bot.fetch_user(game.player1_id)
                await player.send(embed=status_embed)
                await safe_send(player, f"```\n{board_str}\n```")
            except:
                pass
    
    # Switch turns for PvP
    if game.game_state != "finished" and not game.is_ai:
        game.current_turn = 2 if game.current_turn == 1 else 1
        
        # Auto-show next player their board
        next_is_player1 = game.current_turn == 1
        next_player_id = game.player1_id if next_is_player1 else game.player2_id
        
        await asyncio.sleep(0.3)
        
        try:
            next_player = await bot.fetch_user(next_player_id)
            if next_player:
                board_str = game.get_board_string(next_is_player1)
                status_embed = game.get_game_status_embed()
                
                await next_player.send(embed=status_embed)
                await safe_send(next_player, f"```\n{board_str}\n```")
        except Exception as e:
            print(f"[BATTLESHIP] Failed to send board to next player: {e}")

@bot.hybrid_command(name="gameboard", description="View your battleship boards")
async def gameboard(ctx):
    """Display current game board"""
    await safe_defer(ctx, ephemeral=True)
    
    game = get_game(ctx.author.id)
    if not game:
        await ctx.send("you're not in an active battleship game!", ephemeral=True)
        return
    
    is_player1 = ctx.author.id == game.player1_id
    board_str = game.get_board_string(for_player1=is_player1)
    
    status_embed = game.get_game_status_embed()
    ship_embed = game.get_ship_status_embed(for_player1=is_player1)
    
    await ctx.send(embed=status_embed, ephemeral=True)
    await ctx.send(embed=ship_embed, ephemeral=True)
    await safe_send(ctx, f"```\n{board_str}\n```", ephemeral=True)

@bot.hybrid_command(name="quitgame", description="Forfeit battleship game")
async def quitgame(ctx):
    """Quit current game"""
    await safe_defer(ctx)
    
    game = get_game(ctx.author.id)
    if not game:
        await ctx.send("you're not in a battleship game")
        return
    
    is_player1 = ctx.author.id == game.player1_id
    winner_name = game.player2_name if is_player1 else game.player1_name
    
    embed = discord.Embed(
        title="⚔️ Battle Forfeited",
        description=f"**{ctx.author.display_name}** surrendered!\n\n🏆 **{winner_name} WINS!**",
        color=0xFFD700
    )
    
    await ctx.send(embed=embed)
    end_game(game.game_id)

# ── VOICE COMMANDS ─────────────────────────────────
@bot.hybrid_command(name="joinvc", description="Join your VC — listens, transcribes, AND speaks responses")
async def joinvc(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("you're not in a VC")
        return
    await ctx.defer()
    guild_id = str(ctx.guild.id)

    # Disconnect existing session
    existing = voice_sessions.pop(guild_id, None)
    if existing:
        try:
            if hasattr(existing["vc"], "stop_listening"):
                existing["vc"].stop_listening()
            if existing.get("sink"):
                existing["sink"].cleanup()
            await existing["vc"].disconnect()
        except Exception as e:
            print(f"[VC] Cleanup error: {e}")

    try:
        vc = await ctx.author.voice.channel.connect(cls=VoiceRecvClient)
        print(f"[VC] Voice client type: {type(vc).__name__}")
    except Exception as e:
        await ctx.send(f"couldn't connect: {e}")
        return

    sink = None
    msg  = f"👂 joined **{ctx.author.voice.channel.name}**"

    if VOICE_RECV_AVAILABLE and isinstance(vc, VoiceRecvClient):
        try:
            sink = FGOSSink(guild_id, ctx.channel, bot)
            # Wrap listen with error handler
            try:
                vc.listen(sink)
                msg += " — listening and will reply out loud when you talk"
                print(f"[VC] Sink started for guild {guild_id}")
            except Exception as listen_err:
                print(f"[VC] Error starting listener: {listen_err}")
                msg += " — joined but voice listening failed (library issue)"
                sink = None
        except Exception as sink_err:
            print(f"[VC] Sink startup error: {sink_err}")
            msg += f" — listening setup failed: {sink_err}"
            sink = None
    else:
        if not VOICE_RECV_AVAILABLE:
            msg += " — ⚠️ install `discord-ext-voice-recv` to enable listening"
        else:
            msg += " — joined (audio receive unavailable)"

    voice_sessions[guild_id] = {
        "vc": vc, "sink": sink, "channel": ctx.channel, "transcripts": {}
    }
    await ctx.send(msg)
    
    try:
        await speak_in_vc(vc, "yo what's good", "hype/joking")
    except Exception as e:
        print(f"[VC] TTS startup error: {e}")

@bot.hybrid_command(name="leavevc", description="Make FG-OS leave the voice channel")
async def leavevc(ctx):
    await safe_defer(ctx)
    guild_id = str(ctx.guild.id)
    session  = voice_sessions.pop(guild_id, None)
    if not session:
        await safe_send(ctx, "i'm not in a VC")
        return
    try:
        if hasattr(session["vc"], "stop_listening"):
            session["vc"].stop_listening()
        if session.get("sink"):
            session["sink"].cleanup()
        await session["vc"].disconnect()
    except Exception as e:
        print(f"[VC] Leave error: {e}")
    await safe_send(ctx, "left the VC, saved all the receipts tho")

@bot.hybrid_command(name="say", description="Make FG-OS speak something in VC")
async def say(ctx, *, text: str):
    guild_id = str(ctx.guild.id)
    session  = voice_sessions.get(guild_id)
    if not session or not session["vc"].is_connected():
        await ctx.send("i'm not in a VC — use /joinvc first")
        return
    mood = detect_mood(text)
    await speak_in_vc(session["vc"], text, mood)
    await ctx.send(f"🔊 said: *{text[:100]}*", ephemeral=True)

@bot.hybrid_command(name="trashtalk", description="Trash talk someone using their VC speech + message history")
async def trashtalk(ctx, member: discord.Member):
    await ctx.defer()
    guild_id             = str(ctx.guild.id)
    session              = voice_sessions.get(guild_id)
    session_transcripts  = session["transcripts"].get(str(member.id), []) if session else []
    db_transcripts       = get_voice_transcripts(str(member.id), guild_id, limit=10)
    all_vc               = db_transcripts + session_transcripts
    msgs                 = get_user_messages(member.id, limit=20)
    if not all_vc and len(msgs) < 3:
        await ctx.send(f"no receipts on {member.display_name} yet — need them to talk in VC or send more messages")
        return
    vc_sample  = "\n".join(f'  "{t}"' for t in all_vc[-8:])  if all_vc  else "  (no VC data yet)"
    msg_sample = "\n".join(f'  "{m}"' for m in msgs[-10:])    if msgs    else "  (no text yet)"
    prompt = (
        f"DEVASTATING Gen Z trash talk targeting {member.display_name}.\n\n"
        f"VOICE:\n{vc_sample}\n\nTEXT:\n{msg_sample}\n\n"
        f"Use BOTH. Specific, dry, 2-4 sentences, funny not mean. End with gen Z energy."
    )
    try:
        trash = quick_ai(prompt, max_tokens=150)
        await ctx.send(f"🗑️ {member.mention} {trash}")
        if session and session["vc"].is_connected():
            await speak_in_vc(session["vc"], trash, "hype/joking")
    except Exception as e:
        await ctx.send(f"⚠️ Error: {e}")

@bot.hybrid_command(name="vctranscript", description="See what FG-OS heard from someone in VC")
async def vctranscript(ctx, member: discord.Member = None):
    await ctx.defer(ephemeral=True)
    guild_id  = str(ctx.guild.id)
    target    = member or ctx.author
    session   = voice_sessions.get(guild_id)
    session_t = session["transcripts"].get(str(target.id), []) if session else []
    all_t     = get_voice_transcripts(str(target.id), guild_id, limit=10) + session_t
    if not all_t:
        await ctx.send(f"no VC transcript for {target.display_name} yet", ephemeral=True)
        return
    out = "\n".join(f"[{i+1}] {t}" for i, t in enumerate(all_t[-10:]))
    await ctx.send(f"```\n🎤 VC ({target.display_name}):\n{out[:1800]}\n```", ephemeral=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MESSAGE WATCHER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content  = (message.content or "").strip()
    guild_id = str(message.guild.id) if message.guild else "dm"

    if message.guild is None:
        birthday_profiles = [p for p in get_all_birthday_profiles() if str(p.user_id) == str(message.author.id)]
        for profile in birthday_profiles:
            progress = get_interview_progress(str(profile.user_id), str(profile.guild_id))
            await handle_birthday_dm_answer(message, profile, progress)
        await bot.process_commands(message)
        return

    if content and not content.startswith("/") and not content.startswith("!"):
        log_message(message.author.id, guild_id, message.author.display_name, content)
        for fact in extract_facts(content):
            save_memory(message.author.id, guild_id, fact)

        profile = get_birthday_profile(str(message.author.id), guild_id)
        if profile and profile.birthday and profile.timezone:
            try:
                birthday_now = datetime.datetime.now(datetime.timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo(profile.timezone or "UTC"))
                birthday = datetime.date.fromisoformat(profile.birthday)
                if birthday.month == birthday_now.month and birthday.day == birthday_now.day and not has_birthday_day_reply(str(message.author.id), guild_id):
                    note_birthday_day_reply(str(message.author.id), guild_id)
                    await message.reply(f"🎂 A very happy birthday to you, {message.author.display_name} — I’m glad you’re here with us.")
            except Exception:
                pass

    if bot.user.mentioned_in(message):
        # Prevent duplicate handling of the same message (race between handlers)
        if message.id in recently_handled_messages:
            await bot.process_commands(message)
            return
        recently_handled_messages.add(message.id)
        try:
            # remove id after 10 seconds to avoid memory growth
            loop = asyncio.get_event_loop()
            loop.call_later(10, recently_handled_messages.discard, message.id)
        except Exception:
            pass

        async with message.channel.typing():
            try:
                prompt = re.sub(rf"<@!?{bot.user.id}>", "", message.content or "").strip() or "yo"

                vc = None
                if message.guild:
                    session = voice_sessions.get(guild_id)
                    if session and message.author.voice and \
                       message.author.voice.channel == session["vc"].channel:
                        vc = session["vc"]

                await handle_query(
                    message.author.id, guild_id,
                    message.author.display_name,
                    message.channel.id, prompt,
                    message.reply,
                    voice_client=vc
                )
            except Exception as e:
                try:
                    await safe_send(message, f"⚠️ Error: {e}")
                except Exception:
                    pass

    await bot.process_commands(message)


bot.run(DISCORD_TOKEN)