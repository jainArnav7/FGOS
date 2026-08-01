from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

import discord

from core.birthday import (
    BirthdayProfile,
    INTERVIEW_FIELD_MAP,
    INTERVIEW_QUESTIONS,
    _birthday_is_today,
    _days_until_birthday,
    clear_role_removal,
    generate_birthday_announcement,
    get_all_birthday_profiles,
    get_birthday_profile,
    get_birthday_role_removal_due,
    get_interview_progress,
    mark_interview_completed,
    mark_role_removal,
    set_interview_progress,
    update_birthday_profile,
)
from core.config import BIRTHDAY_ANNOUNCEMENT_ENABLED, BIRTHDAY_CHANNEL_ID, BIRTHDAY_ROLE_ID
from core.database import get_db

logger = logging.getLogger(__name__)


async def send_birthday_interview_prompt(bot: discord.Client, profile: BirthdayProfile) -> None:
    """DM exactly one interview question at a time and persist the interview step."""
    guild = bot.get_guild(int(profile.guild_id)) if profile.guild_id.isdigit() else None
    if guild is None:
        return

    member = guild.get_member(int(profile.user_id))
    if member is None:
        return

    progress = get_interview_progress(str(profile.user_id), str(profile.guild_id))
    current_step = int(progress.get("current_step", 0) or 0)
    if current_step >= len(INTERVIEW_QUESTIONS):
        mark_interview_completed(str(profile.user_id), str(profile.guild_id))
        return

    try:
        await member.send(
            f"🎂 Birthday interview — question {current_step + 1}/{len(INTERVIEW_QUESTIONS)}\n{INTERVIEW_QUESTIONS[current_step]}\n\n"
            "Reply with your answer, `skip` to move on, or `cancel` to stop."
        )
        set_interview_progress(str(profile.user_id), str(profile.guild_id), current_step, progress.get("answers", {}), status="pending")
    except Exception:
        logger.exception("Failed to DM birthday interview prompt for %s", profile.user_id)


async def handle_birthday_dm_answer(message: discord.Message, profile: BirthdayProfile, progress: dict[str, object]) -> None:
    """Resume or finish the birthday interview from a DM answer."""
    if message.author.bot:
        return

    content = (message.content or "").strip()
    current_step = int(progress.get("current_step", 0) or 0)
    answers = dict(progress.get("answers", {}) or {})
    lowered = content.lower()

    if lowered in {"cancel", "stop"}:
        set_interview_progress(str(profile.user_id), str(profile.guild_id), current_step, answers, status="cancelled")
        await message.author.send("Birthday interview cancelled. You can restart it anytime with `/birthday interview`.")
        return

    if lowered == "resume":
        set_interview_progress(str(profile.user_id), str(profile.guild_id), current_step, answers, status="pending")
        await message.author.send(
            f"🎂 Birthday interview resumes.\n{INTERVIEW_QUESTIONS[current_step]}\n\n"
            "Reply with your answer, `skip` to move on, or `cancel` to stop."
        )
        return

    if lowered == "skip":
        current_step += 1
        if current_step >= len(INTERVIEW_QUESTIONS):
            mark_interview_completed(str(profile.user_id), str(profile.guild_id))
            await message.author.send("You finished the birthday interview. Thanks for sharing.")
            return
        set_interview_progress(str(profile.user_id), str(profile.guild_id), current_step, answers, status="pending")
        await message.author.send(
            f"🎂 Birthday interview — question {current_step + 1}/{len(INTERVIEW_QUESTIONS)}\n{INTERVIEW_QUESTIONS[current_step]}\n\n"
            "Reply with your answer, `skip` to move on, or `cancel` to stop."
        )
        return

    field_name = INTERVIEW_FIELD_MAP.get(current_step)
    if field_name is None:
        return

    answers[field_name] = content
    current_step += 1
    if current_step >= len(INTERVIEW_QUESTIONS):
        ok, _ = update_birthday_profile(str(profile.user_id), str(profile.guild_id), answers)
        if ok:
            mark_interview_completed(str(profile.user_id), str(profile.guild_id))
            await message.author.send("You finished the birthday interview. Thanks for sharing.")
        else:
            await message.author.send("I couldn’t save the interview answers right now. Try again later.")
        return

    set_interview_progress(str(profile.user_id), str(profile.guild_id), current_step, answers, status="pending")
    await message.author.send(
        f"🎂 Birthday interview — question {current_step + 1}/{len(INTERVIEW_QUESTIONS)}\n{INTERVIEW_QUESTIONS[current_step]}\n\n"
        "Reply with your answer, `skip` to move on, or `cancel` to stop."
    )


async def birthday_clock(bot: discord.Client) -> None:
    """Checks birthday timelines hourly, respecting each member’s timezone and restarting automatically on bot startup."""
    profiles = get_all_birthday_profiles()
    now = dt.datetime.now(dt.timezone.utc)
    for profile in profiles:
        try:
            local_now = now.astimezone(ZoneInfo(profile.timezone or "UTC"))
            days_until = _days_until_birthday(profile, local_now)

            if _birthday_is_today(profile, local_now):
                await celebrate_birthday(bot, profile, local_now)
                continue

            if days_until in {7, 3, 1} and not profile.interview_completed:
                await send_birthday_interview_prompt(bot, profile)

            if days_until in {7, 3, 1}:
                await send_birthday_countdown(bot, profile, local_now)

            await cleanup_birthday_role_state(bot)
        except Exception:
            logger.exception("Birthday hourly check failed for user %s", profile.user_id)


async def send_birthday_countdown(bot: discord.Client, profile: BirthdayProfile, local_now: dt.datetime) -> None:
    guild = bot.get_guild(int(profile.guild_id)) if profile.guild_id.isdigit() else None
    if guild is None:
        return
    channel = guild.get_channel(BIRTHDAY_CHANNEL_ID) if BIRTHDAY_CHANNEL_ID else None
    if channel is None:
        return
    days = _days_until_birthday(profile, local_now)
    if days in {7, 3, 1}:
        try:
            await channel.send(f"🎉 <@{profile.user_id}> has a birthday in {days} day{'s' if days != 1 else ''}.")
        except Exception:
            logger.exception("Birthday countdown failed for %s", profile.user_id)


async def celebrate_birthday(bot: discord.Client, profile: BirthdayProfile, local_now: dt.datetime) -> None:
    try:
        guild = bot.get_guild(int(profile.guild_id)) if profile.guild_id.isdigit() else None
        if guild is None:
            return
        member = guild.get_member(int(profile.user_id))
        if member is None:
            return

        if BIRTHDAY_ROLE_ID:
            role = guild.get_role(BIRTHDAY_ROLE_ID)
            if role is not None and role not in member.roles:
                await member.add_roles(role, reason="Birthday role")

        if BIRTHDAY_ANNOUNCEMENT_ENABLED:
            channel = guild.get_channel(BIRTHDAY_CHANNEL_ID) if BIRTHDAY_CHANNEL_ID else None
            if channel is not None:
                text = await generate_birthday_announcement(member, guild.name, profile)
                await channel.send(text)

        current_year = local_now.year
        if profile.last_celebrated_year != current_year:
            conn = get_db()
            conn.execute(
                "UPDATE birthday_profiles SET last_celebrated_year=?, updated_at = CURRENT_TIMESTAMP WHERE user_id=? AND guild_id=?",
                (current_year, str(profile.user_id), str(profile.guild_id)),
            )
            conn.commit()
            conn.close()

        removal_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)).isoformat()
        mark_role_removal(str(profile.user_id), str(profile.guild_id), removal_at)
    except Exception:
        logger.exception("Birthday celebration failed for %s", profile.user_id)


async def cleanup_birthday_role_state(bot: discord.Client) -> None:
    """Remove the temporary birthday role once the 24-hour window has elapsed."""
    try:
        rows = get_birthday_role_removal_due()
        for user_id, guild_id, removal_at in rows:
            if not removal_at:
                continue
            try:
                expires_at = dt.datetime.fromisoformat(removal_at)
            except ValueError:
                continue
            if dt.datetime.now(dt.timezone.utc) < expires_at:
                continue
            guild = bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
            if guild is None:
                continue
            member = guild.get_member(int(user_id))
            if member is None:
                continue
            role = guild.get_role(BIRTHDAY_ROLE_ID) if BIRTHDAY_ROLE_ID else None
            if role is not None and role in member.roles:
                await member.remove_roles(role, reason="Birthday role expired")
            clear_role_removal(str(user_id), str(guild_id))
    except Exception:
        logger.exception("Birthday role cleanup failed")
