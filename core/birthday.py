from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from discord import Member

from core.ai import ask_ai
from core.database import get_db
from core.memory import get_recent_messages

logger = logging.getLogger(__name__)

BIRTHDAY_PROFILE_FIELDS = (
    "birthday",
    "timezone",
    "announcement_enabled",
    "interview_completed",
    "last_celebrated_year",
    "favorite_color",
    "favorite_food",
    "favorite_game",
    "favorite_artist",
    "favorite_movie",
    "favorite_hobby",
    "favorite_cake",
    "fun_fact",
    "birthday_wish",
    "nickname",
)

INTERVIEW_QUESTIONS = (
    "Favorite color",
    "Favorite food",
    "Favorite game",
    "Favorite artist",
    "Favorite hobby",
    "Favorite cake",
    "Fun fact",
    "One thing you're proud of this year",
    "Birthday wish",
)

INTERVIEW_FIELD_MAP = {
    0: "favorite_color",
    1: "favorite_food",
    2: "favorite_game",
    3: "favorite_artist",
    4: "favorite_hobby",
    5: "favorite_cake",
    6: "fun_fact",
    7: "favorite_movie",
    8: "birthday_wish",
}


@dataclass
class BirthdayProfile:
    user_id: str
    guild_id: str
    birthday: str
    timezone: str = "UTC"
    announcement_enabled: int = 1
    interview_completed: int = 0
    last_celebrated_year: int | None = None
    last_birthday_reply_date: str | None = None
    favorite_color: str | None = None
    favorite_food: str | None = None
    favorite_game: str | None = None
    favorite_artist: str | None = None
    favorite_movie: str | None = None
    favorite_hobby: str | None = None
    favorite_cake: str | None = None
    fun_fact: str | None = None
    birthday_wish: str | None = None
    nickname: str | None = None
    role_removal_at: str | None = None
    updated_at: str | None = None


def ensure_birthday_tables() -> None:
    """Create the birthday tables in the existing shared SQLite database."""
    conn = get_db()
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


ensure_birthday_tables()


def _row_to_profile(row: Any) -> BirthdayProfile:
    return BirthdayProfile(
        user_id=row["user_id"],
        guild_id=row["guild_id"],
        birthday=row["birthday"],
        timezone=row["timezone"],
        announcement_enabled=int(row["announcement_enabled"] or 0),
        interview_completed=int(row["interview_completed"] or 0),
        last_celebrated_year=row["last_celebrated_year"],
        last_birthday_reply_date=row["last_birthday_reply_date"],
        favorite_color=row["favorite_color"],
        favorite_food=row["favorite_food"],
        favorite_game=row["favorite_game"],
        favorite_artist=row["favorite_artist"],
        favorite_movie=row["favorite_movie"],
        favorite_hobby=row["favorite_hobby"],
        favorite_cake=row["favorite_cake"],
        fun_fact=row["fun_fact"],
        birthday_wish=row["birthday_wish"],
        nickname=row["nickname"],
        role_removal_at=row["role_removal_at"],
        updated_at=row["updated_at"],
    )


def _parse_birthday_date(value: str) -> date | None:
    try:
        parsed = date.fromisoformat(value)
    except Exception:
        return None
    return parsed if parsed.year >= 1900 else None


def _parse_timezone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return None


def _user_key(user_id: str, guild_id: str) -> str:
    return f"{user_id}:{guild_id}"


def get_birthday_profile(user_id: str, guild_id: str) -> BirthdayProfile | None:
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM birthday_profiles WHERE user_id=? AND guild_id=? LIMIT 1",
            (str(user_id), str(guild_id)),
        ).fetchone()
        conn.close()
        return _row_to_profile(row) if row else None
    except Exception:
        logger.exception("Birthday profile lookup failed for %s/%s", user_id, guild_id)
        return None


def get_all_birthday_profiles(guild_id: str | None = None) -> list[BirthdayProfile]:
    try:
        conn = get_db()
        if guild_id is None:
            rows = conn.execute("SELECT * FROM birthday_profiles ORDER BY guild_id, user_id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM birthday_profiles WHERE guild_id=? ORDER BY user_id",
                (str(guild_id),),
            ).fetchall()
        conn.close()
        return [_row_to_profile(row) for row in rows]
    except Exception:
        logger.exception("Birthday profile scan failed")
        return []


def save_birthday_profile(
    user_id: str,
    guild_id: str,
    birthday: str,
    timezone_name: str = "UTC",
    announcement_enabled: int = 1,
    nickname: str | None = None,
) -> tuple[bool, str]:
    try:
        if not _parse_birthday_date(birthday):
            return False, "Please use a valid YYYY-MM-DD birthday date."
        if _parse_timezone(timezone_name) is None:
            return False, f"Invalid timezone: {timezone_name}. Use an IANA timezone like America/New_York."

        conn = get_db()
        conn.execute(
            """
            INSERT INTO birthday_profiles (
                user_id, guild_id, birthday, timezone, announcement_enabled, nickname,
                favorite_color, favorite_food, favorite_game, favorite_artist,
                favorite_movie, favorite_hobby, favorite_cake, fun_fact,
                birthday_wish, interview_completed, last_celebrated_year, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, guild_id)
            DO UPDATE SET
                birthday = excluded.birthday,
                timezone = excluded.timezone,
                announcement_enabled = excluded.announcement_enabled,
                nickname = COALESCE(excluded.nickname, birthday_profiles.nickname),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                str(user_id),
                str(guild_id),
                birthday,
                timezone_name,
                int(announcement_enabled),
                nickname.strip() if nickname else None,
            ),
        )
        conn.commit()
        conn.close()
        return True, "Birthday profile saved."
    except Exception:
        logger.exception("Birthday save failed for %s/%s", user_id, guild_id)
        return False, "The birthday database is unavailable right now."


def update_birthday_profile(user_id: str, guild_id: str, updates: dict[str, Any]) -> tuple[bool, str]:
    try:
        if not updates:
            return False, "No profile fields were provided to update."

        conn = get_db()
        set_clauses: list[str] = []
        values: list[Any] = []

        for key, value in updates.items():
            if key not in BIRTHDAY_PROFILE_FIELDS:
                continue
            if key == "timezone" and _parse_timezone(str(value or "UTC")) is None:
                conn.close()
                return False, f"Invalid timezone: {value}."
            if key in {"announcement_enabled", "interview_completed"}:
                value = int(bool(value))
            if value is None:
                continue
            set_clauses.append(f"{key} = ?")
            values.append(value)

        if not set_clauses:
            conn.close()
            return False, "No valid birthday fields were supplied."

        set_clauses.append("updated_at = CURRENT_TIMESTAMP")
        values.extend([str(user_id), str(guild_id)])

        conn.execute(
            f"UPDATE birthday_profiles SET {', '.join(set_clauses)} WHERE user_id=? AND guild_id=?",
            values,
        )
        conn.commit()
        conn.close()
        return True, "Birthday profile updated."
    except Exception:
        logger.exception("Birthday update failed for %s/%s", user_id, guild_id)
        return False, "The birthday database is unavailable right now."


def delete_birthday_profile(user_id: str, guild_id: str) -> bool:
    try:
        conn = get_db()
        conn.execute("DELETE FROM birthday_profiles WHERE user_id=? AND guild_id=?", (str(user_id), str(guild_id)))
        conn.execute("DELETE FROM birthday_interviews WHERE user_id=? AND guild_id=?", (str(user_id), str(guild_id)))
        conn.commit()
        conn.close()
        return True
    except Exception:
        logger.exception("Birthday deletion failed for %s/%s", user_id, guild_id)
        return False


def _serialize_answers(answers: dict[str, Any]) -> str:
    return json.dumps(answers, ensure_ascii=False)


def _load_answers(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def get_interview_progress(user_id: str, guild_id: str) -> dict[str, Any]:
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT current_step, answers_json, status, updated_at FROM birthday_interviews WHERE user_id=? AND guild_id=? LIMIT 1",
            (str(user_id), str(guild_id)),
        ).fetchone()
        conn.close()
        if not row:
            return {"current_step": 0, "answers": {}, "status": "pending"}
        return {
            "current_step": int(row["current_step"] or 0),
            "answers": _load_answers(row["answers_json"]),
            "status": row["status"] or "pending",
            "updated_at": row["updated_at"],
        }
    except Exception:
        logger.exception("Interview progress lookup failed for %s/%s", user_id, guild_id)
        return {"current_step": 0, "answers": {}, "status": "pending"}


def set_interview_progress(user_id: str, guild_id: str, current_step: int, answers: dict[str, Any], status: str = "pending") -> None:
    try:
        conn = get_db()
        conn.execute(
            """
            INSERT INTO birthday_interviews (user_id, guild_id, current_step, answers_json, status, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, guild_id)
            DO UPDATE SET
                current_step = excluded.current_step,
                answers_json = excluded.answers_json,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                str(user_id),
                str(guild_id),
                int(current_step),
                _serialize_answers(answers),
                status,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Interview progress save failed for %s/%s", user_id, guild_id)


def mark_interview_completed(user_id: str, guild_id: str) -> None:
    try:
        conn = get_db()
        conn.execute(
            "UPDATE birthday_profiles SET interview_completed = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id=? AND guild_id=?",
            (str(user_id), str(guild_id)),
        )
        conn.execute("DELETE FROM birthday_interviews WHERE user_id=? AND guild_id=?", (str(user_id), str(guild_id)))
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Interview completion failed for %s/%s", user_id, guild_id)


def _local_now(profile: BirthdayProfile) -> datetime:
    tz = _parse_timezone(profile.timezone or "UTC") or ZoneInfo("UTC")
    return datetime.now(tz)


def _days_until_birthday(profile: BirthdayProfile, now: datetime | None = None) -> int:
    current = now or _local_now(profile)
    birthday = _parse_birthday_date(profile.birthday)
    if birthday is None:
        return 999
    candidate = date(current.year, birthday.month, birthday.day)
    if candidate < current.date():
        candidate = date(current.year + 1, birthday.month, birthday.day)
    return (candidate - current.date()).days


def _birthday_is_today(profile: BirthdayProfile, now: datetime | None = None) -> bool:
    current = now or _local_now(profile)
    birthday = _parse_birthday_date(profile.birthday)
    if birthday is None:
        return False
    return birthday.month == current.month and birthday.day == current.day


def get_upcoming_birthdays(guild_id: str, days_ahead: int = 30) -> list[BirthdayProfile]:
    try:
        profiles = get_all_birthday_profiles(guild_id)
        now = datetime.now(timezone.utc)
        upcoming: list[BirthdayProfile] = []
        for profile in profiles:
            current_local = now.astimezone(ZoneInfo(profile.timezone or "UTC"))
            birthday = _parse_birthday_date(profile.birthday)
            if birthday is None:
                continue
            candidate = date(current_local.year, birthday.month, birthday.day)
            if candidate < current_local.date():
                candidate = date(current_local.year + 1, birthday.month, birthday.day)
            delta = (candidate - current_local.date()).days
            if 0 <= delta <= days_ahead:
                upcoming.append(profile)
        upcoming.sort(key=lambda profile: _days_until_birthday(profile))
        return upcoming
    except Exception:
        logger.exception("Upcoming birthdays lookup failed for guild %s", guild_id)
        return []


def get_next_birthday_profile(guild_id: str) -> BirthdayProfile | None:
    upcoming = get_upcoming_birthdays(guild_id, days_ahead=366)
    return upcoming[0] if upcoming else None


def build_birthday_announcement_prompt(member_name: str, guild_name: str, profile: BirthdayProfile, memory_items: Iterable[str]) -> str:
    memory_text = "\n".join(f"- {item}" for item in memory_items if item) or "- No specific memory available"
    profile_text = "\n".join(
        f"- {label}: {value}"
        for label, value in [
            ("Favorite color", profile.favorite_color),
            ("Favorite food", profile.favorite_food),
            ("Favorite game", profile.favorite_game),
            ("Favorite artist", profile.favorite_artist),
            ("Favorite movie", profile.favorite_movie),
            ("Favorite hobby", profile.favorite_hobby),
            ("Favorite cake", profile.favorite_cake),
            ("Fun fact", profile.fun_fact),
            ("Birthday wish", profile.birthday_wish),
        ]
        if value
    ) or "- No personal preferences saved yet"
    return (
        f"Write a single, original Discord birthday announcement for {member_name} in the server '{guild_name}'. "
        "Use the FG-OS tone: warm, grounded, mildly clever, friendly, and natural. "
        "Make it feel personal to this server and to the person. "
        "Do not use a generic 'Happy Birthday!' opener. Do not repeat the same phrase structure in every message. "
        "Use the profile details and the memory items if they help, but keep it concise and celebratory. "
        "Output only the final announcement text, no extra commentary.\n\n"
        f"Birthday profile:\n{profile_text}\n\n"
        f"Memory:\n{memory_text}"
    )


async def generate_birthday_announcement(member: Member, guild_name: str, profile: BirthdayProfile) -> str:
    try:
        memory_rows = get_recent_messages(str(member.id), limit=8)
        history = []
        for message in memory_rows:
            if isinstance(message, tuple):
                role, content = message[0], message[1]
                history.append({"role": role, "content": content})
        prompt = build_birthday_announcement_prompt(
            member.display_name,
            guild_name,
            profile,
            [message[1] if isinstance(message, tuple) else message for message in memory_rows],
        )
        user = SimpleNamespace(id=f"birthday-announcement-{member.id}", name=member.display_name, prompt=prompt)
        return await asyncio.to_thread(ask_ai, user, history=history, max_tokens=180)
    except Exception:
        logger.exception("Birthday announcement generation failed for %s", member.id)
        return f"{member.mention} is celebrating a big day today — the whole server is wishing them a great one."


def get_birthday_role_removal_due(guild_id: str | None = None) -> list[tuple[str, str, str]]:
    try:
        conn = get_db()
        if guild_id is None or str(guild_id) == "":
            rows = conn.execute(
                "SELECT user_id, guild_id, role_removal_at FROM birthday_profiles WHERE role_removal_at IS NOT NULL"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT user_id, guild_id, role_removal_at FROM birthday_profiles WHERE guild_id=? AND role_removal_at IS NOT NULL",
                (str(guild_id),),
            ).fetchall()
        conn.close()
        return [(row["user_id"], row["guild_id"], row["role_removal_at"]) for row in rows]
    except Exception:
        logger.exception("Role-removal lookup failed for guild %s", guild_id)
        return []


def mark_role_removal(user_id: str, guild_id: str, removal_at: str) -> None:
    try:
        conn = get_db()
        conn.execute(
            "UPDATE birthday_profiles SET role_removal_at=? WHERE user_id=? AND guild_id=?",
            (removal_at, str(user_id), str(guild_id)),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Role-removal timestamp save failed for %s/%s", user_id, guild_id)


def clear_role_removal(user_id: str, guild_id: str) -> None:
    try:
        conn = get_db()
        conn.execute(
            "UPDATE birthday_profiles SET role_removal_at=NULL WHERE user_id=? AND guild_id=?",
            (str(user_id), str(guild_id)),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Role-removal cleanup failed for %s/%s", user_id, guild_id)


def has_birthday_day_reply(user_id: str, guild_id: str, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    today = current.date().isoformat()
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT last_birthday_reply_date FROM birthday_profiles WHERE user_id=? AND guild_id=? LIMIT 1",
            (str(user_id), str(guild_id)),
        ).fetchone()
        conn.close()
        return bool(row and row["last_birthday_reply_date"] == today)
    except Exception:
        logger.exception("Birthday reply lookup failed for %s/%s", user_id, guild_id)
        return False


def note_birthday_day_reply(user_id: str, guild_id: str, now: datetime | None = None) -> None:
    current = now or datetime.now(timezone.utc)
    today = current.date().isoformat()
    try:
        conn = get_db()
        conn.execute(
            "UPDATE birthday_profiles SET last_birthday_reply_date=? WHERE user_id=? AND guild_id=?",
            (today, str(user_id), str(guild_id)),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Birthday reply persistence failed for %s/%s", user_id, guild_id)
