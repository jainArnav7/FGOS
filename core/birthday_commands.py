from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from core.birthday import (
    BirthdayProfile,
    delete_birthday_profile,
    get_all_birthday_profiles,
    get_birthday_profile,
    get_next_birthday_profile,
    get_upcoming_birthdays,
    save_birthday_profile,
    set_interview_progress,
    update_birthday_profile,
)

logger = logging.getLogger(__name__)

birthday_group = app_commands.Group(name="birthday", description="Birthday system for FG-OS")


def _profile_embed(profile: BirthdayProfile) -> discord.Embed:
    embed = discord.Embed(title=f"🎂 Birthday profile for {profile.nickname or profile.user_id}", color=0xF47FFF)
    embed.add_field(name="Birthday", value=profile.birthday, inline=True)
    embed.add_field(name="Timezone", value=profile.timezone, inline=True)
    embed.add_field(name="Announcement", value="On" if profile.announcement_enabled else "Off", inline=True)
    embed.add_field(name="Favorite color", value=profile.favorite_color or "—", inline=True)
    embed.add_field(name="Favorite food", value=profile.favorite_food or "—", inline=True)
    embed.add_field(name="Favorite game", value=profile.favorite_game or "—", inline=True)
    embed.add_field(name="Favorite artist", value=profile.favorite_artist or "—", inline=True)
    embed.add_field(name="Favorite hobby", value=profile.favorite_hobby or "—", inline=True)
    embed.add_field(name="Favorite cake", value=profile.favorite_cake or "—", inline=True)
    embed.add_field(name="Birthday wish", value=profile.birthday_wish or "—", inline=False)
    embed.add_field(name="Fun fact", value=profile.fun_fact or "—", inline=False)
    return embed


async def _require_member_profile(interaction: discord.Interaction) -> BirthdayProfile | None:
    profile = get_birthday_profile(str(interaction.user.id), str(interaction.guild_id))
    if profile is None:
        await interaction.response.send_message("You need to set your birthday first with `/birthday set`.", ephemeral=True)
        return None
    return profile


@birthday_group.command(name="set", description="Set your birthday profile")
@app_commands.describe(birthday="Birthday in YYYY-MM-DD", timezone="IANA timezone such as America/New_York", nickname="Nickname to use for birthday messages")
async def birthday_set(interaction: discord.Interaction, birthday: str, timezone: str = "UTC", nickname: str | None = None):
    ok, message = save_birthday_profile(str(interaction.user.id), str(interaction.guild_id), birthday, timezone, 1, nickname)
    await interaction.response.send_message(message, ephemeral=True)


@birthday_group.command(name="edit", description="Edit your birthday profile")
@app_commands.describe(
    favorite_color="Favorite color",
    favorite_food="Favorite food",
    favorite_game="Favorite game",
    favorite_artist="Favorite artist",
    favorite_movie="Favorite movie",
    favorite_hobby="Favorite hobby",
    favorite_cake="Favorite cake",
    favorite_fact="A fun fact about you",
    birthday_wish="A birthday wish for your next birthday",
    nickname="Your birthday nickname",
    timezone="IANA timezone such as America/New_York",
)
async def birthday_edit(
    interaction: discord.Interaction,
    favorite_color: str | None = None,
    favorite_food: str | None = None,
    favorite_game: str | None = None,
    favorite_artist: str | None = None,
    favorite_movie: str | None = None,
    favorite_hobby: str | None = None,
    favorite_cake: str | None = None,
    favorite_fact: str | None = None,
    birthday_wish: str | None = None,
    nickname: str | None = None,
    timezone: str | None = None,
):
    profile = await _require_member_profile(interaction)
    if profile is None:
        return
    updates: dict[str, Any] = {}
    if favorite_color is not None:
        updates["favorite_color"] = favorite_color.strip()
    if favorite_food is not None:
        updates["favorite_food"] = favorite_food.strip()
    if favorite_game is not None:
        updates["favorite_game"] = favorite_game.strip()
    if favorite_artist is not None:
        updates["favorite_artist"] = favorite_artist.strip()
    if favorite_movie is not None:
        updates["favorite_movie"] = favorite_movie.strip()
    if favorite_hobby is not None:
        updates["favorite_hobby"] = favorite_hobby.strip()
    if favorite_cake is not None:
        updates["favorite_cake"] = favorite_cake.strip()
    if favorite_fact is not None:
        updates["fun_fact"] = favorite_fact.strip()
    if birthday_wish is not None:
        updates["birthday_wish"] = birthday_wish.strip()
    if nickname is not None:
        updates["nickname"] = nickname.strip()
    if timezone is not None:
        updates["timezone"] = timezone.strip()
    ok, message = update_birthday_profile(str(interaction.user.id), str(interaction.guild_id), updates)
    await interaction.response.send_message(message, ephemeral=True)


@birthday_group.command(name="remove", description="Remove your birthday profile")
async def birthday_remove(interaction: discord.Interaction):
    removed = delete_birthday_profile(str(interaction.user.id), str(interaction.guild_id))
    await interaction.response.send_message("Your birthday profile has been removed." if removed else "Unable to remove that profile right now.", ephemeral=True)


@birthday_group.command(name="profile", description="View your birthday profile")
async def birthday_profile(interaction: discord.Interaction):
    profile = await _require_member_profile(interaction)
    if profile is None:
        return
    await interaction.response.send_message(embed=_profile_embed(profile), ephemeral=True)


@birthday_group.command(name="next", description="See the next birthday in the server")
async def birthday_next(interaction: discord.Interaction):
    profile = get_next_birthday_profile(str(interaction.guild_id))
    if profile is None:
        await interaction.response.send_message("No birthday profiles are configured for this server yet.", ephemeral=True)
        return
    await interaction.response.send_message(f"Next up: <@{profile.user_id}> — {profile.birthday}.", ephemeral=True)


@birthday_group.command(name="upcoming", description="Show upcoming birthdays in the server")
async def birthday_upcoming(interaction: discord.Interaction):
    upcoming = get_upcoming_birthdays(str(interaction.guild_id), days_ahead=30)
    if not upcoming:
        await interaction.response.send_message("No birthdays are coming up in the next 30 days.", ephemeral=True)
        return
    lines = [f"• <@{profile.user_id}> — {profile.birthday} ({profile.timezone})" for profile in upcoming[:10]]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@birthday_group.command(name="settings", description="Birthday system settings")
async def birthday_settings(interaction: discord.Interaction):
    profile = await _require_member_profile(interaction)
    if profile is None:
        return
    await interaction.response.send_message(
        f"Announcement enabled: {'Yes' if profile.announcement_enabled else 'No'}\n"
        f"Interview completed: {'Yes' if profile.interview_completed else 'No'}\n"
        f"Timezone: {profile.timezone}",
        ephemeral=True,
    )


@birthday_group.command(name="interview", description="Start or resume your birthday interview")
async def birthday_interview(interaction: discord.Interaction):
    profile = await _require_member_profile(interaction)
    if profile is None:
        return
    try:
        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message("I can only DM you from a server member context.", ephemeral=True)
            return
        await member.send("I’ll ask you one birthday question at a time. Reply with `skip`, `cancel`, or your answer.")
        set_interview_progress(str(interaction.user.id), str(interaction.guild_id), 0, {}, status="pending")
        await interaction.response.send_message("I’ve started the birthday interview in your DMs. Check your messages.", ephemeral=True)
    except Exception:
        logger.exception("Birthday interview dispatch failed")
        await interaction.response.send_message("I couldn’t start the interview because DMs are disabled or blocked.", ephemeral=True)
