"""
Level System Cog – Vegas Bot
============================
• Automatic level-up detection after every real-mode game
• /level  — shows current level, progress, and chest status
• /daily  — claim daily chest (coin reward based on current level)
• DM sent to user on level-up
"""
from __future__ import annotations

import random
from datetime import date

import discord
from discord import app_commands
from discord.ext import commands

from modules.constants import FOOTER_TEXT
from modules.database import get_data, get_user_data, set_user_data
from modules.levels import (
    MAX_LEVEL,
    chest_rewards_for_level,
    chest_coins_for_level,
    progress_info,
    wager_requirement,
    deposit_requirement,
    coins_to_usd,
)
from modules.player import Player
from modules.utils import create_error_embed


def _coin_emoji() -> str:
    server_data = get_data("server/server") or {}
    return server_data.get("coin_emoji", "🪙")


def _fmt_coins_usd(coins: int, usd: float, ce: str) -> str:
    """Format a coin amount with its USD equivalent: '1,234 🪙 ($2.45)'"""
    if usd >= 1000:
        usd_str = f"${usd:,.0f}"
    elif usd >= 0.10:
        usd_str = f"${usd:,.2f}"
    elif usd >= 0.001:
        usd_str = f"${usd:.4f}"
    else:
        usd_str = f"${usd:.6f}"
    return f"{coins:,} {ce} ({usd_str})"


def _fmt_chest_range(min_coins: int, max_coins: int, min_usd: float, max_usd: float, ce: str) -> str:
    """Format a chest reward range: '5 🪙 ($0.50) — 20 🪙 ($2.00)'"""
    return f"{_fmt_coins_usd(min_coins, min_usd, ce)} — {_fmt_coins_usd(max_coins, max_usd, ce)}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _level_embed(player: Player) -> discord.Embed:
    level = player.level
    stats = player.stats
    total_wagered = int(stats.get("total_wagered", 0))
    total_deposit = int(stats.get("total_deposit", 0))

    info = progress_info(level, total_wagered, total_deposit)
    chest_min_usd, chest_max_usd = chest_rewards_for_level(level)
    chest_min_coins, chest_max_coins = chest_coins_for_level(level)
    ce = _coin_emoji()

    # Level colour: bronze → silver → gold → diamond gradient
    if level >= 80:
        color = 0x00cfff   # diamond blue
    elif level >= 50:
        color = 0xffd700   # gold
    elif level >= 25:
        color = 0xc0c0c0   # silver
    else:
        color = 0xcd7f32   # bronze

    embed = discord.Embed(color=color)
    embed.set_author(name=f"Level {level}  —  {player.uid}", icon_url=None)

    # ── Progress to next level ────────────────────────────────────────────
    if info["next_level"]:
        w_req = wager_requirement(info["next_level"])
        d_req = deposit_requirement(info["next_level"])
        wagered_usd = coins_to_usd(total_wagered)
        deposit_usd = coins_to_usd(total_deposit)

        w_pct = info["wager_progress_pct"]
        d_pct = info["deposit_progress_pct"]
        w_bar = _progress_bar(w_pct)
        d_bar = _progress_bar(d_pct)

        embed.add_field(
            name=f"➡️  Next Level: {info['next_level']}",
            value=(
                f"**Wager**\n"
                f"{w_bar}  {w_pct}%\n"
                f"${wagered_usd:,.2f}  /  ${w_req:,}\n"
                f"\n"
                f"**Deposit**\n"
                f"{d_bar}  {d_pct}%\n"
                f"${deposit_usd:,.2f}  /  ${d_req:,}"
            ),
            inline=False,
        )

        embed.add_field(
            name="📌  Still Needed",
            value=(
                f"Wager  →  **${info['wager_needed']:,}**\n"
                f"Deposit  →  **${info['deposit_needed']:,}**"
            ),
            inline=True,
        )
    else:
        embed.add_field(
            name="🌟  Max Level",
            value="You have reached the highest level!",
            inline=False,
        )

    # ── Daily chest ───────────────────────────────────────────────────────
    embed.add_field(
        name="🎁  Daily Chest",
        value=_fmt_chest_range(chest_min_coins, chest_max_coins, chest_min_usd, chest_max_usd, ce),
        inline=True,
    )

    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _progress_bar(pct: int, length: int = 10) -> str:
    filled = round(pct / 100 * length)
    return "█" * filled + "░" * (length - filled)


async def _send_levelup_dm(bot: commands.Bot, user_id: int, new_levels: list[int]):
    """Send a single DM for the highest level gained."""
    try:
        user = await bot.fetch_user(user_id)
    except Exception:
        return

    lvl = max(new_levels)
    chest_min_usd, chest_max_usd = chest_rewards_for_level(lvl)
    chest_min_coins, chest_max_coins = chest_coins_for_level(lvl)
    ce = _coin_emoji()
    skipped = len(new_levels) - 1
    desc = f"You reached **Level {lvl}**!"
    if skipped > 0:
        desc += f"\n*(+{skipped} levels skipped)*"
    desc += (
        f"\n\n🎁  Daily chest reward\n"
        f"{_fmt_chest_range(chest_min_coins, chest_max_coins, chest_min_usd, chest_max_usd, ce)}\n\n"
        f"Use **/daily** to claim it."
    )
    embed = discord.Embed(title="🎉  Level Up!", description=desc, color=0xffd700)
    embed.set_footer(text=FOOTER_TEXT)
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        pass  # DMs closed


# ── Cog ───────────────────────────────────────────────────────────────────────

class LevelsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Called externally after every real-mode game result ──────────────────

    async def process_level_up(self, user_id: int):
        """
        Check if user gained levels after a game and notify them.
        Call this from games.py after handle_result() for real-mode games.
        """
        player = Player(user_id)
        new_levels = player.check_and_apply_level_up()
        if new_levels:
            await _send_levelup_dm(self.bot, user_id, new_levels)

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(name="level", description="View your current level and progress.")
    async def cmd_level(self, interaction: discord.Interaction):
        player = Player(interaction.user.id)
        # Check for pending level-ups first
        new_levels = player.check_and_apply_level_up()
        if new_levels:
            await _send_levelup_dm(self.bot, interaction.user.id, new_levels)

        embed = _level_embed(player)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="daily", description="Claim your daily chest reward.")
    async def cmd_daily(self, interaction: discord.Interaction):
        player = Player(interaction.user.id)
        level_data = player.get_level_data()
        today = str(date.today())

        if level_data.get("last_chest_date") == today:
            embed = create_error_embed(
                f"You already claimed your chest today!\nCome back tomorrow. 🕐"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        level = player.level
        chest_min_usd, chest_max_usd = chest_rewards_for_level(level)
        chest_min_coins, chest_max_coins = chest_coins_for_level(level)
        reward_coins = random.randint(chest_min_coins, chest_max_coins)
        from modules.levels import get_coin_usd_rate
        reward_usd = reward_coins * get_coin_usd_rate()
        ce = _coin_emoji()

        # Give reward and update claim date
        player.add_balance("real", reward_coins)
        level_data["last_chest_date"] = today
        set_user_data(int(player.uid), "level", level_data)

        embed = discord.Embed(
            title="🎁  Daily Chest",
            color=0xffd700,
        )
        embed.add_field(
            name=f"Level {level} Chest",
            value=_fmt_coins_usd(reward_coins, reward_usd, ce) + " added to your balance!",
            inline=False,
        )
        embed.set_footer(text=FOOTER_TEXT)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="leveltable", description="Show level requirements and chest rewards.")
    async def cmd_leveltable(self, interaction: discord.Interaction):
        ce = _coin_emoji()
        embed = discord.Embed(
            title="📋  Level Requirements",
            description="Wager & deposit requirements are in **USD**. Chest rewards are in coins.",
            color=0x5865f2,
        )

        # Show milestones: every 10 levels (2, 10, 20, … 100)
        milestones = [2, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        for lvl in milestones:
            w = wager_requirement(lvl)
            d = deposit_requirement(lvl)
            cm_usd, cx_usd = chest_rewards_for_level(lvl)
            cm_coins, cx_coins = chest_coins_for_level(lvl)
            embed.add_field(
                name=f"Level {lvl}",
                value=(
                    f"Wager  →  **${w:,}**\n"
                    f"Deposit  →  **${d:,}**\n"
                    f"Chest  →  {_fmt_chest_range(cm_coins, cx_coins, cm_usd, cx_usd, ce)}"
                ),
                inline=True,
            )

        embed.set_footer(text=FOOTER_TEXT)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelsCog(bot))
