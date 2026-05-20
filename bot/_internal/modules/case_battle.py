"""Case Battle — settings, allowed cases, optional log channel."""

from __future__ import annotations

from typing import Optional

import discord

from modules.database import get_data, set_data


def get_case_battle_settings() -> dict:
    data = get_data("server/case_battle") or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("log_channel_id", None)
    data.setdefault("allowed_case_ids", [])
    return data


def save_case_battle_settings(data: dict) -> None:
    set_data("server/case_battle", data)


def get_allowed_battle_cases() -> dict:
    """Cases eligible for battles (admin allow-list or all with items)."""
    from cogs.games import _get_cases_data

    settings = get_case_battle_settings()
    allowed_ids = settings.get("allowed_case_ids") or []
    all_cases = _get_cases_data().get("cases", {})
    if not allowed_ids:
        return {cid: c for cid, c in all_cases.items() if c.get("items")}
    return {
        cid: c
        for cid, c in all_cases.items()
        if cid in allowed_ids and c.get("items")
    }


async def log_case_battle(
    interaction: discord.Interaction,
    *,
    opponent: str,
    challenger: discord.Member,
    case_name: str,
    stake: int,
    mode: str,
    player_item: dict,
    bot_item: dict,
    winner: str,
    game_uid: str,
    profit: int,
) -> None:
    """Unified game log — skipped for bot opponents (private room only)."""
    from modules.game_log import post_short_game_log

    if winner == "player":
        result = "win"
    elif winner == "tie":
        result = "tie"
    else:
        result = "lose"

    await post_short_game_log(
        challenger,
        "Case Battle",
        result,
        profit,
        mode,
        client=interaction.client,
        guild_id=interaction.guild.id if interaction.guild else None,
        skip=(opponent == "bot"),
    )
