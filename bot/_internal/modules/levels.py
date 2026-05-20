"""
Level System – Vegas Bot
========================
100 levels driven by two requirements:
  • total_wagered  (real-mode only, in coins — 1 coin = $1)
  • total_deposit  (cumulative deposits in coins)

Algorithm
---------
Both grow exponentially from 1 → 1000 across 100 levels.

  wager_req(level)   = round(1 * 1000 ** ((level - 1) / 99))   ← coins
  deposit_req(level) = round(wager_req(level) * 0.30)           ← 30% of wager req

Daily chest reward grows every 5 levels (tier 1-5, 6-10, … 96-100).
  chest_min/max(tier) = round(base * growth_factor ** (tier - 1))
  where base_min=5, base_max=15, growth_factor≈1.70 (gives ~500-2000 at tier 20)
"""

import math
from typing import Tuple, Dict

MAX_LEVEL = 100

# ── Requirement scale ───────────────────────────────────────────────────────
# level 2  → _WAGER_BASE  ($5)
# level 100 → _WAGER_MAX  ($25,000)
# Exponential growth between those two anchors.
_WAGER_BASE = 5
_WAGER_MAX  = 1_000

# ── Requirement generators ──────────────────────────────────────────────────

def wager_requirement(level: int) -> int:
    """Cumulative wager (USD) required to REACH this level."""
    if level <= 1:
        return 0
    ratio = _WAGER_MAX / _WAGER_BASE
    return max(1, round(_WAGER_BASE * (ratio ** ((level - 2) / (MAX_LEVEL - 2)))))


def deposit_requirement(level: int) -> int:
    """Cumulative deposit (coins) required to REACH this level from the previous one."""
    if level <= 1:
        return 0
    return max(1, wager_requirement(level))


def get_level_table() -> Dict[int, Dict]:
    """
    Return the full level table as a dict keyed by level (2-100).
    Each entry: {"wager": int, "deposit": int, "chest_min": int, "chest_max": int}
    """
    table = {}
    for lvl in range(2, MAX_LEVEL + 1):
        w = wager_requirement(lvl)
        d = deposit_requirement(lvl)
        cm, cx = chest_rewards_for_level(lvl)
        table[lvl] = {"wager": w, "deposit": d, "chest_min": cm, "chest_max": cx}
    return table


# ── Chest rewards ───────────────────────────────────────────────────────────
# Rewards are defined in USD — small bonus, not a passive income source.
# Level 1-5  → $0.01 – $0.05
# Level 96-100 → ~$0.32 – $1.60
# Growth: 1.20x per tier (20 tiers total)

_CHEST_BASE_MIN_USD = 0.01
_CHEST_BASE_MAX_USD = 0.05
_CHEST_GROWTH       = 1.20


def chest_rewards_for_level(level: int) -> Tuple[float, float]:
    """Return (min_usd, max_usd) dollar value of the daily chest at *level*."""
    tier   = max(1, math.ceil(level / 5))  # level 1-5 → tier 1, …, 96-100 → tier 20
    growth = _CHEST_GROWTH ** (tier - 1)
    return (
        round(_CHEST_BASE_MIN_USD * growth, 4),
        round(_CHEST_BASE_MAX_USD * growth, 4),
    )


def chest_coins_for_level(level: int) -> Tuple[int, int]:
    """Return (min_coins, max_coins) converted from USD using the live exchange rate."""
    rate = get_coin_usd_rate()
    if rate <= 0:
        rate = 0.10
    min_usd, max_usd = chest_rewards_for_level(level)
    return (
        max(1, round(min_usd / rate)),
        max(1, round(max_usd / rate)),
    )


# ── Exchange rate helper ────────────────────────────────────────────────────

def get_coin_usd_rate() -> float:
    """Return current coin → USD rate from server/exchange_rates (default 0.10)."""
    from modules.database import get_data
    rates_data = get_data("server/exchange_rates") or {}
    return float(rates_data.get("coin_usd_rate", 0.10))


def coins_to_usd(coins: int) -> float:
    """Convert a coin amount to its USD equivalent using the live exchange rate."""
    return coins * get_coin_usd_rate()


# ── Progress helpers ────────────────────────────────────────────────────────

def can_level_up(current_level: int, total_wagered: int, total_deposit: int) -> bool:
    """Return True if the player meets the requirements for the next level.

    *total_wagered* and *total_deposit* are in coins; requirements are in USD.
    The live exchange rate is used for conversion.
    """
    next_lvl = current_level + 1
    if next_lvl > MAX_LEVEL:
        return False
    rate = get_coin_usd_rate()
    wagered_usd = total_wagered * rate
    deposit_usd = total_deposit * rate
    return (
        wagered_usd >= wager_requirement(next_lvl)
        and deposit_usd >= deposit_requirement(next_lvl)
    )


def levels_to_gain(current_level: int, total_wagered: int, total_deposit: int) -> int:
    """Return how many levels the player should gain right now (may be >1)."""
    gained = 0
    lvl = current_level
    rate = get_coin_usd_rate()
    wagered_usd = total_wagered * rate
    deposit_usd = total_deposit * rate
    while True:
        next_lvl = lvl + 1
        if next_lvl > MAX_LEVEL:
            break
        if wagered_usd >= wager_requirement(next_lvl) and deposit_usd >= deposit_requirement(next_lvl):
            lvl += 1
            gained += 1
        else:
            break
    return gained


def progress_info(current_level: int, total_wagered: int, total_deposit: int) -> Dict:
    """
    Return a dict with progress towards the next level.
    Coin amounts are converted to USD using the live exchange rate.
    All 'needed' values are returned in USD.
    """
    next_lvl = current_level + 1
    if next_lvl > MAX_LEVEL:
        return {
            "next_level": None,
            "wager_needed": 0,
            "deposit_needed": 0,
            "wager_progress_pct": 100,
            "deposit_progress_pct": 100,
        }
    rate = get_coin_usd_rate()
    wagered_usd = total_wagered * rate
    deposit_usd = total_deposit * rate
    w_req = wager_requirement(next_lvl)
    d_req = deposit_requirement(next_lvl)
    return {
        "next_level": next_lvl,
        "wager_needed":  max(0.0, round(w_req - wagered_usd, 2)),
        "deposit_needed": max(0.0, round(d_req - deposit_usd, 2)),
        "wager_progress_pct":  min(100, round(wagered_usd / w_req * 100)) if w_req else 100,
        "deposit_progress_pct": min(100, round(deposit_usd / d_req * 100)) if d_req else 100,
    }
