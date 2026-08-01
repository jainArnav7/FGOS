import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "data" / "fg_os_memory.db"
DB_NAME = str(DB_PATH)


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def setup_database():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY,
            user_id TEXT,
            username TEXT,
            role TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories(
            id INTEGER PRIMARY KEY,
            user_id TEXT,
            memory TEXT,
            importance INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS birthday_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            guild_id TEXT NOT NULL,
            birthday TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            announcement_enabled INTEGER NOT NULL DEFAULT 1,
            interview_completed INTEGER NOT NULL DEFAULT 0,
            last_celebrated_year INTEGER,
            last_birthday_reply_date TEXT,
            favorite_color TEXT,
            favorite_food TEXT,
            favorite_game TEXT,
            favorite_artist TEXT,
            favorite_movie TEXT,
            favorite_hobby TEXT,
            favorite_cake TEXT,
            fun_fact TEXT,
            birthday_wish TEXT,
            nickname TEXT,
            role_removal_at TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, guild_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS birthday_interviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            guild_id TEXT NOT NULL,
            current_step INTEGER NOT NULL DEFAULT 0,
            answers_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, guild_id)
        )
        """
    )
    conn.commit()
    conn.close()
    return DB_NAME


setup_database()
