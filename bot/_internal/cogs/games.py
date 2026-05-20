import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Select, Button, Modal, TextInput
from typing import Optional
import random
import asyncio
import time

from modules.database import get_data, set_data, replace_data, get_server_data, get_user_data, set_user_data
from modules.translator import t
from modules.player import Player
from modules.utils import format_balance
import modules.promo as promo_engine
import modules.bonus as bonus_engine
import modules.race as race_engine
from Games import RouletteGame, DiceGame, CoinFlipGame, MinesGame, CrystalsGame, TowersGame, LimboGame, SlotGame, GameResult
from Games import (
    get_bj_emojis, bj_new_state, bj_hand_value, bj_hand_display, bj_card_display,
    bj_can_split, bj_can_double, bj_can_insurance, bj_is_blackjack,
    do_hit, do_stand, do_double, do_split, do_insurance,
    bj_evaluate, evaluate_side_bets, BJ_RANKS, BJ_SUITS,
)
from Games import calc_hilo_odds, new_hilo_state, hilo_guess, HILO_HOUSE_EDGE


def _get_bj_emoji_map(bot) -> dict:
    """Fetch BJ card emoji map from the configured guild by emoji name at runtime."""
    try:
        gd       = get_data("server/games") or {}
        bj       = gd.get("blackjack", {}) if isinstance(gd, dict) else {}
        guild_id = bj.get("emoji_guild_id")
        if guild_id and bot:
            guild = bot.get_guild(int(guild_id))
            if guild:
                all_keys  = [r + s for r in BJ_RANKS for s in BJ_SUITS] + ["CB"]
                emoji_map = {}
                for key in all_keys:
                    em = discord.utils.get(guild.emojis, name=key)
                    if em:
                        emoji_map[key] = str(em)   # <:name:id>
                return emoji_map
        # Fall back to pre-saved strings (old import method)
        return bj.get("emojis", {}) if isinstance(bj, dict) else {}
    except Exception:
        return {}


def _get_blackjack_settings() -> dict:
    """Read Blackjack rigged_chance from server/games['blackjack']."""
    games_data = get_data("server/games") or {}
    bj_data = games_data.get("blackjack", {}) if isinstance(games_data, dict) else {}
    if not isinstance(bj_data, dict):
        bj_data = {}
    rigged_chance = bj_data.get("rigged_chance", 0.0)
    try:
        rigged_chance = float(rigged_chance)
    except (TypeError, ValueError):
        rigged_chance = 0.0
    rigged_chance = max(0.0, min(100.0, rigged_chance))
    return {"rigged_chance": rigged_chance}


def _rig_hit_card(state: dict) -> None:
    """
    Swap a card that busts the current hand to the top of the deck.
    If no single card can bust (hand value ≤ 11), put the LOWEST value card
    at the top so the hand stays as low as possible — rigging will succeed
    on a subsequent hit.
    """
    h = state["hands"][state["cur"]]
    deck = state["deck"]

    # Try to find a bust card first
    for i in range(len(deck) - 1, -1, -1):
        if bj_hand_value(h["cards"] + [deck[i]]) > 21:
            deck[-1], deck[i] = deck[i], deck[-1]
            return

    # No bust card possible yet (hand ≤ 11) — put the lowest-value card on top
    # so the next hit is also small and we can bust on the hit after that
    min_idx = None
    min_val = float("inf")
    for i, card in enumerate(deck):
        v = bj_hand_value([card])
        if v < min_val:
            min_val = v
            min_idx = i
    if min_idx is not None:
        deck[-1], deck[min_idx] = deck[min_idx], deck[-1]


def _rig_dealer_beat(state: dict) -> None:
    """
    Ensure the dealer beats every non-busted player hand.
    Strategy:
      1. If dealer already wins cleanly → do nothing.
      2. Reset dealer to initial 2 cards (put extras back in deck).
      3. Draw cards one-by-one: prefer a card that directly beats the player;
         if none exists yet, take the highest safe card and keep drawing.
      4. Always enforce the 17+ hit rule at the end — dealer can NEVER
         legally stand below 17.
    """
    non_busted_vals = [
        bj_hand_value(h["cards"])
        for h in state["hands"]
        if h["status"] != "busted"
    ]
    if not non_busted_vals:
        return  # all hands already busted — player already lost

    max_val = max(non_busted_vals)
    dealer = state["dealer"]

    # Dealer already wins cleanly — nothing to do
    d_val = bj_hand_value(dealer)
    if 21 >= d_val >= max_val:
        return

    # Reset dealer to initial 2 cards; return extras to deck
    while len(dealer) > 2:
        state["deck"].append(dealer.pop())

    # Draw targeted cards until dealer beats player (safety limit: 10 draws)
    for _ in range(10):
        d_val = bj_hand_value(dealer)
        if d_val > max_val:
            break

        # First priority: a card that takes dealer directly to (max_val+1)..21
        best_idx = None
        best_result = None
        for i, card in enumerate(state["deck"]):
            test = bj_hand_value(dealer + [card])
            if max_val < test <= 21:
                if best_result is None or test < best_result:
                    best_result = test
                    best_idx = i
        if best_idx is not None:
            dealer.append(state["deck"].pop(best_idx))
            break

        # No winning card in one step — take the highest safe card and loop
        safe_idx = None
        safe_val = -1
        for i, card in enumerate(state["deck"]):
            test = bj_hand_value(dealer + [card])
            if test <= 21 and test > safe_val:
                safe_val = test
                safe_idx = i
        if safe_idx is None:
            break  # deck exhausted
        dealer.append(state["deck"].pop(safe_idx))

    # Hard rule: dealer MUST hit until 17+ — only when the rig hasn't already secured the win
    if bj_hand_value(dealer) <= max_val:
        while bj_hand_value(dealer) < 17:
            dealer.append(state["deck"].pop())


def _rig_initial_deal(state: dict) -> None:
    """
    Swap player's starting 2-card hand to a 12–16 total.
    Cards are drawn from the combined pool (current hand + deck)
    so the shoe stays consistent.
    """
    h    = state["hands"][0]["cards"]
    deck = state["deck"]

    combined = h[:] + deck[:]
    random.shuffle(combined)

    c1 = c2 = None
    for a in range(len(combined)):
        for b in range(a + 1, len(combined)):
            if 12 <= bj_hand_value([combined[a], combined[b]]) <= 16:
                c1, c2 = combined[a], combined[b]
                break
        if c1:
            break

    if not c1:
        return  # can't find valid pair — leave hand untouched

    combined.remove(c1)
    combined.remove(c2)
    h[0], h[1] = c1, c2
    state["deck"] = combined


def _is_tracking_exempt_user(user_id: int | str) -> bool:
    """Return True when user's bets must not be tracked in stats/history."""
    admins = get_data("server/admins") or {}
    permissions = admins.get(str(user_id), [])

    if isinstance(permissions, str):
        permissions = [permissions]
    if not isinstance(permissions, list):
        return False

    normalized = {str(p).lower() for p in permissions}
    return "admin" in normalized or "cashier" in normalized

# ─── Mines in-memory game state ──────────────────────────────────────────────
_active_mines: dict = {}  # str(message_id) → MinesState

# ─── Towers in-memory game state ─────────────────────────────────────────────
_active_towers: dict = {}  # str(message_id) → TowersState

# ─── HiLo in-memory game state ───────────────────────────────────────────────
_active_hilo: dict = {}  # str(message_id) → {state, mode, game_uid, ...}


# ─── Case Opening helpers ─────────────────────────────────────────────────────

def _get_cases_data() -> dict:
    """Load and bridge the new cases format (item_ids → items resolved).
    Returns: {"cases": merged_dict, "items": items_lib}
    where merged_dict contains both official and community cases with items resolved.
    """
    data = get_data("server/cases") or {}
    if not isinstance(data, dict):
        data = {}

    items_lib = data.get("items", {}) or {}
    if not isinstance(items_lib, dict):
        items_lib = {}

    def _resolve(item_ids):
        result = []
        for iid in (item_ids or []):
            item = items_lib.get(iid)
            if item and isinstance(item, dict):
                result.append({**item, "id": iid})
        return result

    merged = {}
    for cid, c in (data.get("cases", {}) or {}).items():
        if not isinstance(c, dict):
            continue
        items = _resolve(c.get("item_ids", []))
        # Include all official cases even without items (admin may not have added items yet)
        merged[cid] = {**c, "items": items, "is_community": False, "owner_id": None}

    for cid, c in (data.get("community_cases", {}) or {}).items():
        if not isinstance(c, dict):
            continue
        # Only include published community cases with at least one item
        if not c.get("published", False):
            continue
        items = _resolve(c.get("item_ids", []))
        if items:
            merged[cid] = {**c, "items": items, "is_community": True}

    return {"cases": merged, "items": items_lib}


def _get_user_favorites(user_id: int) -> list:
    """Return user's favourite case_id list."""
    favs = get_user_data(user_id, "case_favorites") or {}
    return favs.get("case_ids", []) if isinstance(favs, dict) else []


def _set_user_favorites(user_id: int, case_ids: list):
    """Persist user's favourite case_id list."""
    set_user_data(user_id, "case_favorites", {"case_ids": case_ids})


def _case_open_item(items: list) -> dict:
    """Ağırlıklı rastgele item seç. Yüksek değer = düşük ihtimal."""
    if not items:
        return {"name": "Boş", "emoji": "❓", "value": 0}
    weights = [1.0 / max(item.get("value", 1), 1) for item in items]
    total = sum(weights)
    r = random.uniform(0, total)
    cumulative = 0.0
    for item, w in zip(items, weights):
        cumulative += w
        if r <= cumulative:
            return item
    return items[-1]


def _case_open_item_pf(items: list, float_val: float) -> dict:
    """Provably Fair float ile ağırlıklı deterministik item seçimi."""
    if not items:
        return {"name": "Boş", "emoji": "❓", "value": 0}
    weights = [1.0 / max(item.get("value", 1), 1) for item in items]
    total = sum(weights)
    r = float_val * total
    cumulative = 0.0
    for item, w in zip(items, weights):
        cumulative += w
        if r <= cumulative:
            return item
    return items[-1]


# ─── Crystals per-type embed colors ──────────────────────────────────────────
_CRYSTAL_COLORS: dict = {
    "blue":   0x3498db,
    "white":  0xecf0f1,
    "black":  0x2c3e50,
    "purple": 0x9b59b6,
    "yellow": 0xf1c40f,
    "green":  0x2ecc71,
    "red":    0xe74c3c,
    "aqua":   0x1abc9c,
}
# Combo sonuç renkleri
_COMBO_COLORS: dict = {
    "quintuple":  0xffd700,  # altın
    "quadruple":  0xff8c00,  # turuncu
    "full_house": 0x8a2be2,  # mor
    "triple":     0x00bfff,  # mavi
    "two_pair":   0x32cd32,  # yeşil
    "one_pair":   0x778899,  # gri
    "no_match":   0xdc143c,  # kırmızı
}


def _get_towers_settings() -> dict:
    """server/games['towers'] içinden Towers emoji ayarlarını okur."""
    games_data = get_data("server/games") or {}
    tdata = games_data.get("towers", {}) if isinstance(games_data, dict) else {}
    if not isinstance(tdata, dict):
        tdata = {}

    emojis = tdata.get("emojis", {})
    if not isinstance(emojis, dict):
        emojis = {}

    game_emoji   = str(emojis.get("game",   "🗼") or "🗼")
    hidden_emoji = str(emojis.get("hidden", "🔮") or "🔮")
    gem_emoji    = str(emojis.get("gem",    "💎") or "💎")
    bomb_emoji   = str(emojis.get("bomb",   "💣") or "💣")

    rigged_chance = tdata.get("rigged_chance", 15.0)
    try:
        rigged_chance = float(rigged_chance)
    except (TypeError, ValueError):
        rigged_chance = 15.0
    rigged_chance = max(0.0, min(100.0, rigged_chance))

    return {
        "game":          game_emoji,
        "hidden":        hidden_emoji,
        "gem":           gem_emoji,
        "bomb":          bomb_emoji,
        "rigged_chance": rigged_chance,
    }


def _get_mines_settings() -> dict:
    """Read Mines house edge and emoji configuration from server/games."""
    games_data = get_data("server/games") or {}
    mines_data = games_data.get("mines", {}) if isinstance(games_data, dict) else {}
    if not isinstance(mines_data, dict):
        mines_data = {}

    house_edge_percent = mines_data.get("house_edge", 15.0)
    try:
        house_edge_percent = float(house_edge_percent)
    except (TypeError, ValueError):
        house_edge_percent = 15.0

    house_edge_percent = max(0.0, min(99.99, house_edge_percent))

    emojis = mines_data.get("emojis", {}) if isinstance(mines_data.get("emojis", {}), dict) else {}
    game_emoji = str(mines_data.get("emoji", "💣") or "💣")
    hidden_emoji = str(emojis.get("hidden", "❓") or "❓")
    gem_emoji = str(emojis.get("gem", "💎") or "💎")
    mine_emoji = str(emojis.get("mine", "💣") or "💣")

    rigged_chance = mines_data.get("rigged_chance", 5.0)
    try:
        rigged_chance = float(rigged_chance)
    except (TypeError, ValueError):
        rigged_chance = 5.0
    rigged_chance = max(0.0, min(100.0, rigged_chance))

    return {
        "house_edge_percent": house_edge_percent,
        "house_edge_decimal": house_edge_percent / 100.0,
        "game":          game_emoji,
        "hidden":        hidden_emoji,
        "gem":           gem_emoji,
        "mine":          mine_emoji,
        "rigged_chance": rigged_chance,
    }


def _get_limbo_settings() -> dict:
    """Read Limbo rigged_chance from server/games['limbo']."""
    games_data = get_data("server/games") or {}
    limbo_data = games_data.get("limbo", {}) if isinstance(games_data, dict) else {}
    if not isinstance(limbo_data, dict):
        limbo_data = {}
    rigged_chance = limbo_data.get("rigged_chance", 0.0)
    try:
        rigged_chance = float(rigged_chance)
    except (TypeError, ValueError):
        rigged_chance = 0.0
    rigged_chance = max(0.0, min(100.0, rigged_chance))
    return {"rigged_chance": rigged_chance}


def _get_crystals_settings() -> dict:
    """server/games['crystals'] içinden Crystals emoji ve çarpan ayarlarını okur."""
    games_data = get_data("server/games") or {}
    cdata = games_data.get("crystals", {}) if isinstance(games_data, dict) else {}
    if not isinstance(cdata, dict):
        cdata = {}

    emojis = cdata.get("emojis", {})
    if not isinstance(emojis, dict):
        emojis = {}

    # Kristal başına emoji sözlüğü
    crystal_emojis_raw = emojis.get("crystals", {})
    if not isinstance(crystal_emojis_raw, dict):
        crystal_emojis_raw = {}
    crystal_defaults = {
        "blue": "🔵", "white": "⚪", "black": "⚫", "purple": "🟣",
        "yellow": "🟡", "green": "🟢", "red": "🔴", "aqua": "💧",
    }
    crystal_emojis = {
        k: str(crystal_emojis_raw.get(k, crystal_defaults[k]) or crystal_defaults[k])
        for k in crystal_defaults
    }

    # Çarpanlar
    mults_raw = cdata.get("multipliers", {})
    if not isinstance(mults_raw, dict):
        mults_raw = {}
    default_mults = {
        "quintuple": 20.0, "quadruple": 4.80, "full_house": 3.84,
        "triple": 2.88, "two_pair": 1.92, "one_pair": 0.10, "no_match": 0.0,
    }
    multipliers = {}
    for k, dv in default_mults.items():
        try:
            multipliers[k] = float(mults_raw.get(k, dv))
        except (TypeError, ValueError):
            multipliers[k] = dv

    house_edge_pct = float(cdata.get("house_edge", 0.0) or 0.0)
    house_edge_pct = max(0.0, min(99.99, house_edge_pct))

    game_emoji     = str(emojis.get("game",     "💎") or "💎")
    hidden_emoji   = str(emojis.get("hidden",   "🔮") or "🔮")
    platform_emoji = str(emojis.get("platform", "〰️") or "〰️")
    bosluk_emoji   = str(emojis.get("bosluk",   "⬛") or "⬛")

    return {
        "game":            game_emoji,
        "hidden":          hidden_emoji,
        "platform":        platform_emoji,
        "bosluk":          bosluk_emoji,
        "crystal_emojis": crystal_emojis,
        "multipliers":     multipliers,
        "house_edge_pct": house_edge_pct,
    }


# ─── Crystals embed builders ──────────────────────────────────────────────────

def _crystals_board_str(revealed: list, total: int, settings: dict) -> str:
    """5 slotluk yatay kristal dizisi döndürür."""
    em = settings["crystal_emojis"]
    hidden = settings["hidden"]
    slots = [
        em.get(revealed[i], "💎") if i < len(revealed) else hidden
        for i in range(total)
    ]
    return f"{settings['bosluk']}".join(slots)


def _crystals_reveal_embed(
    user: discord.Member, revealed: list, bet: int, mode: str, step: int, settings: dict
) -> discord.Embed:
    """Açılış animasyonu sırasında gösterilen embed."""
    board = _crystals_board_str(revealed, 5, settings)
    last_type = revealed[-1] if revealed else None
    color = _CRYSTAL_COLORS.get(last_type, 0x3498db) if last_type else 0x3498db
    user_lang_id = str(user.id)
    mode_display = t("games.mode_demo", user_id=user_lang_id) if mode == "demo" else t("games.mode_real", user_id=user_lang_id)

    step_texts = [
        "",
        t("games.crystals.steps.1", user_id=user_lang_id),
        t("games.crystals.steps.2", user_id=user_lang_id),
        t("games.crystals.steps.3", user_id=user_lang_id),
        t("games.crystals.steps.4", user_id=user_lang_id),
        t("games.crystals.steps.5", user_id=user_lang_id),
    ]

    platform = settings.get("platform", "〰️")
    desc = (
        f"\n{board}\n{platform}{platform}{platform}{platform}{platform}{platform}{platform}{platform}{platform}\n\n"
        f"💰 **{t('games.bet', user_id=user_lang_id)}:** {format_balance(bet, mode)}\n🎮 **{t('games.mode', user_id=user_lang_id)}:** {mode_display}\n"
        f"⚡ **{step_texts[step]}** `[{step}/5]`"
    )
    embed = discord.Embed(
        title=f"{settings['game']}  C R Y S T A L S",
        description=desc,
        color=color,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=t("games.footer_crystals_pf", user_id=user_lang_id))
    return embed


def _crystals_result_embed(
    user: discord.Member, crystals: list, combo: str, mult: float,
    earnings: int, bet: int, mode: str, game_uid: str, settings: dict,
) -> discord.Embed:
    """Son sonuç embed'i."""
    from collections import Counter as _Counter
    board = _crystals_board_str(crystals, 5, settings)
    combo_label = CrystalsGame.COMBO_LABELS.get(combo, combo)
    color = _COMBO_COLORS.get(combo, 0x3498db)
    profit = earnings - bet
    sign = "+" if profit >= 0 else ""
    user_lang_id = str(user.id)
    mode_display = t("games.mode_demo", user_id=user_lang_id) if mode == "demo" else t("games.mode_real", user_id=user_lang_id)

    if mult > 1.0:
        result_line = (
            f"🏆 **{t('games.crystals.winnings', user_id=user_lang_id)}:** {format_balance(earnings, mode)} "
            f"(__**{sign}{format_balance(profit, mode)}**__)"
        )
    elif mult > 0.0:
        result_line = (
            f"🔸 **{t('games.crystals.recovered', user_id=user_lang_id)}:** {format_balance(earnings, mode)} "
            f"(__**{format_balance(-profit, mode)} {t('games.lost', user_id=user_lang_id).lower()}**__)"
        )
    else:
        result_line = f"💀 **{t('games.lost', user_id=user_lang_id)}:** {format_balance(bet, mode)}"

    # Eşleşen renkleri göster
    counts = _Counter(crystals)
    em = settings["crystal_emojis"]
    match_parts = [
        f"{em.get(ct, '💎')}×{n}"
        for ct, n in sorted(counts.items(), key=lambda x: -x[1])
        if n > 1
    ]
    match_str = "  ".join(match_parts)

    platform = settings.get("platform", "〰️")
    desc = (
        f"\n{board}\n{platform}{platform}{platform}{platform}{platform}{platform}{platform}{platform}{platform}\n\n"
        f"🎴 **{t('games.crystals.combo', user_id=user_lang_id)}:** {combo_label}\n"
        f"📊 **{t('games.mines.multiplier', user_id=user_lang_id)}:** `{mult:.2f}x`\n"
        f"💰 **{t('games.bet', user_id=user_lang_id)}:** {format_balance(bet, mode)}\n🎮 **{t('games.mode', user_id=user_lang_id)}:** {mode_display}\n"
        f"{result_line}\n"
    )
    if match_str:
        desc += f"💠 **{t('games.crystals.matching', user_id=user_lang_id)}:** {match_str}\n"
    desc += f"\n🔐 **{t('games.game_id', user_id=user_lang_id)}:** `{game_uid}`"

    embed = discord.Embed(
        title=f"{settings['game']}  C R Y S T A L S  —  {combo_label}",
        description=desc,
        color=color,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=t("games.footer_crystals_pf", user_id=user_lang_id))
    return embed


# ─── Crystals Discord UI ─────────────────────────────────────────────────────

class CrystalsPlayButton(discord.ui.Button):
    """Crystals oyununu başlatan ve animasyonu yöneten buton."""

    def __init__(self, message_id: str):
        session = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        settings = _get_crystals_settings()
        super().__init__(
            label=t("games.crystals.reveal_button", user_id=owner_id),
            style=discord.ButtonStyle.success,
            row=3,
            custom_id=f"crystals_play:{message_id}",
            emoji=settings["game"],
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.access_denied_title", user_id=user_lang_id),
                    description=t("games.errors.not_your_session", user_id=user_lang_id),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )

        if not GameSession.is_session_active(self.message_id):
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.session_expired_title", user_id=user_lang_id),
                    description=t("games.errors.session_expired_desc", user_id=user_lang_id),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )

        session = GameSession.get_session(self.message_id)
        if not session:
            return await interaction.response.send_message(
                t("games.errors.session_not_found", user_id=user_lang_id),
                ephemeral=True,
            )

        bet  = int(session.get("bet", 100))
        mode = session.get("mode", "demo")

        player = Player(interaction.user.id)

        # Bakiye kontrolü (free-round promo varsa atlanır)
        if not CrystalsGame().can_afford_bet(player, mode, bet):
            balance = player.get_balance(mode)
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                    description=t(
                        "games.errors.insufficient_balance_desc",
                        user_id=user_lang_id,
                        need=format_balance(bet, mode),
                        have=format_balance(balance, mode),
                    ),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )

        # ── Provably Fair + kristal üretimi ───────────────────────────────────
        from modules.provably_fair import consume_pf_round, hash_seed, log_game_start, new_game_uid
        server_seed, client_seed, nonce, _ = consume_pf_round(interaction.user.id)
        game_uid = new_game_uid()
        crystals = CrystalsGame.generate_crystals(server_seed, client_seed, nonce)
        combo    = CrystalsGame.get_combo(crystals)
        settings = _get_crystals_settings()
        mult     = CrystalsGame.get_multiplier(combo, settings["multipliers"])
        earnings = int(bet * mult)
        profit   = earnings - bet

        # ── Bahisi düş (free-round varsa bakiye kesilmez) ────────────────────────
        crystals_game  = CrystalsGame()
        is_free_round, bet = crystals_game.deduct_bet(player, mode, bet)
        # Recalculate earnings with effective bet
        earnings = int(bet * mult)
        profit   = earnings - bet
        GameSession.update_session(self.message_id, in_game=True)
        GameSession.touch_session(self.message_id)

        # Defer — animasyon boyunca message.edit kullanacağız
        await interaction.response.defer()

        # PF log başlangıç
        log_msg = await log_game_start(
            interaction, "Crystals", settings["game"], interaction.user,
            bet, mode, hash_seed(server_seed), client_seed, nonce, game_uid,
        )

        # ── Reveal animasyonu ─────────────────────────────────────────────────
        from modules.games_play_v2 import crystals_reveal_items

        revealed: list = []
        for i in range(1, 6):
            revealed.append(crystals[i - 1])
            embed = _crystals_reveal_embed(interaction.user, revealed, bet, mode, i, settings)
            try:
                await interaction.message.edit(
                    embed=None,
                    content=None,
                    view=play_layout(embed, crystals_reveal_items(), timeout=None),
                )
            except Exception:
                pass
            await asyncio.sleep(0.9)

        # ── Sonucu kaydet ─────────────────────────────────────────────────────
        if mult > 1.0:
            result_str = "win"
        elif mult == 1.0:
            result_str = "tie"
        else:
            result_str = "lose"
        game_result = GameResult(
            result=result_str,
            bet=bet,
            multiplier=mult,
            meta={"crystals": crystals, "combo": combo},
            amount=earnings,
        )
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        last_game_info = crystals_game.handle_result(game_result, player, mode, member=member, is_free_round=is_free_round)

        # Kısmi kayıp (0 < mult < 1): handle_result sıfır ödedi
        # → kazanılan kısmı geri ekle (örn. one_pair = 0.10x) — sadece normal roundlarda
        if result_str == "lose" and earnings > 0 and not is_free_round:
            player.add_balance(mode, earnings)

        if mode == "real" and member:
            from Games.base_game import _check_and_assign_tier_role
            await _check_and_assign_tier_role(member, player)
            levels_cog = interaction.client.cogs.get("LevelsCog")
            if levels_cog:
                await levels_cog.process_level_up(interaction.user.id)

        # ── Event hooks ──────────────────────────────────────────────────────
        from modules.event_manager import process_game_event
        from cogs.events import send_event_completion
        _CRYSTALS_WIN = {"triple", "full_house", "quadruple", "quintuple"}
        _ev_crystals = process_game_event(
            interaction.user.id,
            {"game": "crystals", "combo": combo, "won": combo in _CRYSTALS_WIN,
             "bet": bet, "mode": mode},
            player,
        )

        # ── Sonuç embed'i ─────────────────────────────────────────────────────
        result_embed = _crystals_result_embed(
            interaction.user, crystals, combo, mult, earnings, bet, mode, game_uid, settings,
        )
        try:
            await interaction.message.edit(
                embed=None,
                content=None,
                view=play_layout(result_embed, crystals_reveal_items(), timeout=None),
            )
        except Exception:
            pass

        # ── PF log sonu ───────────────────────────────────────────────────────
        from modules.provably_fair import log_game_end
        try:
            await log_game_end(
                log_msg, "Crystals", settings["game"], interaction.user,
                bet, mode, server_seed, client_seed, nonce, game_uid,
                result_str,
                {"crystals": crystals, "combo": combo, "multiplier": mult},
                profit,
            )
        except Exception:
            pass

        GameSession.update_session(self.message_id, last_game=last_game_info, in_game=False)
        if _ev_crystals:
            await send_event_completion(interaction, _ev_crystals)

        # ── Menüyü geri yükle ─────────────────────────────────────────────────
        await asyncio.sleep(3)
        session_now = GameSession.get_session(self.message_id)
        if session_now:
            layout = hub_active_layout(self.message_id, interaction.user, session_now, "crystals")
            try:
                await interaction.message.edit(embed=None, content=None, view=layout)
            except Exception:
                pass


class MinesState:
    """In-memory state for one active mines game."""

    __slots__ = (
        "owner_id", "board", "mine_count", "bet", "mode",
        "revealed", "diamonds", "server_seed", "client_seed",
        "nonce", "game_uid", "pf_log_msg", "message_id", "is_free_round",
    )

    def __init__(self, owner_id, board, mine_count, bet, mode,
                 server_seed, client_seed, nonce, game_uid, message_id, is_free_round: bool = False):
        self.owner_id = owner_id
        self.board = board
        self.mine_count = mine_count
        self.bet = bet
        self.mode = mode
        self.revealed: set = set()
        self.diamonds: int = 0
        self.server_seed = server_seed
        self.client_seed = client_seed
        self.nonce = nonce
        self.game_uid = game_uid
        self.pf_log_msg = None
        self.message_id = str(message_id)
        self.is_free_round: bool = is_free_round


class GameSession:
    """Oyun oturumu yöneticisi"""
    
    @staticmethod
    def create_session(message_id: str, user_id: int, channel_id: int):
        """Yeni oyun oturumu oluştur"""
        sessions = get_data("server/game_sessions")
        user_prefs = get_data(f"{user_id}/selected_bet")
        
        # Kullanıcının kaydedilmiş tercihlerini yükle
        if not user_prefs:
            user_prefs = {"bet": 100, "mode": "demo"}
        
        sessions[str(message_id)] = {
            "owner": user_id,
            "channel_id": channel_id,
            "created_at": int(time.time()),
            "last_activity": int(time.time()),
            "in_game": False,
            "game": None,
            "bet": user_prefs.get("bet", 100),
            "mode": user_prefs.get("mode", "demo"),
            "last_game": None
        }
        set_data("server/game_sessions", sessions)
    
    @staticmethod
    def get_session(message_id: str):
        """Oyun oturumunu getir"""
        sessions = get_data("server/game_sessions")
        return sessions.get(str(message_id))
    
    @staticmethod
    def update_session(message_id: str, **kwargs):
        """Oyun oturumunu güncelle"""
        sessions = get_data("server/game_sessions")
        if str(message_id) in sessions:
            sessions[str(message_id)].update(kwargs)
            set_data("server/game_sessions", sessions)
    
    @staticmethod
    def delete_session(message_id: str):
        """Oyun oturumunu sil"""
        sessions = get_data("server/game_sessions")
        if str(message_id) in sessions:
            del sessions[str(message_id)]
            replace_data("server/game_sessions", sessions)
    
    @staticmethod
    def check_owner(message_id: str, user_id: int) -> bool:
        """Kullanıcı oturum sahibi mi kontrol et"""
        session = GameSession.get_session(message_id)
        return session and session["owner"] == user_id
    
    @staticmethod
    def touch_session(message_id: str):
        """Session aktivitesini güncelle"""
        sessions = get_data("server/game_sessions")
        if str(message_id) in sessions:
            sessions[str(message_id)]["last_activity"] = int(time.time())
            set_data("server/game_sessions", sessions)
    
    @staticmethod
    def is_session_active(message_id: str, timeout: int = 60) -> bool:
        """Session aktif mi kontrol et (timeout saniye)"""
        session = GameSession.get_session(message_id)
        if not session:
            return False
        
        last_activity = session.get("last_activity", session["created_at"])
        current_time = int(time.time())
        return (current_time - last_activity) < timeout
    
    @staticmethod
    def find_user_session(user_id: int, timeout: int = 60) -> tuple[str, int] | None:
        """Kullanıcının aktif session'ını bul (varsa (message_id, last_activity) döndürür)"""
        sessions = get_data("server/game_sessions")
        current_time = int(time.time())
        for message_id, session in sessions.items():
            if session.get("owner") == user_id:
                last_activity = session.get("last_activity", session.get("created_at", 0))
                if (current_time - last_activity) < timeout:
                    return message_id, last_activity
        return None

    @staticmethod
    def save_user_preferences(user_id: int, bet: int = None, mode: str = None):
        """Kullanıcı tercihlerini kalıcı olarak kaydet"""
        user_prefs = get_data(f"{user_id}/selected_bet")
        
        if not user_prefs:
            user_prefs = {"bet": 100, "mode": "demo"}
        
        if bet is not None:
            user_prefs["bet"] = bet
        if mode is not None:
            user_prefs["mode"] = mode
        
        set_data(f"{user_id}/selected_bet", user_prefs)


# ─── Mines embed builders ────────────────────────────────────────────────────

def _mines_playing_embed(user: discord.Member, state: "MinesState") -> discord.Embed:
    settings = _get_mines_settings()
    safe_total = MinesGame.TOTAL - state.mine_count
    mult = MinesGame.calc_multiplier(state.mine_count, state.diamonds, settings["house_edge_decimal"])
    next_mult = MinesGame.calc_multiplier(state.mine_count, state.diamonds + 1, settings["house_edge_decimal"])
    earnings = int(state.bet * mult)
    next_earnings = int(state.bet * next_mult)
    user_lang_id = str(user.id)
    mode_display = (
        t("games.mode_demo", user_id=user_lang_id)
        if state.mode == "demo"
        else t("games.mode_real", user_id=user_lang_id)
    )

    if state.diamonds > 0:
        bar = settings["gem"] * min(state.diamonds, 10) + "⬜" * min(safe_total - state.diamonds, 10)
        progress_line = f"{bar}  `{state.diamonds}/{safe_total}`"
        mult_line = (
            f"📊 **{t('games.mines.multiplier', user_id=user_lang_id)}:** `{mult:.2f}x` ➜ `{next_mult:.2f}x`\n"
            f"💵 **{t('games.mines.earnings', user_id=user_lang_id)}:** {format_balance(earnings, state.mode)}\n"
            f"⏭️ **{t('games.mines.next_gem', user_id=user_lang_id)}:** {format_balance(next_earnings, state.mode)}"
        )
    else:
        bar = "⬜" * min(safe_total, 10)
        progress_line = f"{bar}  `0/{safe_total}`"
        mult_line = (
            f"📊 **{t('games.mines.pick_gem_start', user_id=user_lang_id)}**\n"
            f"⏭️ **{t('games.mines.first_gem', user_id=user_lang_id)}:** `{next_mult:.2f}x` → {format_balance(next_earnings, state.mode)}"
        )

    desc = (
        f"```\n💣 {t('games.mines.mines_label', user_id=user_lang_id)}: {state.mine_count}   💎 {t('games.mines.safe_cells_label', user_id=user_lang_id)}: {safe_total}\n```\n"
        f"💰 **{t('games.bet', user_id=user_lang_id)}:** {format_balance(state.bet, state.mode)} │ 🎮 **{t('games.mode', user_id=user_lang_id)}:** {mode_display}\n\n"
        f"**{t('games.progress', user_id=user_lang_id)}:** {progress_line}\n{mult_line}\n\n"
        f"🔐 **{t('games.game_id', user_id=user_lang_id)}:** `{state.game_uid}`"
    )
    if getattr(state, "is_free_round", False):
        active_promo = promo_engine.get_active_promo(state.owner_id) or {}
        rounds_total  = int(active_promo.get("rounds_total", 0))
        rounds_played = int(active_promo.get("rounds_played", 0))
        total_won     = int(active_promo.get("total_winnings", 0))
        desc = (
            f"🎟️ **FREE BET ROUND** — `{rounds_played + 1}` / `{rounds_total}`\n"
            f"💰 **Total winnings so far:** {format_balance(total_won, 'real')}\n\n"
        ) + desc
    color = 0x2ecc71 if state.diamonds > 0 else 0x3498db
    embed = discord.Embed(title=f"{settings['game']}  M I N E S", description=desc, color=color)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=t("games.footer_mines_pf", user_id=user_lang_id))
    return embed


def _mines_cashout_embed(user: discord.Member, state: "MinesState", mult: float, earnings: int) -> discord.Embed:
    profit = earnings - state.bet
    sign = "+" if profit >= 0 else ""
    user_lang_id = str(user.id)
    embed = discord.Embed(
        title=t("games.mines.cashed_out_title", user_id=user_lang_id),
        description=(
            f"**💎 {t('games.mines.diamonds_collected', user_id=user_lang_id)}:** {state.diamonds}\n"
            f"**📊 {t('games.mines.final_multiplier', user_id=user_lang_id)}:** `{mult:.2f}x`\n"
            f"**💰 {t('games.bet', user_id=user_lang_id)}:** {format_balance(state.bet, state.mode)}\n"
            f"**🏆 {t('games.mines.winnings', user_id=user_lang_id)}:** {format_balance(earnings, state.mode)} "
            f"(__**{sign}{format_balance(profit, state.mode)}**__)\n\n"
            f"🔐 **{t('games.game_id', user_id=user_lang_id)}:** `{state.game_uid}`"
        ),
        color=0x2ecc71,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=t("games.footer_mines_pf", user_id=user_lang_id))
    return embed


def _mines_bomb_embed(user: discord.Member, state: "MinesState") -> discord.Embed:
    user_lang_id = str(user.id)
    embed = discord.Embed(
        title=t("games.mines.boom_title", user_id=user_lang_id),
        description=(
            f"**💣 {t('games.mines.hit_mine', user_id=user_lang_id)}**\n"
            f"**💎 {t('games.mines.diamonds_before_boom', user_id=user_lang_id)}:** {state.diamonds}\n"
            f"**💀 {t('games.lost', user_id=user_lang_id)}:** {format_balance(state.bet, state.mode)}\n\n"
            f"🔐 **{t('games.game_id', user_id=user_lang_id)}:** `{state.game_uid}`"
        ),
        color=0xe74c3c,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=t("games.footer_mines_pf", user_id=user_lang_id))
    return embed


# ─── Mines game-end helpers ───────────────────────────────────────────────────

async def _mines_restore_menu(interaction: discord.Interaction, state: "MinesState") -> None:
    """Wait 3 s then restore the game menu in the original message."""
    await asyncio.sleep(3)
    session = GameSession.get_session(state.message_id)
    if session:
        GameSession.update_session(state.message_id, in_game=False)
        session = GameSession.get_session(state.message_id)
        layout = hub_active_layout(state.message_id, interaction.user, session, "mines")
        try:
            await interaction.message.edit(embed=None, content=None, view=layout)
        except Exception:
            pass
    _active_mines.pop(state.message_id, None)


async def _mines_do_cashout(interaction: discord.Interaction, state: "MinesState") -> None:
    """Pay out the cashout, log PF, and restore the menu."""
    settings = _get_mines_settings()
    mult = MinesGame.calc_multiplier(state.mine_count, state.diamonds, settings["house_edge_decimal"])
    earnings = int(state.bet * mult)
    profit = earnings - state.bet
    player = Player(state.owner_id)
    result_str = "win" if earnings > state.bet else "lose"
    game_result = GameResult(result=result_str, bet=state.bet, multiplier=mult, amount=earnings)
    member = interaction.user if isinstance(interaction.user, discord.Member) else None

    # handle_result manages free-round promo tracking automatically
    last_game_info = MinesGame().handle_result(game_result, player, state.mode, member=member,
                                               is_free_round=state.is_free_round)

    promo_done     = last_game_info.get("promo_done", False)
    promo_credited = last_game_info.get("promo_credited", 0)
    promo_active   = last_game_info.get("promo_active", False)

    if state.mode == "real" and member:
        from Games.base_game import _check_and_assign_tier_role
        await _check_and_assign_tier_role(member, player)
        levels_cog = interaction.client.cogs.get("LevelsCog")
        if levels_cog:
            await levels_cog.process_level_up(state.owner_id)

    # ── Event hooks ──────────────────────────────────────────────────────────
    from modules.event_manager import process_game_event
    from cogs.events import send_event_completion
    _ev_completed = process_game_event(
        state.owner_id,
        {"game": "mines", "won": True, "gems_found": state.diamonds,
         "mine_count": state.mine_count, "multiplier": mult,
         "bet": state.bet, "mode": state.mode},
        player,
    )

    if state.is_free_round:
        rounds_total      = last_game_info.get("promo_rounds_left", 0) + (1 if not promo_done else 0)
        rounds_played_now = int((promo_engine.get_active_promo(state.owner_id) or {}).get("rounds_played", 0))
        total_won_now     = last_game_info.get("promo_total_won", earnings)
        if promo_done:
            fr_embed = discord.Embed(
                title="🎰  Free Bet — All Rounds Complete!",
                description=(
                    f"**💎 Diamonds collected:** {state.diamonds}  ·  **Multiplier:** `{mult:.2f}×`\n"
                    f"**This round winnings:** {format_balance(earnings, 'real')}\n\n"
                    f"✅ **Total winnings credited:** {format_balance(promo_credited, 'real')}\n"
                    f"⚠️ A **1× wager requirement** of {format_balance(promo_credited, 'real')} has been added.\n\n"
                    f"> Complete your wagering by playing real-money games!"
                ),
                color=discord.Color.green(),
            )
        else:
            fr_embed = discord.Embed(
                title="🎟️  Free Round — Cashed Out!",
                description=(
                    f"**💎 Diamonds:** {state.diamonds}  ·  **{mult:.2f}×**\n"
                    f"**This round:** {format_balance(earnings, 'real')}\n\n"
                    f"📊 **Progress:** Round **{rounds_played_now}** / **{rounds_total + rounds_played_now}**\n"
                    f"💰 **Total so far:** {format_balance(total_won_now, 'real')}\n\n"
                    f"> Start your next free round from the 🎲 Games menu!"
                ),
                color=0x9b59b6,
            )
        fr_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        fr_embed.set_footer(text="Vegas Casino | Free Bet System")
        embed = fr_embed
    else:
        embed = _mines_cashout_embed(interaction.user, state, mult, earnings)
    from modules.games_play_v2 import mines_grid_items

    await interaction.response.edit_message(
        embed=None,
        content=None,
        view=play_layout(embed, mines_grid_items(state, game_over=True)),
    )

    from modules.provably_fair import log_game_end
    try:
        await log_game_end(
            state.pf_log_msg, "Mines", settings["game"], interaction.user,
            state.bet, state.mode, state.server_seed, state.client_seed,
            state.nonce, state.game_uid, result_str,
            {"diamonds": state.diamonds, "mines": state.mine_count, "multiplier": mult},
            profit,
        )
    except Exception:
        pass

    GameSession.update_session(state.message_id, last_game=last_game_info)
    if _ev_completed:
        await send_event_completion(interaction, _ev_completed)
    await _mines_restore_menu(interaction, state)


async def _mines_do_bomb(interaction: discord.Interaction, state: "MinesState") -> None:
    """Record the loss, log PF, and restore the menu."""
    settings = _get_mines_settings()
    player = Player(state.owner_id)
    member = interaction.user if isinstance(interaction.user, discord.Member) else None

    game_result = GameResult(result="lose", bet=state.bet, multiplier=0.0)
    # handle_result manages free-round promo tracking automatically
    last_game_info = MinesGame().handle_result(game_result, player, state.mode, member=member,
                                               is_free_round=state.is_free_round)

    promo_done     = last_game_info.get("promo_done", False)
    promo_credited = last_game_info.get("promo_credited", 0)
    promo_active   = last_game_info.get("promo_active", False)

    if state.mode == "real" and member:
        levels_cog = interaction.client.cogs.get("LevelsCog")
        if levels_cog:
            await levels_cog.process_level_up(state.owner_id)

    # ── Event hooks ──────────────────────────────────────────────────────────
    from modules.event_manager import process_game_event
    process_game_event(
        state.owner_id,
        {"game": "mines", "won": False, "gems_found": state.diamonds,
         "mine_count": state.mine_count, "bet": state.bet, "mode": state.mode},
        player,
    )

    if state.is_free_round:
        rounds_played_now = int((promo_engine.get_active_promo(state.owner_id) or {}).get("rounds_played", 0))
        total_won_now     = last_game_info.get("promo_total_won", 0)
        rounds_total_left = last_game_info.get("promo_rounds_left", 0)
        if promo_done:
            fr_embed = discord.Embed(
                title="💣  Free Bet — All Rounds Complete!",
                description=(
                    f"**💣 Bomb hit!**  No winnings this round.\n\n"
                    f"✅ **Total winnings credited:** {format_balance(promo_credited, 'real')}\n"
                    + (f"⚠️ A **1× wager requirement** of {format_balance(promo_credited, 'real')} has been added.\n\n"
                       f"> Complete your wagering by playing real-money games!" if promo_credited > 0
                       else "> No winnings to credit — better luck next time!")
                ),
                color=discord.Color.orange(),
            )
        else:
            fr_embed = discord.Embed(
                title="💣  Free Round — Bomb Hit!",
                description=(
                    f"**💣 Bomb hit!**  No winnings this round.\n\n"
                    f"📊 **Progress:** Round **{rounds_played_now}** / **{rounds_played_now + rounds_total_left}**\n"
                    f"💰 **Total so far:** {format_balance(total_won_now, 'real')}\n\n"
                    f"> Start your next free round from the 🎲 Games menu!"
                ),
                color=discord.Color.red(),
            )
        fr_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        fr_embed.set_footer(text="Vegas Casino | Free Bet System")
        embed = fr_embed
    else:
        embed = _mines_bomb_embed(interaction.user, state)
    from modules.games_play_v2 import mines_grid_items

    await interaction.response.edit_message(
        embed=None,
        content=None,
        view=play_layout(embed, mines_grid_items(state, game_over=True)),
    )

    from modules.provably_fair import log_game_end
    try:
        await log_game_end(
            state.pf_log_msg, "Mines", settings["game"], interaction.user,
            state.bet, state.mode, state.server_seed, state.client_seed,
            state.nonce, state.game_uid, "lose",
            {"diamonds": state.diamonds, "mines": state.mine_count},
            -state.bet,
        )
    except Exception:
        pass

    GameSession.update_session(state.message_id, last_game=last_game_info)
    await _mines_restore_menu(interaction, state)


# ─── Mines Discord UI ─────────────────────────────────────────────────────────

class MinesCell(discord.ui.Button):
    """One unrevealed cell in the mines grid."""

    def __init__(self, message_id: str, r: int, c: int):
        settings = _get_mines_settings()
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji=settings["hidden"],
            row=r,
            custom_id=f"mc_{r}{c}_{message_id}",
        )
        self.r = r
        self.c = c
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        mid = str(interaction.message.id)
        state = _active_mines.get(mid)
        if not state:
            return await interaction.response.send_message(
                t("games.errors.game_not_found", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        if interaction.user.id != state.owner_id:
            return await interaction.response.send_message(
                t("games.errors.not_your_game", user_id=str(interaction.user.id)),
                ephemeral=True,
            )

        cell = (self.r, self.c)
        if cell in state.revealed:
            return await interaction.response.defer()

        state.revealed.add(cell)
        GameSession.touch_session(mid)

        if state.board[self.r][self.c] == 1:
            await _mines_do_bomb(interaction, state)
        else:
            settings = _get_mines_settings()
            rigged_chance = settings["rigged_chance"] / 100.0
            if random.random() < rigged_chance:
                # Gizli tuzak: güvenli hücre anlık bomba olarak işaretlenir.
                # Mayın sayısını korumak için rastgele bir mayını gem'e çevir.
                unrevealed_mines = [
                    (r, c)
                    for r in range(MinesGame.ROWS)
                    for c in range(MinesGame.COLS)
                    if (r, c) not in state.revealed
                    and (r, c) != (self.r, self.c)
                    and state.board[r][c] == 1
                ]
                if unrevealed_mines:
                    swap_r, swap_c = random.choice(unrevealed_mines)
                    state.board[swap_r][swap_c] = 0
                state.board[self.r][self.c] = 1
                await _mines_do_bomb(interaction, state)
            else:
                state.diamonds += 1
                safe_total = MinesGame.TOTAL - state.mine_count
                if state.diamonds >= safe_total:
                    # All safe cells found — auto-cashout
                    await _mines_do_cashout(interaction, state)
                else:
                    embed = _mines_playing_embed(interaction.user, state)
                    from modules.games_play_v2 import mines_grid_items

                    await interaction.response.edit_message(
                        embed=None,
                        content=None,
                        view=play_layout(embed, mines_grid_items(state)),
                    )


class MinesCashoutButton(discord.ui.Button):
    """Cashout button placed in row 4 of the mines grid."""

    def __init__(self, message_id: str, mult: float, earnings: int, mode: str, disabled: bool):
        from modules.database import get_data as _gd
        _srv = _gd("server/server") or {}
        coin_emoji = (
            _srv.get("demo_coin_emoji", "💎") if mode == "demo"
            else _srv.get("coin_emoji", "💵")
        )
        user_lang_id = str((GameSession.get_session(message_id) or {}).get("owner", 0))
        cashout_label = t("games.mines.cashout", user_id=user_lang_id)
        label = f"{cashout_label}  {mult:.2f}x  ·  {earnings:,}" if not disabled else cashout_label
        super().__init__(
            label=label,
            emoji=coin_emoji,
            style=discord.ButtonStyle.primary if not disabled else discord.ButtonStyle.secondary,
            row=4,
            disabled=disabled,
            custom_id=f"mco_{message_id}",
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        mid = str(interaction.message.id)
        state = _active_mines.get(mid)
        if not state:
            return await interaction.response.send_message(
                t("games.errors.game_not_found", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        if interaction.user.id != state.owner_id:
            return await interaction.response.send_message(
                t("games.errors.not_your_game", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        if state.diamonds == 0:
            return await interaction.response.defer()
        GameSession.touch_session(mid)
        await _mines_do_cashout(interaction, state)


class MinesGridView(discord.ui.View):
    """Interactive 5×4 mines grid (20 cells) + cashout button (row 4, last slot)."""

    def __init__(self, state: "MinesState", game_over: bool = False):
        super().__init__(timeout=600)
        settings = _get_mines_settings()
        mult = MinesGame.calc_multiplier(state.mine_count, state.diamonds, settings["house_edge_decimal"])
        earnings = int(state.bet * mult)

        for r in range(4):
            for c in range(5):
                cell = (r, c)
                if cell in state.revealed:
                    is_mine = state.board[r][c] == 1
                    btn = discord.ui.Button(
                        style=discord.ButtonStyle.danger if is_mine else discord.ButtonStyle.success,
                        emoji=settings["mine"] if is_mine else settings["gem"],
                        row=r,
                        disabled=True,
                        custom_id=f"mr_{r}{c}_{state.message_id}",
                    )
                elif game_over:
                    is_mine = state.board[r][c] == 1
                    btn = discord.ui.Button(
                        style=discord.ButtonStyle.danger if is_mine else discord.ButtonStyle.secondary,
                        emoji=settings["mine"] if is_mine else settings["gem"],
                        row=r,
                        disabled=True,
                        custom_id=f"mo_{r}{c}_{state.message_id}",
                    )
                else:
                    btn = MinesCell(state.message_id, r, c)
                self.add_item(btn)

        # Cashout button (row 4, 5th position = last slot)
        cashout_disabled = state.diamonds == 0 or game_over
        self.add_item(MinesCashoutButton(state.message_id, mult, earnings, state.mode, cashout_disabled))


class MineCountSelect(discord.ui.Select):
    """Dropdown to choose mine count before the game starts."""

    _PRESETS = [
        (1,  "Very Easy"),
        (2,  "Easy"),
        (3,  "Easy"),
        (5,  "Medium"),
        (7,  "Medium"),
        (10, "Hard"),
        (15, "Very Hard"),
        (19, "God Mode"),
    ]

    def __init__(self, message_id: str):
        self.message_id = message_id
        session = GameSession.get_session(message_id)
        current = session.get("mines_count", 3) if session else 3
        owner_id = str(session.get("owner", 0)) if session else "0"
        settings = _get_mines_settings()

        options = []
        for n, label in self._PRESETS:
            first_mult = MinesGame.calc_multiplier(n, 1, settings["house_edge_decimal"])
            label_key = label.lower().replace(" ", "_")
            options.append(discord.SelectOption(
                label=f"{n} {t('games.mines.mines_word', user_id=owner_id)} — {t(f'games.mines.difficulty.{label_key}', user_id=owner_id)}",
                description=(
                    f"{t('games.mines.first_gem', user_id=owner_id)}: {first_mult:.2f}x  |  "
                    f"{t('games.mines.safe_cells_label', user_id=owner_id)}: {MinesGame.TOTAL - n}"
                ),
                value=str(n),
                emoji=settings["game"],
                default=(n == current),
            ))

        super().__init__(
            placeholder=t("games.mines.choose_count", user_id=owner_id),
            options=options,
            min_values=1,
            max_values=1,
            custom_id=f"mines_count:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        mine_count = int(self.values[0])
        GameSession.update_session(self.message_id, mines_count=mine_count)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "mines")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class StartMinesButton(discord.ui.Button):
    """Launches the mines game after the player has chosen mine count and bet."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        super().__init__(
            label=t("games.start_game", user_id=owner_id),
            style=discord.ButtonStyle.success,
            row=3,
            custom_id=f"mines_start:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=user_lang_id),
                ephemeral=True,
            )

        if not GameSession.is_session_active(self.message_id):
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.session_expired_title", user_id=user_lang_id),
                    description=t("games.errors.session_expired_desc", user_id=user_lang_id),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )

        session = GameSession.get_session(self.message_id)
        if not session:
            return await interaction.response.send_message(
                t("games.errors.session_not_found", user_id=user_lang_id),
                ephemeral=True,
            )

        bet = int(session.get("bet", 100))
        mode = session.get("mode", "demo")
        mine_count = int(session.get("mines_count", 3))

        player     = Player(interaction.user.id)
        mines_game = MinesGame()

        # Bakiye kontrolü (free-round promo varsa atlanır)
        if not mines_game.can_afford_bet(player, mode, bet):
            balance = player.get_balance(mode)
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                    description=t(
                        "games.errors.insufficient_balance_desc",
                        user_id=user_lang_id,
                        need=format_balance(bet, mode),
                        have=format_balance(balance, mode),
                    ),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )

        from modules.provably_fair import consume_pf_round, hash_seed, log_game_start, new_game_uid
        server_seed, client_seed, nonce, _ = consume_pf_round(interaction.user.id)
        game_uid = new_game_uid()
        board = MinesGame.generate_board(server_seed, client_seed, nonce, mine_count)
        settings = _get_mines_settings()

        # Bahisi düş (free-round varsa bakiye kesilmez, bet promo'dan alınır)
        is_free_round, bet = mines_game.deduct_bet(player, mode, bet)

        mid = str(interaction.message.id)
        state = MinesState(
            owner_id=interaction.user.id,
            board=board,
            mine_count=mine_count,
            bet=bet,
            mode=mode,
            server_seed=server_seed,
            client_seed=client_seed,
            nonce=nonce,
            game_uid=game_uid,
            message_id=mid,
            is_free_round=is_free_round,
        )
        _active_mines[mid] = state

        log_msg = await log_game_start(
            interaction, "Mines", settings["game"], interaction.user,
            bet, mode, hash_seed(server_seed), client_seed, nonce, game_uid,
        )
        state.pf_log_msg = log_msg

        GameSession.update_session(mid, in_game=True)
        GameSession.touch_session(mid)

        embed = _mines_playing_embed(interaction.user, state)
        from modules.games_play_v2 import mines_grid_items

        await interaction.response.edit_message(
            embed=None,
            content=None,
            view=play_layout(embed, mines_grid_items(state)),
        )


# ════════════════════════════════════════════════════════════════════════════
# TOWERS
# ════════════════════════════════════════════════════════════════════════════

class TowersState:
    """In-memory state for one active towers game."""

    __slots__ = (
        "owner_id", "tower_mode", "bet", "mode",
        "bomb_positions",   # list[int]: bomb column index per floor (0-indexed)
        "current_floor",    # int: index of the NEXT floor to play (0 = floor 1, 10 = all cleared)
        "picked",           # dict[int, int]: floor_idx → chosen column
        "server_seed", "client_seed", "nonce", "game_uid", "pf_log_msg", "message_id",
        "is_free_round",
    )

    def __init__(self, owner_id, tower_mode, bet, mode,
                 bomb_positions, server_seed, client_seed, nonce, game_uid, message_id,
                 is_free_round: bool = False):
        self.owner_id = owner_id
        self.tower_mode = tower_mode
        self.bet = bet
        self.mode = mode
        self.bomb_positions = bomb_positions
        self.current_floor = 0
        self.picked: dict = {}
        self.server_seed = server_seed
        self.client_seed = client_seed
        self.nonce = nonce
        self.game_uid = game_uid
        self.pf_log_msg = None
        self.message_id = str(message_id)
        self.is_free_round: bool = is_free_round


# ── Towers board string builder ───────────────────────────────────────────────

def _towers_board_str(state: "TowersState", settings: dict, reveal_all: bool = False) -> str:
    """
    Kuleyi yukarıdan aşağıya text olarak oluşturur.

    - Geçilen katlar: gem / bomb / bosluk gösterilir.
    - Aktif kat:      hidden emoji (▶ marker).
    - Üst katlar:     hidden emoji.
    - reveal_all=True → oyun bittiğinde geçilemeyen katların bombası da gösterilir.
    """
    mults  = TowersGame.MULTIPLIERS[state.tower_mode]
    cols   = TowersGame.COLS[state.tower_mode]
    hidden = settings["hidden"]
    gem    = settings["gem"]
    bomb   = settings["bomb"]

    lines = []
    for floor_idx in range(TowersGame.FLOORS - 1, -1, -1):   # kat 10 → 1
        mult      = mults[floor_idx]
        floor_num = floor_idx + 1

        if floor_idx < state.current_floor:
            # Geçilen kat — açık göster (bomb + gem, boş hücre = boşluk)
            bomb_col   = state.bomb_positions[floor_idx]
            picked_col = state.picked.get(floor_idx)
            cells_list = []
            for c in range(cols):
                if c == bomb_col:
                    cells_list.append(bomb)
                else:
                    cells_list.append(gem)
            cells  = " ".join(cells_list)
            marker = "✅"

        elif floor_idx == state.current_floor:
            # Aktif kat
            if reveal_all:
                # Oyun bitti — aktif katın bomba/gem konumunu göster
                bomb_col   = state.bomb_positions[floor_idx]
                picked_col = state.picked.get(floor_idx)
                cells_list = []
                for c in range(cols):
                    if c == bomb_col:
                        cells_list.append(bomb)
                    else:
                        cells_list.append(gem)
                cells  = " ".join(cells_list)
                marker = "💥"
            else:
                cells  = " ".join([hidden] * cols)
                marker = "▶️"

        else:
            # Henüz ulaşılmamış kat
            if reveal_all:
                bomb_col   = state.bomb_positions[floor_idx]
                cells_list = [bomb if c == bomb_col else gem for c in range(cols)]
                cells  = " ".join(cells_list)
            else:
                cells  = " ".join([hidden] * cols)
            marker = "🔒"

        lines.append(f"`{marker}` **{floor_num:>2}** │ {cells} │ `{mult:.2f}x`")

    return "\n".join(lines)


# ── Towers embed builders ─────────────────────────────────────────────────────

def _towers_playing_embed(user: discord.Member, state: "TowersState") -> discord.Embed:
    settings    = _get_towers_settings()
    mults       = TowersGame.MULTIPLIERS[state.tower_mode]
    user_lang_id = str(user.id)

    mode_display = (
        t("games.mode_demo", user_id=user_lang_id)
        if state.mode == "demo"
        else t("games.mode_real", user_id=user_lang_id)
    )

    board = _towers_board_str(state, settings)

    # Earnings & multiplier info
    if state.current_floor > 0:
        current_mult   = mults[state.current_floor - 1]
        next_mult      = mults[state.current_floor] if state.current_floor < TowersGame.FLOORS else current_mult
        earnings       = int(state.bet * current_mult)
        next_earnings  = int(state.bet * next_mult)
        stats_line = (
            f"📊 **{t('games.towers.multiplier', user_id=user_lang_id)}:** "
            f"`{current_mult:.2f}x` ➜ `{next_mult:.2f}x`\n"
            f"💵 **{t('games.towers.earnings', user_id=user_lang_id)}:** "
            f"{format_balance(earnings, state.mode)}\n"
            f"⏭️ **{t('games.towers.next_floor', user_id=user_lang_id)}:** "
            f"{format_balance(next_earnings, state.mode)}"
        )
    else:
        first_mult = mults[0]
        stats_line = (
            f"📊 **{t('games.towers.pick_start', user_id=user_lang_id)}**\n"
            f"⏭️ **{t('games.towers.first_floor', user_id=user_lang_id)}:** `{first_mult:.2f}x`"
        )

    desc = (
        f"{board}\n\n"
        f"💰 **{t('games.bet', user_id=user_lang_id)}:** {format_balance(state.bet, state.mode)} │ "
        f"🎮 **{t('games.mode', user_id=user_lang_id)}:** {mode_display}\n\n"
        f"{stats_line}\n\n"
        f"🔐 **{t('games.game_id', user_id=user_lang_id)}:** `{state.game_uid}`"
    )

    color = 0x2ecc71 if state.current_floor > 0 else 0x3498db
    mode_label = t(f"games.towers.modes.{state.tower_mode}", user_id=user_lang_id)
    embed = discord.Embed(
        title=f"{settings['game']}  T O W E R S  ─  {mode_label}",
        description=desc,
        color=color,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=t("games.footer_towers_pf", user_id=user_lang_id))
    return embed


def _towers_cashout_embed(user: discord.Member, state: "TowersState", mult: float, earnings: int) -> discord.Embed:
    settings     = _get_towers_settings()
    profit       = earnings - state.bet
    sign         = "+" if profit >= 0 else ""
    user_lang_id = str(user.id)
    board        = _towers_board_str(state, settings, reveal_all=True)

    embed = discord.Embed(
        title=t("games.towers.cashed_out_title", user_id=user_lang_id),
        description=(
            f"{board}\n\n"
            f"**🏢 {t('games.towers.floors_cleared', user_id=user_lang_id)}:** {state.current_floor} / {TowersGame.FLOORS}\n"
            f"**📊 {t('games.towers.final_multiplier', user_id=user_lang_id)}:** `{mult:.2f}x`\n"
            f"**💰 {t('games.bet', user_id=user_lang_id)}:** {format_balance(state.bet, state.mode)}\n"
            f"**🏆 {t('games.towers.winnings', user_id=user_lang_id)}:** {format_balance(earnings, state.mode)} "
            f"(__**{sign}{format_balance(profit, state.mode)}**__)\n\n"
            f"🔐 **{t('games.game_id', user_id=user_lang_id)}:** `{state.game_uid}`"
        ),
        color=0x2ecc71,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=t("games.footer_towers_pf", user_id=user_lang_id))
    return embed


def _towers_bomb_embed(user: discord.Member, state: "TowersState") -> discord.Embed:
    settings     = _get_towers_settings()
    user_lang_id = str(user.id)
    board        = _towers_board_str(state, settings, reveal_all=True)

    embed = discord.Embed(
        title=t("games.towers.boom_title", user_id=user_lang_id),
        description=(
            f"{board}\n\n"
            f"**💣 {t('games.towers.hit_bomb', user_id=user_lang_id)}**\n"
            f"**🏢 {t('games.towers.floors_reached', user_id=user_lang_id)}:** {state.current_floor}\n"
            f"**💀 {t('games.lost', user_id=user_lang_id)}:** {format_balance(state.bet, state.mode)}\n\n"
            f"🔐 **{t('games.game_id', user_id=user_lang_id)}:** `{state.game_uid}`"
        ),
        color=0xe74c3c,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=t("games.footer_towers_pf", user_id=user_lang_id))
    return embed


# ── Towers game-end helpers ───────────────────────────────────────────────────

async def _towers_restore_menu(interaction: discord.Interaction, state: "TowersState") -> None:
    """3 saniye bekle, ardından oyun menüsünü geri yükle."""
    await asyncio.sleep(3)
    session = GameSession.get_session(state.message_id)
    if session:
        GameSession.update_session(state.message_id, in_game=False)
        session = GameSession.get_session(state.message_id)
        layout = hub_active_layout(state.message_id, interaction.user, session, "towers")
        try:
            await interaction.message.edit(embed=None, content=None, view=layout)
        except Exception:
            pass
    _active_towers.pop(state.message_id, None)


async def _towers_do_cashout(interaction: discord.Interaction, state: "TowersState") -> None:
    """Cashout ödemesi yap, PF logla ve menüyü geri yükle."""
    mults    = TowersGame.MULTIPLIERS[state.tower_mode]
    mult     = mults[state.current_floor - 1]
    earnings = int(state.bet * mult)
    profit   = earnings - state.bet

    player      = Player(state.owner_id)
    result_str  = "win" if earnings > state.bet else "tie"
    game_result = GameResult(result=result_str, bet=state.bet, multiplier=mult, amount=earnings)
    member      = interaction.user if isinstance(interaction.user, discord.Member) else None
    last_game_info = TowersGame().handle_result(game_result, player, state.mode, member=member, is_free_round=state.is_free_round)

    if state.mode == "real" and member:
        from Games.base_game import _check_and_assign_tier_role
        await _check_and_assign_tier_role(member, player)
        levels_cog = interaction.client.cogs.get("LevelsCog")
        if levels_cog:
            await levels_cog.process_level_up(state.owner_id)

    # ── Event hooks ──────────────────────────────────────────────────────────
    from modules.event_manager import process_game_event
    from cogs.events import send_event_completion
    _ev_towers = process_game_event(
        state.owner_id,
        {"game": "towers", "won": True, "level_reached": state.current_floor,
         "bet": state.bet, "mode": state.mode},
        player,
    )

    embed = _towers_cashout_embed(interaction.user, state, mult, earnings)
    from modules.games_play_v2 import towers_floor_items

    await interaction.response.edit_message(
        embed=None,
        content=None,
        view=play_layout(
            embed, towers_floor_items(state, game_over=True, cashed_out=True)
        ),
    )

    from modules.provably_fair import log_game_end
    try:
        settings = _get_towers_settings()
        await log_game_end(
            state.pf_log_msg, "Towers", settings["game"], interaction.user,
            state.bet, state.mode, state.server_seed, state.client_seed,
            state.nonce, state.game_uid, result_str,
            {"floors": state.current_floor, "mode": state.tower_mode, "multiplier": mult},
            profit,
        )
    except Exception:
        pass

    GameSession.update_session(state.message_id, last_game=last_game_info)
    if _ev_towers:
        await send_event_completion(interaction, _ev_towers)
    await _towers_restore_menu(interaction, state)


async def _towers_do_bomb(interaction: discord.Interaction, state: "TowersState") -> None:
    """Kaybı kaydet, PF logla ve menüyü geri yükle."""
    player      = Player(state.owner_id)
    game_result = GameResult(result="lose", bet=state.bet, multiplier=0.0)
    member      = interaction.user if isinstance(interaction.user, discord.Member) else None
    last_game_info = TowersGame().handle_result(game_result, player, state.mode, member=member, is_free_round=state.is_free_round)

    if state.mode == "real" and member:
        levels_cog = interaction.client.cogs.get("LevelsCog")
        if levels_cog:
            await levels_cog.process_level_up(state.owner_id)

    # ── Event hooks (streak reset on bomb) ───────────────────────────────────
    from modules.event_manager import process_game_event
    process_game_event(
        state.owner_id,
        {"game": "towers", "won": False, "level_reached": 0,
         "bet": state.bet, "mode": state.mode},
        player,
    )

    embed = _towers_bomb_embed(interaction.user, state)
    from modules.games_play_v2 import towers_floor_items

    await interaction.response.edit_message(
        embed=None,
        content=None,
        view=play_layout(
            embed, towers_floor_items(state, game_over=True, cashed_out=False)
        ),
    )

    from modules.provably_fair import log_game_end
    try:
        settings = _get_towers_settings()
        await log_game_end(
            state.pf_log_msg, "Towers", settings["game"], interaction.user,
            state.bet, state.mode, state.server_seed, state.client_seed,
            state.nonce, state.game_uid, "lose",
            {"floors": state.current_floor, "mode": state.tower_mode},
            -state.bet,
        )
    except Exception:
        pass

    GameSession.update_session(state.message_id, last_game=last_game_info)
    await _towers_restore_menu(interaction, state)


# ── Towers Discord UI ─────────────────────────────────────────────────────────

class TowersColButton(discord.ui.Button):
    """Bir kattaki kolon seçim butonu."""

    def __init__(self, message_id: str, col: int):
        settings = _get_towers_settings()
        super().__init__(
            label=str(col + 1),
            style=discord.ButtonStyle.secondary,
            emoji=settings["hidden"],
            row=0,
            custom_id=f"tc_{col}_{message_id}",
        )
        self.col        = col
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        mid   = str(interaction.message.id)
        state = _active_towers.get(mid)
        if not state:
            return await interaction.response.send_message(
                t("games.errors.game_not_found", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        if interaction.user.id != state.owner_id:
            return await interaction.response.send_message(
                t("games.errors.not_your_game", user_id=str(interaction.user.id)),
                ephemeral=True,
            )

        GameSession.touch_session(mid)

        bomb_col = state.bomb_positions[state.current_floor]
        settings = _get_towers_settings()
        rigged_chance = settings["rigged_chance"] / 100.0

        if self.col == bomb_col:
            # Gerçek bomba — kaybet
            state.picked[state.current_floor] = self.col
            await _towers_do_bomb(interaction, state)
        elif random.random() < rigged_chance:
            # Gizli tuzak: tıklanan kolon bomba yapılır (%rigged_chance ihtimalle)
            state.bomb_positions[state.current_floor] = self.col
            state.picked[state.current_floor] = self.col
            await _towers_do_bomb(interaction, state)
        else:
            # Güvenli kolon — kat geç
            state.picked[state.current_floor] = self.col
            state.current_floor += 1

            if state.current_floor >= TowersGame.FLOORS:
                # Tüm katlar geçildi → otomatik cashout
                await _towers_do_cashout(interaction, state)
            else:
                from modules.games_play_v2 import towers_floor_items

                embed = _towers_playing_embed(interaction.user, state)
                await interaction.response.edit_message(
                    embed=None,
                    content=None,
                    view=play_layout(embed, towers_floor_items(state)),
                )


class TowersCashoutButton(discord.ui.Button):
    """Cashout butonu."""

    def __init__(self, message_id: str, mult: float, earnings: int, mode: str, disabled: bool):
        from modules.database import get_data as _gd
        _srv      = _gd("server/server") or {}
        coin_emoji = (
            _srv.get("demo_coin_emoji", "💎") if mode == "demo"
            else _srv.get("coin_emoji", "💵")
        )
        user_lang_id  = str((GameSession.get_session(message_id) or {}).get("owner", 0))
        cashout_label = t("games.towers.cashout", user_id=user_lang_id)
        label = f"{cashout_label}  {mult:.2f}x  ·  {earnings:,}" if not disabled else cashout_label
        super().__init__(
            label=label,
            emoji=coin_emoji,
            style=discord.ButtonStyle.primary if not disabled else discord.ButtonStyle.secondary,
            row=1,
            disabled=disabled,
            custom_id=f"tco_{message_id}",
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        mid   = str(interaction.message.id)
        state = _active_towers.get(mid)
        if not state:
            return await interaction.response.send_message(
                t("games.errors.game_not_found", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        if interaction.user.id != state.owner_id:
            return await interaction.response.send_message(
                t("games.errors.not_your_game", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        if state.current_floor == 0:
            return await interaction.response.defer()
        GameSession.touch_session(mid)
        await _towers_do_cashout(interaction, state)


class TowersFloorView(discord.ui.View):
    """Aktif kat için kolon seçim butonları + cashout."""

    def __init__(self, state: "TowersState", game_over: bool = False, cashed_out: bool = False):
        super().__init__(timeout=600)
        settings = _get_towers_settings()
        cols     = TowersGame.COLS[state.tower_mode]
        mults    = TowersGame.MULTIPLIERS[state.tower_mode]

        # Kolon seçim butonları (row 0)
        for c in range(cols):
            if game_over:
                if state.current_floor < TowersGame.FLOORS:
                    bomb_here = state.bomb_positions[state.current_floor]
                else:
                    bomb_here = -1   # all cleared (auto-cashout)

                if cashed_out:
                    btn_style = discord.ButtonStyle.success
                    btn_emoji = settings["gem"]
                else:
                    btn_style = discord.ButtonStyle.danger if c == bomb_here else discord.ButtonStyle.secondary
                    btn_emoji = settings["bomb"] if c == bomb_here else None

                btn = discord.ui.Button(
                    label=str(c + 1),
                    style=btn_style,
                    emoji=btn_emoji,
                    row=0,
                    disabled=True,
                    custom_id=f"tgo_{c}_{state.message_id}",
                )
            else:
                btn = TowersColButton(state.message_id, c)
            self.add_item(btn)

        # Cashout butonu (row 1)
        cashout_disabled = state.current_floor == 0 or game_over
        mult     = mults[state.current_floor - 1] if state.current_floor > 0 else 1.0
        earnings = int(state.bet * mult)
        self.add_item(TowersCashoutButton(state.message_id, mult, earnings, state.mode, cashout_disabled))


class TowersModeSelect(discord.ui.Select):
    """Zorluk modu seçim menüsü (Easy / Normal / Hard)."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session  = GameSession.get_session(message_id)
        current  = session.get("towers_mode", "easy") if session else "easy"
        owner_id = str(session.get("owner", 0)) if session else "0"

        options = []
        for mode_key in ("easy", "normal", "hard"):
            mults    = TowersGame.MULTIPLIERS[mode_key]
            cols     = TowersGame.COLS[mode_key]
            max_mult = mults[-1]
            options.append(discord.SelectOption(
                label=t(f"games.towers.modes.{mode_key}", user_id=owner_id),
                description=t(
                    "games.towers.mode_desc",
                    user_id=owner_id,
                    cols=cols,
                    max_mult=f"{max_mult:.2f}",
                ),
                value=mode_key,
                default=(mode_key == current),
            ))

        super().__init__(
            placeholder=t("games.towers.choose_mode", user_id=owner_id),
            options=options,
            min_values=1,
            max_values=1,
            custom_id=f"towers_mode:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        tower_mode = self.values[0]
        GameSession.update_session(self.message_id, towers_mode=tower_mode)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "towers")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class StartTowersButton(discord.ui.Button):
    """Towers oyununu başlatan buton."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session  = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        super().__init__(
            label=t("games.start_game", user_id=owner_id),
            style=discord.ButtonStyle.success,
            row=3,
            custom_id=f"towers_start:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)

        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=user_lang_id),
                ephemeral=True,
            )

        if not GameSession.is_session_active(self.message_id):
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.session_expired_title", user_id=user_lang_id),
                    description=t("games.errors.session_expired_desc", user_id=user_lang_id),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )

        session = GameSession.get_session(self.message_id)
        if not session:
            return await interaction.response.send_message(
                t("games.errors.session_not_found", user_id=user_lang_id),
                ephemeral=True,
            )

        bet         = int(session.get("bet", 100))
        mode        = session.get("mode", "demo")
        tower_mode  = session.get("towers_mode", "easy")

        player      = Player(interaction.user.id)
        towers_game = TowersGame()

        # Bakiye kontrolü (free-round promo varsa atlanır)
        if not towers_game.can_afford_bet(player, mode, bet):
            balance = player.get_balance(mode)
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                    description=t(
                        "games.errors.insufficient_balance_desc",
                        user_id=user_lang_id,
                        need=format_balance(bet, mode),
                        have=format_balance(balance, mode),
                    ),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )

        import secrets as _secrets
        from modules.provably_fair import hash_seed, log_game_start, new_game_uid
        server_seed     = _secrets.token_hex(32)
        client_seed     = _secrets.token_hex(16)
        nonce           = random.randint(1, 99999)
        game_uid        = new_game_uid()
        bomb_positions  = TowersGame.generate_floors(server_seed, client_seed, nonce, tower_mode)
        settings        = _get_towers_settings()

        # Bahisi düş (free-round varsa bakiye kesilmez, bet promo'dan alınır)
        is_free_round, bet = towers_game.deduct_bet(player, mode, bet)

        mid   = str(interaction.message.id)
        state = TowersState(
            owner_id=interaction.user.id,
            tower_mode=tower_mode,
            bet=bet,
            mode=mode,
            bomb_positions=bomb_positions,
            server_seed=server_seed,
            client_seed=client_seed,
            nonce=nonce,
            game_uid=game_uid,
            message_id=mid,
            is_free_round=is_free_round,
        )
        _active_towers[mid] = state

        log_msg = await log_game_start(
            interaction, "Towers", settings["game"], interaction.user,
            bet, mode, hash_seed(server_seed), client_seed, nonce, game_uid,
        )
        state.pf_log_msg = log_msg

        GameSession.update_session(mid, in_game=True)
        GameSession.touch_session(mid)

        embed = _towers_playing_embed(interaction.user, state)
        from modules.games_play_v2 import towers_floor_items

        await interaction.response.edit_message(
            embed=None,
            content=None,
            view=play_layout(embed, towers_floor_items(state)),
        )




# ════════════════════════════════════════════════════════════════════════════
# LIMBO
# ════════════════════════════════════════════════════════════════════════════

_LIMBO_PRESETS = [1.5, 2, 2.5, 3, 4, 5, 7, 10, 15, 20, 30, 50, 100, 250, 500, 1000]


class LimboCustomMultiplierModal(discord.ui.Modal):
    """Özel çarpan girmek için modal."""

    def __init__(self, message_id: str, user_id: str):
        super().__init__(title=t("games.limbo.custom_modal_title", user_id=user_id))
        self.message_id = message_id
        self.user_id = user_id
        self.mult_input = discord.ui.TextInput(
            label=t("games.limbo.custom_modal_label", user_id=user_id),
            placeholder=t("games.limbo.custom_modal_placeholder", user_id=user_id),
            min_length=1,
            max_length=7,
            required=True,
        )
        self.add_item(self.mult_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)
        try:
            value = float(self.mult_input.value.replace(",", "."))
        except ValueError:
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.invalid_input_title", user_id=user_lang_id),
                    description=t("games.errors.invalid_number", user_id=user_lang_id),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )

        if value < 1.01:
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.invalid_input_title", user_id=user_lang_id),
                    description=t("games.limbo.min_multiplier_desc", user_id=user_lang_id),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
        if value > 1000:
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.invalid_input_title", user_id=user_lang_id),
                    description=t("games.limbo.max_multiplier_desc", user_id=user_lang_id),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )

        value = round(value, 2)
        GameSession.update_session(self.message_id, limbo_multiplier=value)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "limbo")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class LimboMultiplierSelect(discord.ui.Select):
    """Preset çarpan seçim menüsü."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session  = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        current  = session.get("limbo_multiplier", 2.0) if session else 2.0

        options = [
            discord.SelectOption(
                label=t("games.limbo.custom_button", user_id=owner_id),
                description=t("games.limbo.custom_option_desc", user_id=owner_id),
                value="custom",
                emoji="✏️",
            )
        ]
        for mult in _LIMBO_PRESETS:
            chance = LimboGame.win_chance(mult) * 100
            options.append(discord.SelectOption(
                label=f"{mult}x",
                description=t(
                    "games.limbo.select_desc",
                    user_id=owner_id,
                    chance=f"{chance:.2f}",
                ),
                value=str(mult),
                emoji="🚀",
                default=(mult == current),
            ))

        super().__init__(
            placeholder=t("games.limbo.select_placeholder", user_id=owner_id),
            options=options,
            min_values=1,
            max_values=1,
            custom_id=f"limbo_mult:{message_id}",
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        selected = self.values[0]
        if selected == "custom":
            modal = LimboCustomMultiplierModal(self.message_id, str(interaction.user.id))
            return await interaction.response.send_modal(modal)
        mult = float(selected)
        GameSession.update_session(self.message_id, limbo_multiplier=mult)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "limbo")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class StartLimboButton(discord.ui.Button):
    """Limbo oyununu başlatan buton."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session  = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        super().__init__(
            label=t("games.start_game", user_id=owner_id),
            style=discord.ButtonStyle.success,
            row=4,
            custom_id=f"limbo_start:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)

        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=user_lang_id),
                ephemeral=True,
            )

        if not GameSession.is_session_active(self.message_id):
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.session_expired_title", user_id=user_lang_id),
                    description=t("games.errors.session_expired_desc", user_id=user_lang_id),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )

        session = GameSession.get_session(self.message_id)
        if not session:
            return await interaction.response.send_message(
                t("games.errors.session_not_found", user_id=user_lang_id),
                ephemeral=True,
            )

        bet    = int(session.get("bet", 100))
        mode   = session.get("mode", "demo")
        target = float(session.get("limbo_multiplier", 2.0))

        player     = Player(interaction.user.id)
        limbo_game = LimboGame()

        # Bakiye kontrolü (free-round promo varsa atlanır)
        if not limbo_game.can_afford_bet(player, mode, bet):
            balance = player.get_balance(mode)
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                    description=t(
                        "games.errors.insufficient_balance_desc",
                        user_id=user_lang_id,
                        need=format_balance(bet, mode),
                        have=format_balance(balance, mode),
                    ),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )

        from modules.provably_fair import consume_pf_round, hash_seed, log_game_start, log_game_end, new_game_uid

        server_seed, client_seed, nonce, pf_fl = consume_pf_round(interaction.user.id)
        game_uid = new_game_uid()

        log_msg = await log_game_start(
            interaction, "Limbo", "🚀", interaction.user,
            bet, mode, hash_seed(server_seed), client_seed, nonce, game_uid,
        )

        # Bahisi düş (free-round varsa bakiye kesilmez, bet promo'dan alınır)
        is_free_round, bet = limbo_game.deduct_bet(player, mode, bet)
        GameSession.update_session(self.message_id, in_game=True)
        GameSession.touch_session(self.message_id)

        # Animasyon
        anim_embed = discord.Embed(
            title="🚀 L I M B O",
            description=t("games.limbo.rolling", user_id=user_lang_id),
            color=discord.Color.orange(),
        )
        anim_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        from modules.games_play_v2 import status_button

        await interaction.response.edit_message(
            embed=None,
            content=None,
            view=play_layout(
                anim_embed,
                [status_button("Rolling...", emoji="🚀")],
                timeout=None,
            ),
        )
        await asyncio.sleep(0.8)

        game_result = LimboGame().play_round(bet, target, floats=pf_fl)

        # ── Rigged chance: force a losing result ─────────────────────────────
        limbo_settings = _get_limbo_settings()
        if limbo_settings["rigged_chance"] > 0 and random.random() * 100 < limbo_settings["rigged_chance"]:
            # Pick a result value strictly below the target so the player loses.
            # Range: [1.00, target - 0.01] — uniform random so it looks natural.
            forced_max = max(1.00, round(target - 0.01, 2))
            forced_val = round(random.uniform(1.00, forced_max), 2)
            game_result = GameResult(
                result="lose",
                bet=bet,
                multiplier=0.0,
                meta={"result_value": forced_val, "target_multiplier": target},
                amount=0,
            )

        result_value = game_result.meta["result_value"]
        profit = game_result.amount - bet

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        last_game_info = limbo_game.handle_result(game_result, player, mode, member=member, is_free_round=is_free_round)

        if mode == "real" and member:
            from Games.base_game import _check_and_assign_tier_role
            await _check_and_assign_tier_role(member, player)
            levels_cog = interaction.client.cogs.get("LevelsCog")
            if levels_cog:
                await levels_cog.process_level_up(interaction.user.id)

        # ── Event hooks ──────────────────────────────────────────────────────
        from modules.event_manager import process_game_event
        from cogs.events import send_event_completion
        _ev_limbo = process_game_event(
            interaction.user.id,
            {"game": "limbo", "won": game_result.result == "win",
             "multiplier_hit": result_value, "bet": bet, "mode": mode},
            player,
        )

        mode_display = (
            t("games.mode_demo", user_id=user_lang_id)
            if mode == "demo"
            else t("games.mode_real", user_id=user_lang_id)
        )

        if game_result.result == "win":
            color = discord.Color.green()
            sign  = "+"
            result_line = (
                f"🏆 **{t('games.won', user_id=user_lang_id)}:** {format_balance(game_result.amount, mode)} "
                f"(__**+{format_balance(profit, mode)}**__)"
            )
        else:
            color = discord.Color.red()
            sign  = "-"
            result_line = f"💀 **{t('games.lost', user_id=user_lang_id)}:** {format_balance(bet, mode)}"

        desc = (
            f"🎯 **{t('games.limbo.target', user_id=user_lang_id)}:** `{target:.2f}x`\n"
            f"🎲 **{t('games.limbo.result_value', user_id=user_lang_id)}:** `{result_value:.2f}x`\n\n"
            f"{result_line}\n\n"
            f"💰 **{t('games.bet', user_id=user_lang_id)}:** {format_balance(bet, mode)}\n"
            f"🎮 **{t('games.mode', user_id=user_lang_id)}:** {mode_display}\n"
            f"💵 **{t('games.your_balance', user_id=user_lang_id)}:** {format_balance(player.get_balance(mode), mode)}\n\n"
            f"🔐 **{t('games.game_id', user_id=user_lang_id)}:** `{game_uid}`"
        )
        title_emoji = "🎉" if game_result.result == "win" else "😢"
        result_embed = discord.Embed(
            title=f"{title_emoji} L I M B O  —  {result_value:.2f}x",
            description=desc,
            color=color,
        )
        result_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        result_embed.set_footer(text=t("games.footer_limbo_pf", user_id=user_lang_id))

        try:
            await interaction.message.edit(
                embed=None,
                content=None,
                view=play_layout(result_embed, [], timeout=None),
            )
        except Exception:
            pass

        from modules.provably_fair import log_game_end
        try:
            await log_game_end(
                log_msg, "Limbo", "🚀", interaction.user,
                bet, mode, server_seed, client_seed, nonce, game_uid,
                game_result.result,
                {"result_value": result_value, "target_multiplier": target},
                profit,
            )
        except Exception:
            pass

        GameSession.update_session(self.message_id, last_game=last_game_info, in_game=False)
        if _ev_limbo:
            await send_event_completion(interaction, _ev_limbo)

        await asyncio.sleep(3)
        session_now = GameSession.get_session(self.message_id)
        if session_now:
            layout = hub_active_layout(self.message_id, interaction.user, session_now, "limbo")
            try:
                await interaction.message.edit(embed=None, content=None, view=layout)
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════════════════
# SLOT MACHINE
# ════════════════════════════════════════════════════════════════════════════

_SLOT_LINE_OPTIONS = [
    {"lines": 10, "emoji": "🔟", "rtp_note": "Concentrated · higher line bet"},
    {"lines": 20, "emoji": "🎯", "rtp_note": "Balanced coverage"},
    {"lines": 30, "emoji": "🎰", "rtp_note": "Full coverage (all 30 lines)"},
]


class SlotLineModeSelect(discord.ui.Select):
    """Choose how many paylines to activate (10 / 20 / 30)."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session  = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        current  = int(session.get("slot_paylines", 30)) if session else 30
        bet      = int(session.get("bet", 100)) if session else 100

        # Get configured game emoji for the select icon
        from Games.slot import get_slot_emojis as _gse
        _emap, _ = _gse()
        _slot_db  = (get_data("server/games") or {}).get("slot", {})
        _game_e   = str(_slot_db.get("emoji") or "🎰")
        # Only use as select emoji if it's a simple unicode emoji (custom Discord emojis can't be used in select options)
        _is_unicode = not _game_e.startswith("<")
        _select_emoji = _game_e if _is_unicode else "🎰"

        options = []
        for opt in _SLOT_LINE_OPTIONS:
            lines    = opt["lines"]
            line_bet = bet / lines
            options.append(discord.SelectOption(
                label=t("games.slot.lines_label", user_id=owner_id, lines=lines),
                description=t(
                    "games.slot.lines_desc",
                    user_id=owner_id,
                    line_bet=f"{line_bet:,.0f}",
                    note=opt["rtp_note"],
                ),
                value=str(lines),
                emoji=opt["emoji"],
                default=(lines == current),
            ))

        super().__init__(
            placeholder=t("games.slot.lines_placeholder", user_id=owner_id),
            options=options,
            min_values=1,
            max_values=1,
            custom_id=f"slot_lines:{message_id}",
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        lines = int(self.values[0])
        GameSession.update_session(self.message_id, slot_paylines=lines)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "slot")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class StartSlotButton(discord.ui.Button):
    """Slot Machine spin button — validates balance and delegates to SlotGame.play()."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session  = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        # Use configured game emoji (only unicode, custom emojis can't be button emoji by string)
        _slot_db = (get_data("server/games") or {}).get("slot", {})
        _game_e  = str(_slot_db.get("emoji") or "🎰")
        _btn_emoji = _game_e if not _game_e.startswith("<") else "🎰"
        super().__init__(
            label=t("games.slot.spin_button", user_id=owner_id),
            style=discord.ButtonStyle.success,
            emoji=_btn_emoji,
            row=3,
            custom_id=f"slot_start:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)

        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=user_lang_id),
                ephemeral=True,
            )

        if not GameSession.is_session_active(self.message_id):
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.session_expired_title", user_id=user_lang_id),
                    description=t("games.errors.session_expired_desc", user_id=user_lang_id),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )

        session = GameSession.get_session(self.message_id)
        if not session:
            return await interaction.response.send_message(
                t("games.errors.session_not_found", user_id=user_lang_id),
                ephemeral=True,
            )

        bet       = int(session.get("bet", 100))
        mode      = session.get("mode", "demo")
        num_lines = int(session.get("slot_paylines", 30))

        player = Player(interaction.user.id)
        slot_game = SlotGame()

        # Bakiye kontrolü (free-round promo varsa atlanır)
        if not slot_game.can_afford_bet(player, mode, bet):
            balance = player.get_balance(mode)
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                    description=t(
                        "games.errors.insufficient_balance_desc",
                        user_id=user_lang_id,
                        need=format_balance(bet, mode),
                        have=format_balance(balance, mode),
                    ),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )

        GameSession.update_session(self.message_id, in_game=True)
        GameSession.touch_session(self.message_id)
        await interaction.response.defer()
        await slot_game.play(interaction, self.message_id, player, bet, mode, num_lines)


class BetModal(Modal):
    """Özel bahis miktarı modal"""
    
    def __init__(self, message_id: str, user_id: str):
        super().__init__(title=t("games.bet_modal.title", user_id=user_id))
        self.message_id = message_id
        self.user_id = user_id
        self.bet_input = TextInput(
            label=t("games.bet_modal.amount_label", user_id=user_id),
            placeholder=t("games.bet_modal.amount_placeholder", user_id=user_id),
            min_length=1,
            max_length=10,
            required=True,
        )
        self.add_item(self.bet_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        """Modal gönderildiğinde"""
        try:
            bet_amount = int(self.bet_input.value)
            server_data = get_server_data()
            min_bet = int(server_data.get("minBet", 20))
            max_bet = int(server_data.get("maxBet", 50000))
            # Minimum ve maksimum kontrol
            if bet_amount < min_bet:
                embed = discord.Embed(
                    title=t("games.errors.minimum_bet_title", user_id=self.user_id),
                    description=t(
                        "games.errors.minimum_bet_desc",
                        user_id=self.user_id,
                        amount=format_balance(min_bet),
                    ),
                    color=discord.Color.red()
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            if bet_amount > max_bet:
                embed = discord.Embed(
                    title=t("games.errors.maximum_bet_title", user_id=self.user_id),
                    description=t(
                        "games.errors.maximum_bet_desc",
                        user_id=self.user_id,
                        amount=format_balance(max_bet),
                    ),
                    color=discord.Color.red()
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # Session'ı güncelle
            GameSession.update_session(self.message_id, bet=bet_amount)
            
            # Kullanıcı tercihlerini kalıcı olarak kaydet
            session = GameSession.get_session(self.message_id)
            if session:
                GameSession.save_user_preferences(session["owner"], bet=bet_amount)
            
            # Embed'i güncelle
            session = GameSession.get_session(self.message_id)
            if session and session.get("game"):
                game = session.get("game", "none")
                layout = hub_active_layout(self.message_id, interaction.user, session, game)
                await interaction.response.edit_message(embed=None, content=None, view=layout)
            else:
                await interaction.response.send_message(
                    t("games.messages.bet_updated", user_id=self.user_id),
                    ephemeral=True,
                )
                
        except ValueError:
            embed = discord.Embed(
                title=t("games.errors.invalid_input_title", user_id=self.user_id),
                description=t("games.errors.invalid_number", user_id=self.user_id),
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


class BetSelect(Select):
    """Bahis seçim menüsü"""
    
    def __init__(self, message_id: str):
        self.message_id = message_id
        session = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"

        server_data = get_server_data()
        min_bet = int(server_data.get("minBet", 20))
        max_bet = int(server_data.get("maxBet", 50000))

        # Generate evenly-spaced presets rounded to "nice" round numbers (multiples of 5/50/500…)
        import math as _math

        def _nice(n):
            if n <= 0:
                return 0
            if n < 10:
                return int(round(n))
            exp = _math.floor(_math.log10(n))
            step = 5 * (10 ** (exp - 1))
            return int(round(n / step) * step)

        steps = 14  # 15 candidate values = 14 intervals
        presets = []
        seen = set()
        for i in range(15):
            raw = min_bet + (max_bet - min_bet) * i / steps
            amount = _nice(raw)
            if amount not in seen:
                seen.add(amount)
                presets.append(amount)

        options = [
            discord.SelectOption(label=t("games.select.change_mode", user_id=owner_id), value="change_mode"),
            discord.SelectOption(label=t("games.select.custom_bet", user_id=owner_id), value="custom_bet"),
        ]
        for amount in presets:
            options.append(discord.SelectOption(
                label=f"{amount:,}",
                value=f"bet_{amount}",
                emoji=server_data.get("coin_emoji", "💵")
            ))

        super().__init__(
            placeholder=t("games.select.bet_placeholder", user_id=owner_id),
            options=options,
            custom_id=f"bet_select:{message_id}"
        )
    
    async def callback(self, interaction: discord.Interaction):
        """Bahis seçimi callback"""
        user_lang_id = str(interaction.user.id)
        # Owner kontrolü
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            embed = discord.Embed(
                title=t("games.errors.access_denied_title", user_id=user_lang_id),
                description=t("games.errors.not_owner", user_id=user_lang_id),
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Session timeout kontrolü
        if not GameSession.is_session_active(self.message_id):
            embed = discord.Embed(
                title=t("games.errors.session_timeout_title", user_id=user_lang_id),
                description=t("games.errors.session_timeout_desc", user_id=user_lang_id),
                color=discord.Color.orange()
            )
            GameSession.delete_session(self.message_id)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Aktiviteyi güncelle
        GameSession.touch_session(self.message_id)
        
        selected = self.values[0]
        session = GameSession.get_session(self.message_id)
        
        if not session:
            embed = discord.Embed(
                title=t("games.errors.session_expired_title", user_id=user_lang_id),
                description=t("games.errors.session_not_active", user_id=user_lang_id),
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        if selected == "change_mode":
            # Mod değiştir
            current_mode = session.get("mode", "demo")
            new_mode = "real" if current_mode == "demo" else "demo"
            GameSession.update_session(self.message_id, mode=new_mode)
            
            # Kullanıcı tercihlerini kalıcı olarak kaydet
            GameSession.save_user_preferences(session["owner"], mode=new_mode)
            
            session = GameSession.get_session(self.message_id)
            if session.get("game"):
                game = session.get("game", "none")
                layout = hub_active_layout(self.message_id, interaction.user, session, game)
                await interaction.response.edit_message(embed=None, content=None, view=layout)
            else:
                await interaction.response.send_message(
                    t("games.messages.mode_changed", user_id=user_lang_id, mode=new_mode.upper()),
                    ephemeral=True,
                )
        
        elif selected == "custom_bet":
            # Custom bet modal aç
            modal = BetModal(self.message_id, user_lang_id)
            await interaction.response.send_modal(modal)
        
        elif selected.startswith("bet_"):
            # Bahis miktarını ayarla
            bet_amount = int(selected.split("_")[1])
            if session.get("game") == "case_battle":
                from modules.case_battle import get_allowed_battle_cases

                cid = session.get("case_battle_case_id")
                if cid:
                    case = get_allowed_battle_cases().get(cid)
                    if case:
                        bet_amount = int(case.get("price", 0))
            GameSession.update_session(self.message_id, bet=bet_amount)
            # Kullanıcı tercihlerini kalıcı olarak kaydet
            GameSession.save_user_preferences(session["owner"], bet=bet_amount)
            session = GameSession.get_session(self.message_id)
            if session.get("game"):
                game = session.get("game", "none")
                layout = hub_active_layout(self.message_id, interaction.user, session, game)
                await interaction.response.edit_message(embed=None, content=None, view=layout)
            else:
                await interaction.response.send_message(
                    t("games.messages.bet_set", user_id=user_lang_id, amount=f"${bet_amount:,}"),
                    ephemeral=True,
                )


class GameSelectDropdown(Select):
    """Oyun seçim menüsü"""
    
    def __init__(self, message_id: str):
        self.message_id = message_id
        session = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        from modules.database import ge, get_data as _gd
        mines_settings = _get_mines_settings()

        # Load enabled/disabled state for each game
        _games_cfg = _gd("server/games") or {}

        def _enabled(game_id: str) -> bool:
            return _games_cfg.get(game_id, {}).get("enabled", True)

        all_game_options = [
            ("roulette", discord.SelectOption(
                label="Roulette",
                description=t("games.game_descriptions.roulette", user_id=owner_id),
                emoji=ge("roulette") or "🎰",
                value="roulette"
            )),
            ("dice", discord.SelectOption(
                label="Dice",
                description=t("games.game_descriptions.dice", user_id=owner_id),
                emoji=ge("dice") or "🎲",
                value="dice"
            )),
            ("coinflip", discord.SelectOption(
                label="Coin Flip",
                description=t("games.game_descriptions.coinflip", user_id=owner_id),
                emoji=ge("coinflip") or "🪙",
                value="coinflip"
            )),
            ("mines", discord.SelectOption(
                label="Mines",
                description=t("games.game_descriptions.mines", user_id=owner_id),
                emoji=mines_settings["game"],
                value="mines"
            )),
            ("crystals", discord.SelectOption(
                label="Crystals",
                description=t("games.game_descriptions.crystals", user_id=owner_id),
                emoji=_get_crystals_settings()["game"],
                value="crystals"
            )),
            ("towers", discord.SelectOption(
                label="Towers",
                description=t("games.game_descriptions.towers", user_id=owner_id),
                emoji=_get_towers_settings()["game"],
                value="towers"
            )),
            ("limbo", discord.SelectOption(
                label="Limbo",
                description=t("games.game_descriptions.limbo", user_id=owner_id),
                emoji="🚀",
                value="limbo"
            )),
            ("case_opening", discord.SelectOption(
                label="Case Opening",
                description=t("games.game_descriptions.case_opening", user_id=owner_id),
                emoji="📦",
                value="case_opening"
            )),
            ("case_battle", discord.SelectOption(
                label="Case Battle",
                description=t("games.game_descriptions.case_battle", user_id=owner_id),
                emoji="⚔️",
                value="case_battle"
            )),
            ("slot", discord.SelectOption(
                label="Slot Machine",
                description=t("games.game_descriptions.slot", user_id=owner_id),
                emoji="🎰",
                value="slot"
            )),
            ("blackjack", discord.SelectOption(
                label="Blackjack",
                description=t("games.game_descriptions.blackjack", user_id=owner_id),
                emoji="🃏",
                value="blackjack"
            )),
            ("hilo", discord.SelectOption(
                label="HiLo",
                description=t("games.game_descriptions.hilo", user_id=owner_id),
                emoji="🎴",
                value="hilo"
            )),
        ]

        options = [opt for game_id, opt in all_game_options if _enabled(game_id)]
        options.append(discord.SelectOption(
            label=t("games.select.back_to_menu", user_id=owner_id),
            emoji="◀️",
            value="back"
        ))
        
        super().__init__(
            placeholder=t("games.select.game_placeholder", user_id=owner_id),
            options=options,
            custom_id=f"game_select:{message_id}"
        )
    
    async def callback(self, interaction: discord.Interaction):
        """Oyun seçimi callback"""
        user_lang_id = str(interaction.user.id)
        # Owner kontrolü
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            embed = discord.Embed(
                title=t("games.errors.access_denied_title", user_id=user_lang_id),
                description=t("games.errors.not_owner", user_id=user_lang_id),
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Session timeout kontrolü
        if not GameSession.is_session_active(self.message_id):
            embed = discord.Embed(
                title=t("games.errors.session_timeout_title", user_id=user_lang_id),
                description=t("games.errors.session_timeout_desc", user_id=user_lang_id),
                color=discord.Color.orange()
            )
            GameSession.delete_session(self.message_id)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Aktiviteyi güncelle
        GameSession.touch_session(self.message_id)
        
        selected = self.values[0]
        
        if selected == "back":
            from modules.utils import get_user_lang

            lang = get_user_lang(interaction.user.id)
            layout = hub_menu_layout(self.message_id, interaction.user, lang)
            await interaction.response.edit_message(embed=None, content=None, view=layout)
        else:
            extra = {}
            if selected == "case_battle":
                extra["case_battle_opponent"] = "bot"
            GameSession.update_session(self.message_id, game=selected, **extra)
            session = GameSession.get_session(self.message_id)
            layout = hub_active_layout(self.message_id, interaction.user, session, selected)
            await interaction.response.edit_message(embed=None, content=None, view=layout)


class PlayButton(Button):
    """Oyun oyna butonu"""
    
    def __init__(self, message_id: str, game: str):
        self.message_id = message_id
        self.game = game
        session = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        
        super().__init__(
            label=t("games.play", user_id=owner_id),
            emoji="🎯",
            style=discord.ButtonStyle.success,
            custom_id=f"play_button:{message_id}"
        )
    
    async def callback(self, interaction: discord.Interaction):
        """Oyun oyna"""
        user_lang_id = str(interaction.user.id)
        # Owner kontrolü
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            embed = discord.Embed(
                title=t("games.errors.access_denied_title", user_id=user_lang_id),
                description=t("games.errors.not_owner", user_id=user_lang_id),
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        session = GameSession.get_session(self.message_id)
        if not session:
            return
        
        # Session timeout kontrolü
        if not GameSession.is_session_active(self.message_id):
            embed = discord.Embed(
                title=t("games.errors.session_timeout_title", user_id=user_lang_id),
                description=t("games.errors.session_timeout_desc", user_id=user_lang_id),
                color=discord.Color.orange()
            )
            GameSession.delete_session(self.message_id)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Oyunu başlatıyoruz - in_game true yap (allow concurrent plays)
        GameSession.update_session(self.message_id, in_game=True)
        GameSession.touch_session(self.message_id)
        
        player = Player(interaction.user.id)
        bet = int(session["bet"])  # Ensure bet is int
        mode = session["mode"]

        # Oyun instance'larını önceden oluştur (can_afford_bet için gerekli)
        games = {
            "roulette": RouletteGame(),
            "dice": DiceGame(),
            "coinflip": CoinFlipGame(),
            "mines": MinesGame(),
            "slot": SlotGame(),
        }
        game_instance = games.get(self.game)

        # Bakiye kontrolü (free-round promo varsa atlanır)
        if game_instance and not game_instance.can_afford_bet(player, mode, bet):
            embed = discord.Embed(
                title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                description=t(
                    "games.errors.insufficient_balance_desc",
                    user_id=user_lang_id,
                    need=format_balance(bet, mode),
                    have=format_balance(balance if (balance := player.get_balance(mode)) else 0, mode),
                ),
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # Oyunu oyna
        await interaction.response.defer()

        if game_instance:
            await game_instance.play(interaction, self.message_id, player, bet, mode)


class CoinFlipChoiceButton(Button):
    """CoinFlip için HOT/COLD seçim butonu"""
    def __init__(self, message_id: str, choice: str):
        self.message_id = message_id
        self.choice = choice
        super().__init__(
            label=choice,
            emoji="🔥" if choice == "Hot" else "❄️",
            style=discord.ButtonStyle.danger if choice == "Hot" else discord.ButtonStyle.primary,
            custom_id=f"coinflip_choice:{message_id}:{choice}"
        )

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)
        # Owner kontrolü
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            embed = discord.Embed(
                title=t("games.errors.access_denied_title", user_id=user_lang_id),
                description=t("games.errors.not_owner", user_id=user_lang_id),
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        session = GameSession.get_session(self.message_id)
        if not session:
            return

        # Session timeout kontrolü
        if not GameSession.is_session_active(self.message_id):
            embed = discord.Embed(
                title=t("games.errors.session_timeout_title", user_id=user_lang_id),
                description=t("games.errors.session_timeout_desc", user_id=user_lang_id),
                color=discord.Color.orange()
            )
            GameSession.delete_session(self.message_id)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        GameSession.update_session(self.message_id, in_game=True)
        GameSession.touch_session(self.message_id)

        player = Player(interaction.user.id)
        bet = int(session["bet"])  # Ensure bet is int
        mode = session["mode"]

        game_instance = CoinFlipGame()

        # Bakiye kontrolü (free-round promo varsa atlanır)
        if not game_instance.can_afford_bet(player, mode, bet):
            balance = player.get_balance(mode)
            embed = discord.Embed(
                title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                description=t(
                    "games.errors.insufficient_balance_desc",
                    user_id=user_lang_id,
                    need=format_balance(bet, mode),
                    have=format_balance(balance, mode),
                ),
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # Oyunu oyna
        await interaction.response.defer()
        # Pass the player's choice to the coinflip game
        await game_instance.play(interaction, self.message_id, player, bet, mode, player_choice=self.choice)




# ════════════════════════════════════════════════════════════════════════════
# CASE OPENING — slot-reel animation helper
# ════════════════════════════════════════════════════════════════════════════

async def _run_case_reel_animation(
    message: discord.Message,
    pool: list,
    winners: list,
    batch_start: int,
    game_uid: str,
    case_emoji: str,
    case_name: str,
    batch_label: str,
) -> None:
    """Slot-reel button animation for 1–5 case results.

    Layout (count == 1):   col 0-4 are the reel; winner locked at col 2 (green).
    Layout (count > 1):    col 0 = case number label, cols 1-4 are reel; winner at col 2 (green).
    """
    from modules.games_play_v2 import case_reel_items, message_edit_play

    # ── Phase 1: Fast spin ────────────────────────────────────────────────
    for i in range(3):
        embed = discord.Embed(
            title=f"🎰  {case_emoji} {case_name}{batch_label}",
            description="🌀 **Spinning...** Items are shuffling!",
            color=0x5865f2,
        )
        embed.set_footer(text=f"Game ID: {game_uid}  •  Provably Fair")
        uid = f"f{i}{random.randint(0, 9999)}"
        await message_edit_play(
            message,
            embed,
            case_reel_items(pool, winners, batch_start, reveal=False, uid=uid),
            timeout=None,
        )
        await asyncio.sleep(0.35)

    # ── Phase 2: Reveal winner ────────────────────────────────────────────
    embed = discord.Embed(
        title=f"✨  {case_emoji} {case_name}{batch_label}",
        description="🎯 **Winner revealed!**",
        color=0x57f287,
    )
    embed.set_footer(text=f"Game ID: {game_uid}  •  Provably Fair")
    await message_edit_play(
        message,
        embed,
        case_reel_items(pool, winners, batch_start, reveal=True, uid=f"r{random.randint(0, 9999)}"),
        timeout=None,
    )
    await asyncio.sleep(0.8)


# ════════════════════════════════════════════════════════════════════════════
# CASE BATTLE
# ════════════════════════════════════════════════════════════════════════════

class CaseBattleCaseSelect(discord.ui.Select):
    """Case picker for battles (admin allow-list or all cases with items)."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        from modules.case_battle import get_allowed_battle_cases

        session = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        current = session.get("case_battle_case_id") if session else None
        cases = get_allowed_battle_cases()
        options = []
        for cid, case in sorted(cases.items(), key=lambda x: x[1].get("price", 0)):
            price = case.get("price", 0)
            label = f"{case.get('name', 'Case')[:80]}"
            desc = f"{format_balance(price, session.get('mode', 'demo') if session else 'demo')}"
            options.append(
                discord.SelectOption(
                    label=label,
                    description=desc[:100],
                    emoji=case.get("emoji", "📦") or "📦",
                    value=cid,
                    default=(cid == current),
                )
            )
        if not options:
            options = [
                discord.SelectOption(
                    label="No cases available",
                    value="_none",
                    description="Ask an admin to configure cases",
                )
            ]
        super().__init__(
            placeholder="⚔️ Select battle case…",
            options=options[:25],
            custom_id=f"case_battle_case:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=user_lang_id),
                ephemeral=True,
            )
        if self.values[0] == "_none":
            return await interaction.response.defer()
        from modules.case_battle import get_allowed_battle_cases

        cases = get_allowed_battle_cases()
        case = cases.get(self.values[0])
        if not case:
            return await interaction.response.send_message("❌ Invalid case.", ephemeral=True)
        GameSession.update_session(
            self.message_id,
            case_battle_case_id=self.values[0],
            bet=int(case.get("price", 0)),
        )
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "case_battle")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class CaseBattleOpponentSelect(discord.ui.Select):
    """Opponent selection — Bot only for now."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session = GameSession.get_session(message_id)
        current = (session.get("case_battle_opponent", "bot") if session else "bot")
        super().__init__(
            placeholder="👤 Select opponent…",
            options=[
                discord.SelectOption(
                    label="Bot",
                    description="Play vs house bot — private room only, no log",
                    emoji="🤖",
                    value="bot",
                    default=(current == "bot"),
                ),
            ],
            custom_id=f"case_battle_opp:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        GameSession.update_session(self.message_id, case_battle_opponent=self.values[0])
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "case_battle")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class StartCaseBattleButton(discord.ui.Button):
    def __init__(self, message_id: str):
        self.message_id = message_id
        super().__init__(
            label="Start Battle",
            emoji="⚔️",
            style=discord.ButtonStyle.success,
            custom_id=f"case_battle_start:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        await _do_case_battle(interaction, self.message_id)


async def _do_case_battle(interaction: discord.Interaction, message_id: str) -> None:
    """1v1 case battle in private room — bot battles skip PF/log channels."""
    user_lang_id = str(interaction.user.id)

    if not GameSession.is_session_active(message_id):
        return await interaction.response.send_message(
            embed=discord.Embed(
                title=t("games.errors.session_expired_title", user_id=user_lang_id),
                description=t("games.errors.session_expired_desc", user_id=user_lang_id),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )

    session = GameSession.get_session(message_id)
    if not session:
        return await interaction.response.send_message(
            t("games.errors.session_not_found", user_id=user_lang_id),
            ephemeral=True,
        )

    mode = session.get("mode", "demo")
    case_id = session.get("case_battle_case_id")
    opponent = session.get("case_battle_opponent", "bot")

    from modules.case_battle import get_allowed_battle_cases, log_case_battle

    cases = get_allowed_battle_cases()
    case = cases.get(case_id) if case_id else None
    if not case or not case.get("items"):
        return await interaction.response.send_message(
            embed=discord.Embed(
                title="❌ Invalid Case",
                description="Selected case not found or has no items.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )

    stake = int(case.get("price", 0))
    player = Player(interaction.user.id)
    balance = player.get_balance(mode)
    if balance < stake:
        return await interaction.response.send_message(
            embed=discord.Embed(
                title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                description=t(
                    "games.errors.insufficient_balance_desc",
                    user_id=user_lang_id,
                    need=format_balance(stake, mode),
                    have=format_balance(balance, mode),
                ),
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )

    GameSession.update_session(message_id, in_game=True)
    GameSession.touch_session(message_id)

    from modules.provably_fair import consume_pf_round, hash_seed, new_game_uid

    server_seed, client_seed, nonce, pf_floats = consume_pf_round(interaction.user.id)
    game_uid = new_game_uid()

    case_emoji = case.get("emoji", "📦")
    case_name = case.get("name", "Case")
    items = case.get("items", [])

    loading_embed = discord.Embed(
        title=f"⚔️  {case_emoji} {case_name}",
        description="🌀 **Battle starting…**",
        color=0x9b59b6,
    )
    loading_embed.set_footer(text=f"Game ID: {game_uid}  •  Provably Fair")
    from modules.games_play_v2 import case_battle_duel_items, status_button

    await interaction.response.edit_message(
        embed=None,
        content=None,
        view=play_layout(
            loading_embed,
            [status_button("Opening cases...", emoji="⚔️")],
            timeout=None,
        ),
    )

    player.remove_balance(mode, stake)
    player_item = _case_open_item_pf(items, pf_floats[0])
    bot_item = _case_open_item_pf(items, pf_floats[1 % len(pf_floats)])
    p_val = int(player_item.get("value", 0))
    b_val = int(bot_item.get("value", 0))

    for _ in range(3):
        spin_embed = discord.Embed(
            title=f"⚔️  {case_emoji} {case_name}",
            description="🌀 **Both sides opening…**",
            color=0x5865f2,
        )
        spin_embed.set_footer(text=f"Game ID: {game_uid}")
        try:
            await interaction.message.edit(
                embed=None,
                content=None,
                view=play_layout(
                    spin_embed,
                    case_battle_duel_items(player_item, bot_item, revealed=False),
                    timeout=None,
                ),
            )
        except Exception:
            pass
        await asyncio.sleep(0.4)

    if p_val > b_val:
        total_won = p_val + b_val
        result_label = "win"
        winner = "player"
    elif b_val > p_val:
        total_won = 0
        result_label = "lose"
        winner = "bot"
    else:
        total_won = stake
        result_label = "tie"
        winner = "tie"

    if total_won > 0:
        player.add_balance(mode, total_won)
    profit = total_won - stake

    is_tracking_exempt = _is_tracking_exempt_user(interaction.user.id)
    if not is_tracking_exempt:
        player.update_stats("Case Battle", stake, result_label, profit, mode)
        player.record_game_history({
            "game": "Case Battle",
            "case_name": case_name,
            "mode": mode,
            "cost": stake,
            "won": total_won,
            "profit": profit,
            "opponent": opponent,
            "player_item": player_item.get("name"),
            "bot_item": bot_item.get("name"),
        })

    if isinstance(interaction.user, discord.Member) and not is_tracking_exempt:
        from Games.base_game import _check_and_assign_tier_role

        await _check_and_assign_tier_role(interaction.user, player)
        levels_cog = interaction.client.cogs.get("LevelsCog")
        if levels_cog:
            await levels_cog.process_level_up(interaction.user.id)

    from modules.event_manager import process_game_event

    process_game_event(
        interaction.user.id,
        {"game": "case_battle", "bet": stake, "mode": mode},
        player,
    )

    profit_sign = "+" if profit >= 0 else "-"
    is_profit = profit >= 0
    color = 0x57f287 if is_profit else (0xfee75c if result_label == "tie" else 0xed4245)
    headers = {"win": "🎉 YOU WIN!", "lose": "😢 Bot wins", "tie": "🤝 Tie — stake refunded"}
    mode_display = (
        t("games.mode_demo", user_id=user_lang_id)
        if mode == "demo"
        else t("games.mode_real", user_id=user_lang_id)
    )
    from modules.constants import FOOTER_TEXT

    result_embed = discord.Embed(title=headers.get(result_label, "⚔️ Battle"), color=color)
    result_embed.add_field(
        name=f"{case_emoji}  {case_name}",
        value=(
            f"**You** ╸ {player_item.get('emoji', '❓')} **{player_item.get('name', '?')}** "
            f"— **{format_balance(p_val, mode)}**\n"
            f"**Bot** ╸ {bot_item.get('emoji', '❓')} **{bot_item.get('name', '?')}** "
            f"— **{format_balance(b_val, mode)}**"
        ),
        inline=False,
    )
    result_embed.add_field(
        name="📊  Round Summary",
        value=(
            f"Paid  ╸ **{format_balance(stake, mode)}**\n"
            f"Won   ╸ **{format_balance(total_won, mode)}**\n"
            f"{'Profit' if is_profit else 'Loss' if result_label != 'tie' else 'Net'}  ╸ "
            f"**{profit_sign}{format_balance(abs(profit), mode)}**"
        ),
        inline=True,
    )
    result_embed.add_field(
        name="💳  Wallet",
        value=(
            f"Mode    ╸ `{mode_display}`\n"
            f"Balance ╸ **{format_balance(player.get_balance(mode), mode)}**"
        ),
        inline=True,
    )
    result_embed.set_thumbnail(url=interaction.user.display_avatar.url)
    result_embed.set_footer(text=f"{FOOTER_TEXT}  •  Game ID: {game_uid}  •  Provably Fair")

    GameSession.update_session(
        message_id,
        last_game={
            "game": "Case Battle",
            "result": result_label,
            "amount": total_won,
            "multiplier": round(total_won / stake, 2) if stake else 0,
        },
        in_game=False,
    )

    try:
        await interaction.message.edit(
            embed=None,
            content=None,
            view=play_layout(
                result_embed,
                case_battle_duel_items(player_item, bot_item, revealed=True),
                timeout=None,
            ),
        )
    except Exception:
        pass

    await log_case_battle(
        interaction,
        opponent=opponent,
        challenger=interaction.user,
        case_name=case_name,
        stake=stake,
        mode=mode,
        player_item=player_item,
        bot_item=bot_item,
        winner=winner,
        game_uid=game_uid,
        profit=profit,
    )

    await asyncio.sleep(2.5)
    session_now = GameSession.get_session(message_id)
    if session_now:
        layout = hub_active_layout(message_id, interaction.user, session_now, "case_battle")
        try:
            await interaction.message.edit(embed=None, content=None, view=layout)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
# CASE OPENING
# ════════════════════════════════════════════════════════════════════════════

class CaseSingleSelect(discord.ui.Select):
    """Unified case select — shows house OR community cases based on view_mode.

    Layout (5 rows max):
      Row 0 — GameSelectDropdown
      Row 1 — BetSelect
      Row 2 — CaseSingleSelect  (this)
      Row 3 — OpenCountSelect   (visible only after a case is chosen)
      Row 4 — Toggle | Fav | Open
    """

    def __init__(self, message_id: str, view_mode: str = "house", row: int = 2):
        self.message_id = message_id
        session  = GameSession.get_session(message_id)
        owner_id = int(session.get("owner", 0)) if session else 0
        current  = session.get("case_opening_case_id") if session else None

        data      = _get_cases_data()
        all_cases = data.get("cases", {})
        favorites = _get_user_favorites(owner_id)

        if view_mode == "community":
            cases       = [(cid, c) for cid, c in all_cases.items() if c.get("is_community")]
            placeholder = "🌐 Community Cases — select one"
        else:
            cases       = [(cid, c) for cid, c in all_cases.items() if not c.get("is_community")]
            placeholder = "🏠 House Cases — select one"

        cases.sort(key=lambda x: (0 if x[0] in favorites else 1, x[1].get("name", "")))

        if cases:
            options = [
                discord.SelectOption(
                    label=c.get("name", "Unnamed")[:25],
                    value=cid,
                    emoji=c.get("emoji", "📦"),
                    description=f"{'⭐ ' if cid in favorites else ''}Price: {c.get('price', 0):,}"[:100],
                    default=(cid == current),
                )
                for cid, c in cases[:25]
            ]
        else:
            empty = "— No community cases yet —" if view_mode == "community" else "— No house cases —"
            options = [discord.SelectOption(label=empty, value="_none_")]

        super().__init__(
            placeholder=placeholder,
            options=options,
            min_values=1,
            max_values=1,
            row=row,
            custom_id=f"case_single_sel:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        val = self.values[0]
        if val == "_none_":
            return await interaction.response.defer()
        GameSession.update_session(self.message_id, case_opening_case_id=val)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "case_opening")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class OpenCountSelect(discord.ui.Select):
    """Dropdown to choose how many cases to open — elegant alternative to 5 separate buttons."""

    _COUNTS  = [1, 2, 3, 5, 10]
    _EMOJIS  = ["1️⃣", "2️⃣", "3️⃣", "5️⃣", "🔟"]

    def __init__(self, message_id: str, row: int = 3):
        self.message_id = message_id
        session  = GameSession.get_session(message_id)
        current  = int(session.get("case_opening_count", 1)) if session else 1

        options = [
            discord.SelectOption(
                label=f"Open ×{n}",
                value=str(n),
                description=f"Open {n} case{'s' if n > 1 else ''} at once",
                emoji=self._EMOJIS[i],
                default=(n == current),
            )
            for i, n in enumerate(self._COUNTS)
        ]
        super().__init__(
            placeholder=f"🎲 Quantity: ×{current} — tap to change",
            options=options,
            min_values=1,
            max_values=1,
            row=row,
            custom_id=f"case_count_sel:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        count = int(self.values[0])
        GameSession.update_session(self.message_id, case_opening_count=count)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "case_opening")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class CaseTypeToggleButton(discord.ui.Button):
    """Switch between house and community cases view."""

    def __init__(self, message_id: str, view_mode: str = "house", row: int = 4):
        self.message_id = message_id
        self.view_mode  = view_mode
        label = "🌐 Community" if view_mode == "house" else "🏠 House Cases"
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            row=row,
            custom_id=f"case_toggle:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        new_mode = "community" if self.view_mode == "house" else "house"
        # Clear the selected case when switching categories
        GameSession.update_session(
            self.message_id,
            case_opening_view_mode=new_mode,
            case_opening_case_id=None,
        )
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "case_opening")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class CaseFavButton(discord.ui.Button):
    """Toggle a case as favourite for the session owner."""

    def __init__(self, message_id: str, case_id: str, is_fav: bool, row: int = 4):
        self.message_id = message_id
        self.case_id    = case_id
        super().__init__(
            label="★ Unfav" if is_fav else "☆ Fav",
            style=discord.ButtonStyle.primary if is_fav else discord.ButtonStyle.secondary,
            row=row,
            custom_id=f"case_fav:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        uid  = interaction.user.id
        favs = _get_user_favorites(uid)
        if self.case_id in favs:
            favs = [f for f in favs if f != self.case_id]
        else:
            if len(favs) >= 50:
                favs = favs[-49:]
            favs.append(self.case_id)
        _set_user_favorites(uid, favs)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "case_opening")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class OpenCaseButton(discord.ui.Button):
    """Open the selected case(s)."""

    def __init__(self, message_id: str, row: int = 4):
        self.message_id = message_id
        session = GameSession.get_session(message_id)
        count   = int(session.get("case_opening_count", 1)) if session else 1
        super().__init__(
            label=f"🎰 Open ×{count}",
            style=discord.ButtonStyle.success,
            row=row,
            custom_id=f"case_open_btn:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=str(interaction.user.id)),
                ephemeral=True,
            )
        await _do_case_open(interaction, self.message_id)


# ────────────────────────────────────────────────────────────────────────────
# _do_case_open — standalone coroutine shared by all open-case buttons
# ────────────────────────────────────────────────────────────────────────────

async def _do_case_open(interaction: discord.Interaction, message_id: str) -> None:
    user_lang_id = str(interaction.user.id)

    if not GameSession.is_session_active(message_id):
        return await interaction.response.send_message(
            embed=discord.Embed(
                title=t("games.errors.session_expired_title", user_id=user_lang_id),
                description=t("games.errors.session_expired_desc", user_id=user_lang_id),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )

    session = GameSession.get_session(message_id)
    if not session:
        return await interaction.response.send_message(
            t("games.errors.session_not_found", user_id=user_lang_id),
            ephemeral=True,
        )

    mode    = session.get("mode", "demo")
    case_id = session.get("case_opening_case_id")
    count   = int(session.get("case_opening_count", 1))

    data  = _get_cases_data()
    case  = data["cases"].get(case_id) if case_id else None
    if not case or not case.get("items"):
        return await interaction.response.send_message(
            embed=discord.Embed(
                title="❌ Invalid Case",
                description="Selected case not found or has no items.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )

    price      = case.get("price", 0)
    total_cost = price * count
    player     = Player(interaction.user.id)
    balance    = player.get_balance(mode)

    if balance < total_cost:
        return await interaction.response.send_message(
            embed=discord.Embed(
                title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                description=t(
                    "games.errors.insufficient_balance_desc",
                    user_id=user_lang_id,
                    need=format_balance(total_cost, mode),
                    have=format_balance(balance, mode),
                ),
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )

    GameSession.update_session(message_id, in_game=True)
    GameSession.touch_session(message_id)

    # Provably Fair — generate seeds and log to channel
    from modules.provably_fair import consume_pf_round, hash_seed, log_game_start, log_game_end, new_game_uid
    server_seed, client_seed, nonce, pf_floats = consume_pf_round(interaction.user.id)
    game_uid = new_game_uid()
    if count > len(pf_floats):
        _, _, _, extra_floats = consume_pf_round(interaction.user.id)
        pf_floats = pf_floats + extra_floats

    log_msg = await log_game_start(
        interaction, "Case Opening", "📦", interaction.user,
        total_cost, mode, hash_seed(server_seed), client_seed, nonce, game_uid,
    )

    case_emoji   = case.get("emoji", "📦")
    case_name    = case.get("name", "Case")
    is_community = case.get("is_community", False)
    items        = case.get("items", [])
    pool         = items if len(items) >= 4 else (items * 4)

    # Initial interaction response — clear buttons, show loading
    loading_embed = discord.Embed(
        title=f"🎰  {case_emoji} {case_name}",
        description="🌀 **Preparing your roll...**",
        color=0x5865f2,
    )
    loading_embed.set_footer(text=f"Game ID: {game_uid}  •  Provably Fair")
    from modules.games_play_v2 import status_button

    await interaction.response.edit_message(
        embed=None,
        content=None,
        view=play_layout(
            loading_embed,
            [status_button("Preparing roll...", emoji="🌀")],
            timeout=None,
        ),
    )

    # Deduct balance BEFORE animation
    player.remove_balance(mode, total_cost)
    results   = [_case_open_item_pf(items, pf_floats[i % len(pf_floats)]) for i in range(count)]
    total_won = sum(r.get("value", 0) for r in results)

    # Community case fee — pay owner per open
    if is_community:
        from cogs.cases import PLATFORM_FEE_PCT
        owner_id = case.get("owner_id")
        if owner_id and owner_id != interaction.user.id:
            fee = round(total_cost * (PLATFORM_FEE_PCT / 100))
            if fee > 0:
                Player(owner_id).add_balance("real", fee)

    # Slot-reel animation — batched in groups of 5
    total_batches = (count + 4) // 5
    for batch_idx in range(total_batches):
        start = batch_idx * 5
        batch = results[start:start + 5]
        blabel = f" ({batch_idx + 1}/{total_batches})" if total_batches > 1 else ""
        await _run_case_reel_animation(
            interaction.message, pool, batch, start, game_uid, case_emoji, case_name, blabel,
        )

    player.add_balance(mode, total_won)
    profit = total_won - total_cost

    is_tracking_exempt = _is_tracking_exempt_user(interaction.user.id)
    result_label = "win" if profit >= 0 else "lose"

    if not is_tracking_exempt:
        player.update_stats("Case Opening", total_cost, result_label, profit, mode)
        player.record_game_history({
            "game": "Case Opening",
            "case_name": case_name,
            "mode": mode,
            "cost": total_cost,
            "won": total_won,
            "profit": profit,
            "count": count,
            "items_won": [r.get("name") for r in results],
        })

    if isinstance(interaction.user, discord.Member) and not is_tracking_exempt:
        from Games.base_game import _check_and_assign_tier_role
        await _check_and_assign_tier_role(interaction.user, player)
        levels_cog = interaction.client.cogs.get("LevelsCog")
        if levels_cog:
            await levels_cog.process_level_up(interaction.user.id)

    from modules.event_manager import process_game_event
    from cogs.events import send_event_completion
    _ev_case = process_game_event(
        interaction.user.id,
        {"game": "case_opening", "bet": total_cost, "mode": mode},
        player,
    )

    # Result embed
    profit_sign  = "+" if profit >= 0 else "-"
    is_profit    = profit >= 0
    color        = 0x57f287 if is_profit else 0xed4245
    mode_display = (
        t("games.mode_demo", user_id=user_lang_id)
        if mode == "demo"
        else t("games.mode_real", user_id=user_lang_id)
    )
    from modules.constants import FOOTER_TEXT

    if count == 1:
        item   = results[0]
        header = "🎉  JACKPOT!" if is_profit else "📦  Better luck next time!"
        result_embed = discord.Embed(title=header, color=color)
        result_embed.add_field(
            name=f"{case_emoji}  {case_name}",
            value=(
                f"> {item.get('emoji', '❓')}  **{item.get('name', '?')}**\n"
                f"> 💰 Worth: **{format_balance(item.get('value', 0), mode)}**"
            ),
            inline=False,
        )
    else:
        header = "🎉  Great haul!" if is_profit else "📦  Better luck next time!"
        lines  = [
            f"{r.get('emoji', '❓')} **{r.get('name', '?')}** ╸ {format_balance(r.get('value', 0), mode)}"
            for r in results
        ]
        result_embed = discord.Embed(title=header, color=color)
        result_embed.add_field(
            name=f"{case_emoji}  {case_name}  ×{count}",
            value="\n".join(lines) or "—",
            inline=False,
        )

    result_embed.add_field(
        name="📊  Round Summary",
        value=(
            f"Paid  ╸ **{format_balance(total_cost, mode)}**\n"
            f"Won   ╸ **{format_balance(total_won, mode)}**\n"
            f"{'Profit' if is_profit else 'Loss'}  ╸ **{profit_sign}{format_balance(abs(profit), mode)}**"
        ),
        inline=True,
    )
    result_embed.add_field(
        name="💳  Wallet",
        value=(
            f"Mode    ╸ `{mode_display}`\n"
            f"Balance ╸ **{format_balance(player.get_balance(mode), mode)}**"
        ),
        inline=True,
    )
    result_embed.set_thumbnail(url=interaction.user.display_avatar.url)
    result_embed.set_footer(text=f"{FOOTER_TEXT}  •  Game ID: {game_uid}  •  Provably Fair")

    last_game_info = {
        "game": "Case Opening",
        "result": result_label,
        "amount": total_won,
        "multiplier": round(total_won / total_cost, 2) if total_cost else 0,
    }
    GameSession.update_session(message_id, last_game=last_game_info, in_game=False)

    try:
        await interaction.message.edit(
            embed=None,
            content=None,
            view=play_layout(result_embed, [], timeout=None),
        )
    except Exception:
        pass

    await log_game_end(
        log_msg, "Case Opening", "📦", interaction.user,
        total_cost, mode, server_seed, client_seed, nonce, game_uid,
        result_label,
        {"case": case_name, "count": count, "items_won": [r.get("name") for r in results]},
        profit,
    )

    await asyncio.sleep(2.5)
    if _ev_case:
        await send_event_completion(interaction, _ev_case)
    session_now = GameSession.get_session(message_id)
    if session_now:
        layout = hub_active_layout(message_id, interaction.user, session_now, "case_opening")
        try:
            await interaction.message.edit(embed=None, content=None, view=layout)
        except Exception:
            pass


# ─── Blackjack views ──────────────────────────────────────────────────────────

def _bj_build_embed(interaction: discord.Interaction, state: dict,
                    emoji_map: dict, mode: str, main_bet: int,
                    *, is_result: bool = False) -> discord.Embed:
    """Build the blackjack playing/result embed."""
    user_lang_id = str(interaction.user.id)
    done   = state["phase"] == "done"
    cur    = state["cur"]
    hands  = state["hands"]

    # ── Dealer row ────────────────────────────────────────────────────────────
    d_hide  = not done and not is_result
    d_str   = bj_hand_display(state["dealer"], emoji_map, hide_second=d_hide)
    d_val   = bj_hand_value(state["dealer"])
    if d_hide:
        up_val = bj_hand_value([state["dealer"][0]])
        d_info = f"(**?+{up_val}**)"
    else:
        d_info = f"(**{d_val}**)"
        if bj_is_blackjack(state["dealer"]):
            d_info += "  🃏 **BLACKJACK!**"

    # ── Player hand rows ──────────────────────────────────────────────────────
    hand_blocks = []
    for i, h in enumerate(hands):
        hval  = bj_hand_value(h["cards"])
        hstr  = bj_hand_display(h["cards"], emoji_map)
        label = f"Hand {i+1}" if len(hands) > 1 else "Your Hand"
        bet_str = format_balance(h["bet"], mode)

        if h["status"] == "busted":
            status_str = "  💥 **Bust**"
        elif bj_is_blackjack(h["cards"]) and not h["from_split"]:
            status_str = "  🃏 **Blackjack!**"
        else:
            status_str = ""

        arrow = "▶ " if (i == cur and not done) else ""
        # Use # heading so the emoji line renders large
        hand_blocks.append(
            f"**{arrow}{label}** ({hval}){status_str}  ·  *{bet_str}*\n"
            f"# {hstr}"
        )

    # ── Side-bet results ──────────────────────────────────────────────────────
    sr     = state.get("side_results") or {}
    side_parts = []
    if sr.get("pp_payout", 0):
        side_parts.append(
            f"🃏 **Perfect Pairs** — {sr.get('pp_label','').replace('_',' ').title()} "
            f"({sr['pp_mult']}x) +{format_balance(sr['pp_payout'], mode)}"
        )
    if sr.get("t3_payout", 0):
        side_parts.append(
            f"🎴 **21+3** — {sr.get('t3_label','').replace('_',' ').title()} "
            f"({sr['t3_mult']}x) +{format_balance(sr['t3_payout'], mode)}"
        )

    # ── Assemble description ──────────────────────────────────────────────────
    desc_parts = [
        f"🏪 **DEALER**  {d_info}",
        f"# {d_str}",
        "",
    ] + hand_blocks

    if side_parts:
        desc_parts += ["", "**Side Bets:**"] + side_parts

    ins = state.get("insurance_bet", 0)
    if ins:
        desc_parts.append(f"🛡 Insurance bet: {format_balance(ins, mode)}")

    # ── Result summary ────────────────────────────────────────────────────────
    if done and is_result:
        ev  = bj_evaluate(state)
        net = ev["total_return"] - main_bet
        desc_parts.append("")
        label_map = {
            "blackjack": "🃏 Blackjack!",
            "win":       "✅ Win",
            "push":      "🤝 Push",
            "lose":      "❌ Lose",
            "bust":      "💥 Bust",
        }
        for h_res in ev["results"]:
            r_label = label_map.get(h_res["result"], h_res["result"])
            if h_res["payout"]:
                desc_parts.append(f"{r_label}  +{format_balance(h_res['payout'], mode)}")
            else:
                desc_parts.append(r_label)
        sign = "+" if net >= 0 else ""
        net_emoji = "🎉" if net > 0 else ("🤝" if net == 0 else "😢")
        desc_parts.append(
            f"\n**{net_emoji} Net: {sign}{format_balance(net, mode)}**"
        )

    color = discord.Color.green() if done else discord.Color.blurple()
    embed = discord.Embed(
        title=t("games.blackjack.title", user_id=user_lang_id),
        description="\n".join(desc_parts),
        color=color,
    )
    embed.set_footer(text=t("games.footer", user_id=user_lang_id))
    return embed


class _BjActionView(discord.ui.View):
    """Dynamic action view shown during blackjack play."""

    def __init__(self, message_id: str, state: dict, main_bet: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.main_bet   = main_bet
        done = state["phase"] == "done"
        if done:
            return
        self.add_item(_BjHitButton(message_id))
        self.add_item(_BjStandButton(message_id))
        if bj_can_double(state):
            self.add_item(_BjDoubleButton(message_id))
        if bj_can_split(state):
            self.add_item(_BjSplitButton(message_id))
        if bj_can_insurance(state):
            self.add_item(_BjInsuranceButton(message_id))


class _BjHitButton(discord.ui.Button):
    def __init__(self, message_id: str):
        super().__init__(
            label="Hit", style=discord.ButtonStyle.primary,
            emoji="🃏", row=0, custom_id=f"bj_hit:{message_id}",
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        await _bj_handle_action(interaction, self.message_id, "hit")


class _BjStandButton(discord.ui.Button):
    def __init__(self, message_id: str):
        super().__init__(
            label="Stand", style=discord.ButtonStyle.danger,
            emoji="🛑", row=0, custom_id=f"bj_stand:{message_id}",
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        await _bj_handle_action(interaction, self.message_id, "stand")


class _BjDoubleButton(discord.ui.Button):
    def __init__(self, message_id: str):
        super().__init__(
            label="Double Down", style=discord.ButtonStyle.success,
            emoji="⬆️", row=1, custom_id=f"bj_double:{message_id}",
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        await _bj_handle_action(interaction, self.message_id, "double")


class _BjSplitButton(discord.ui.Button):
    def __init__(self, message_id: str):
        super().__init__(
            label="Split", style=discord.ButtonStyle.secondary,
            emoji="↔️", row=1, custom_id=f"bj_split:{message_id}",
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        await _bj_handle_action(interaction, self.message_id, "split")


class _BjInsuranceButton(discord.ui.Button):
    def __init__(self, message_id: str):
        super().__init__(
            label="Take Insurance", style=discord.ButtonStyle.secondary,
            emoji="🛡", row=1, custom_id=f"bj_insurance:{message_id}",
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        await _bj_handle_action(interaction, self.message_id, "insurance")


class _BjPlayAgainButton(discord.ui.Button):
    def __init__(self, message_id: str):
        super().__init__(
            label="Play Again", style=discord.ButtonStyle.success,
            emoji="🔄", row=0, custom_id=f"bj_again:{message_id}",
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=user_lang_id), ephemeral=True
            )
        GameSession.update_session(self.message_id, bj_state=None, in_game=False)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "blackjack")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


async def _bj_handle_action(interaction: discord.Interaction, message_id: str, action: str):
    """Central handler for all blackjack in-game actions."""
    user_lang_id = str(interaction.user.id)

    if not GameSession.check_owner(message_id, interaction.user.id):
        return await interaction.response.send_message(
            t("games.errors.not_your_session", user_id=user_lang_id), ephemeral=True
        )
    if not GameSession.is_session_active(message_id):
        return await interaction.response.send_message(
            embed=discord.Embed(
                title=t("games.errors.session_expired_title", user_id=user_lang_id),
                description=t("games.errors.session_expired_desc", user_id=user_lang_id),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )

    session  = GameSession.get_session(message_id)
    if not session:
        return await interaction.response.send_message(
            t("games.errors.session_not_found", user_id=user_lang_id), ephemeral=True
        )

    state     = session.get("bj_state")
    if not state:
        return await interaction.response.send_message("No active blackjack round.", ephemeral=True)

    mode      = session.get("mode", "demo")
    main_bet  = state["hands"][0]["bet"] if state["hands"] else int(session.get("bet", 100))
    player    = Player(interaction.user.id)
    emoji_map = _get_bj_emoji_map(interaction.client)

    await interaction.response.defer()

    if action == "hit":
        if state.get("rigged"):
            _rig_hit_card(state)
        state = do_hit(state)
    elif action == "stand":
        state = do_stand(state)
        if state.get("rigged") and state["phase"] == "done":
            _non_busted = [bj_hand_value(h["cards"]) for h in state["hands"] if h["status"] != "busted"]
            if _non_busted and max(_non_busted) >= 17:
                _rig_dealer_beat(state)
    elif action == "double":
        balance = player.get_balance(mode)
        extra   = state["hands"][state["cur"]]["bet"]
        if balance < extra:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                    description=t("games.errors.insufficient_balance_desc",
                                  user_id=user_lang_id,
                                  need=format_balance(extra, mode),
                                  have=format_balance(balance, mode)),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        if state.get("rigged"):
            _rig_hit_card(state)
        state = do_double(state, player=player, mode=mode)
        if state.get("rigged") and state["phase"] == "done":
            _non_busted = [bj_hand_value(h["cards"]) for h in state["hands"] if h["status"] != "busted"]
            if _non_busted and max(_non_busted) >= 17:
                _rig_dealer_beat(state)
    elif action == "split":
        balance = player.get_balance(mode)
        extra   = state["hands"][state["cur"]]["bet"]
        if balance < extra:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                    description=t("games.errors.insufficient_balance_desc",
                                  user_id=user_lang_id,
                                  need=format_balance(extra, mode),
                                  have=format_balance(balance, mode)),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        state = do_split(state, player=player, mode=mode)
    elif action == "insurance":
        balance   = player.get_balance(mode)
        ins_amount = main_bet // 2
        if balance < ins_amount:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                    description=t("games.errors.insufficient_balance_desc",
                                  user_id=user_lang_id,
                                  need=format_balance(ins_amount, mode),
                                  have=format_balance(balance, mode)),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        state = do_insurance(state, ins_amount, player=player, mode=mode)

    # Save updated state
    GameSession.update_session(message_id, bj_state=state)
    GameSession.touch_session(message_id)
    _ev_bj: list = []

    is_result = state["phase"] == "done"

    # If done → settle bets
    if is_result:
        ev        = bj_evaluate(state)
        total_bet = sum(h["bet"] for h in state["hands"])
        payout = ev["total_return"]
        if payout > 0:
            player.add_balance(mode, payout)
        # Net result for stats
        net = payout - total_bet
        result_label = "win" if net > 0 else ("tie" if net == 0 else "lose")
        from modules.game_log import post_short_game_log

        await post_short_game_log(
            interaction.user,
            "Blackjack",
            result_label,
            net,
            mode,
            client=interaction.client,
            guild_id=interaction.guild.id if interaction.guild else None,
        )
        if not _is_tracking_exempt_user(interaction.user.id):
            player.update_stats("blackjack", total_bet, result_label, net, mode)
            player.record_game_history({
                "game": "Blackjack", "bet": total_bet, "result": result_label,
                "amount": abs(net), "multiplier": round(payout / total_bet, 2) if total_bet else 0,
                "mode": mode,
            })
            if mode == "real":
                # Bonus wager tracking
                current_bal = player.get_balance("real")
                active_bonus = bonus_engine.get_active_bonus(interaction.user.id)
                if active_bonus and active_bonus.get("type") == "fixed":
                    bonus_engine.check_balance_milestone(interaction.user.id, current_bal)
                    bonus_engine.check_forfeit(interaction.user.id, current_bal)
                else:
                    wager_done = bonus_engine.add_wager(interaction.user.id, total_bet)
                    if not wager_done:
                        bonus_engine.check_forfeit(interaction.user.id, current_bal)
                # Promo wager tracking & auto-forfeit
                promo_done = promo_engine.on_real_bet_wagered(interaction.user.id, total_bet)
                if not promo_done:
                    promo_engine.check_forfeit_promo(interaction.user.id, current_bal)
                # Race wager tracking
                race_engine.add_entry(interaction.user.id, total_bet, "wager")
        GameSession.update_session(
            message_id, in_game=False,
            last_game={"game": "Blackjack", "result": result_label,
                       "amount": abs(net), "multiplier": round(payout / total_bet, 2) if total_bet else 0},
        )
        # Check if any non-split hand got a natural BJ win
        _bj_natural = any(
            r.get("result") == "blackjack"
            for r in ev.get("results", [])
        )
        from modules.event_manager import process_game_event
        from cogs.events import send_event_completion
        _ev_bj = process_game_event(
            interaction.user.id,
            {"game": "blackjack", "won": result_label == "win",
             "is_natural_bj": _bj_natural, "bet": total_bet, "mode": mode},
            player,
        )

    embed = _bj_build_embed(interaction, state, emoji_map, mode, main_bet, is_result=is_result)
    bj_items = list(_BjActionView(message_id, state, main_bet).children) if not is_result else []
    try:
        await interaction.message.edit(
            embed=None,
            content=None,
            view=play_layout(embed, bj_items, timeout=None),
        )
    except Exception:
        pass

    if is_result:
        if _ev_bj:
            await send_event_completion(interaction, _ev_bj)
        await asyncio.sleep(2.5)
        GameSession.update_session(message_id, bj_state=None, in_game=False)
        session_now = GameSession.get_session(message_id)
        if session_now:
            layout = hub_active_layout(message_id, interaction.user, session_now, "blackjack")
            try:
                await interaction.message.edit(embed=None, content=None, view=layout)
            except Exception:
                pass


class _BjPPToggleButton(discord.ui.Button):
    """Toggle Perfect Pairs side bet (0 ↔ current bet amount)."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session = GameSession.get_session(message_id)
        pp_bet  = int(session.get("bj_pp_bet", 0)) if session else 0
        active  = pp_bet > 0
        super().__init__(
            label=f"PP ✓ {pp_bet:,}" if active else "Perfect Pairs",
            style=discord.ButtonStyle.success if active else discord.ButtonStyle.secondary,
            emoji="🃏",
            row=3,
            custom_id=f"bj_pp_toggle:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=user_lang_id), ephemeral=True
            )
        session  = GameSession.get_session(self.message_id)
        if not session:
            return
        bet      = int(session.get("bet", 100))
        current  = int(session.get("bj_pp_bet", 0))
        new_val  = 0 if current > 0 else max(1, bet * 20 // 100)
        GameSession.update_session(self.message_id, bj_pp_bet=new_val)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "blackjack")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class _BjT3ToggleButton(discord.ui.Button):
    """Toggle 21+3 side bet (0 ↔ current bet amount)."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session = GameSession.get_session(message_id)
        t3_bet  = int(session.get("bj_t3_bet", 0)) if session else 0
        active  = t3_bet > 0
        super().__init__(
            label=f"21+3 ✓ {t3_bet:,}" if active else "21+3",
            style=discord.ButtonStyle.success if active else discord.ButtonStyle.secondary,
            emoji="🎴",
            row=3,
            custom_id=f"bj_t3_toggle:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=user_lang_id), ephemeral=True
            )
        session  = GameSession.get_session(self.message_id)
        if not session:
            return
        bet      = int(session.get("bet", 100))
        current  = int(session.get("bj_t3_bet", 0))
        new_val  = 0 if current > 0 else max(1, bet * 20 // 100)
        GameSession.update_session(self.message_id, bj_t3_bet=new_val)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "blackjack")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class StartBlackjackButton(discord.ui.Button):
    """Deal button — uses pending side bets from session, no modal."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session  = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        _bj_db = (get_data("server/games") or {}).get("blackjack", {})
        _bj_e  = str(_bj_db.get("emoji") or "🃏")
        _btn_e = _bj_e if not _bj_e.startswith("<") else "🃏"
        super().__init__(
            label=t("games.blackjack.deal_button", user_id=owner_id),
            style=discord.ButtonStyle.success,
            emoji=_btn_e,
            row=3,
            custom_id=f"bj_start:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=user_lang_id), ephemeral=True
            )
        if not GameSession.is_session_active(self.message_id):
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.session_expired_title", user_id=user_lang_id),
                    description=t("games.errors.session_expired_desc", user_id=user_lang_id),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
        session  = GameSession.get_session(self.message_id)
        if not session:
            return await interaction.response.send_message(
                t("games.errors.session_not_found", user_id=user_lang_id), ephemeral=True
            )
        mode     = session.get("mode", "demo")
        main_bet = int(session.get("bet", 100))
        side_pp  = int(session.get("bj_pp_bet", 0))
        side_t3  = int(session.get("bj_t3_bet", 0))
        total    = main_bet + side_pp + side_t3
        player   = Player(interaction.user.id)
        balance  = player.get_balance(mode)
        if balance < total:
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                    description=t("games.errors.insufficient_balance_desc",
                                  user_id=user_lang_id,
                                  need=format_balance(total, mode),
                                  have=format_balance(balance, mode)),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )

        player.remove_balance(mode, total)

        state = bj_new_state(main_bet, side_pp=side_pp, side_21_3=side_t3)

        # Rigged check — before natural BJ check; rig the initial deal
        _bj_settings = _get_blackjack_settings()
        if mode == "real" and random.random() < _bj_settings["rigged_chance"] / 100.0:
            _rig_initial_deal(state)
            state["rigged"] = True

        sr = evaluate_side_bets(state)
        state["side_results"] = sr
        side_win = sr.get("pp_payout", 0) + sr.get("t3_payout", 0)
        if side_win:
            player.add_balance(mode, side_win)

        # Auto-resolve: player has blackjack on initial deal (never happens when rigged)
        player_bj = bj_is_blackjack(state["hands"][0]["cards"])
        if player_bj:
            state["rigged"] = False  # safety: natural BJ overrides rig
            state = do_stand(state)  # triggers dealer play, phase → "done"

        # Reset pending side bets for next round
        GameSession.update_session(self.message_id, bj_state=state, in_game=True,
                                   bj_pp_bet=0, bj_t3_bet=0)
        GameSession.touch_session(self.message_id)

        emoji_map = _get_bj_emoji_map(interaction.client)

        if player_bj:
            ev     = bj_evaluate(state)
            payout = ev["total_return"]
            if payout > 0:
                player.add_balance(mode, payout)
            net          = payout - main_bet
            result_label = "win" if net > 0 else ("tie" if net == 0 else "lose")
            from modules.game_log import post_short_game_log

            await post_short_game_log(
                interaction.user,
                "Blackjack",
                result_label,
                net,
                mode,
                client=interaction.client,
                guild_id=interaction.guild.id if interaction.guild else None,
            )
            if not _is_tracking_exempt_user(interaction.user.id):
                player.update_stats("blackjack", main_bet, result_label, net, mode)
                player.record_game_history({
                    "game": "Blackjack", "bet": main_bet, "result": result_label,
                    "amount": abs(net),
                    "multiplier": round(payout / main_bet, 2) if main_bet else 0,
                    "mode": mode,
                })
            GameSession.update_session(
                self.message_id, in_game=False,
                last_game={"game": "Blackjack", "result": result_label,
                           "amount": abs(net),
                           "multiplier": round(payout / main_bet, 2) if main_bet else 0},
            )
            # Event hook — natural BJ auto-resolve is always a natural BJ
            from modules.event_manager import process_game_event
            from cogs.events import send_event_completion
            _ev_bj_deal = process_game_event(
                interaction.user.id,
                {"game": "blackjack", "won": result_label == "win",
                 "is_natural_bj": True, "bet": main_bet, "mode": mode},
                player,
            )
            embed = _bj_build_embed(interaction, state, emoji_map, mode, main_bet, is_result=True)
            await interaction.response.edit_message(
                embed=None, content=None, view=play_layout(embed, [], timeout=None)
            )
            if _ev_bj_deal:
                await send_event_completion(interaction, _ev_bj_deal)
            await asyncio.sleep(2.5)
            GameSession.update_session(self.message_id, bj_state=None, in_game=False)
            session_now = GameSession.get_session(self.message_id)
            if session_now:
                layout = hub_active_layout(self.message_id, interaction.user, session_now, "blackjack")
                try:
                    await interaction.message.edit(embed=None, content=None, view=layout)
                except Exception:
                    pass
            return

        embed = _bj_build_embed(interaction, state, emoji_map, mode, main_bet)
        bj_view = _BjActionView(self.message_id, state, main_bet)
        bj_items = list(bj_view.children)
        await interaction.response.edit_message(
            embed=None,
            content=None,
            view=play_layout(embed, bj_items, timeout=None),
        )


# ════════════════════════════════════════════════════════════════════════════
# HiLo — Provably Fair card guessing game
# ════════════════════════════════════════════════════════════════════════════

def _hilo_build_embed(
    user: discord.Member,
    state: dict,
    emoji_map: dict,
    mode: str,
    *,
    is_result: bool = False,
) -> discord.Embed:
    """HiLo oyun embed'i oluştur (oynama veya sonuç)."""
    deck        = state["deck"]
    card_idx    = state["card_idx"]
    multiplier  = state["multiplier"]
    bet         = state["bet"]
    rnd         = state["round"]
    phase       = state["phase"]
    last_result = state.get("last_result")
    last_choice = state.get("last_choice")
    game_uid    = state.get("game_uid", "")
    history     = state.get("history", [])

    # ── Gösterilecek kartı ve rengi belirle ──────────────────────
    if phase == "done" and last_result == "lose" and history:
        # Kaybettiren kartı göster
        show_card = history[-1]["next"]
        header    = "💥  **WRONG!**"
        color     = 0xe74c3c
    elif phase == "done":
        show_card = deck[card_idx]
        header    = "💸  **CASHED OUT!**"
        color     = 0x2ecc71
    else:
        show_card = deck[card_idx]
        header    = f"🃏  **CARD**  ·  Round **{rnd}**"
        color     = 0x3498db if rnd == 0 else 0x9b59b6

    # ── Kart yazı etiketi (emoji'nin yanında okunabilir metin) ────
    _RANK_FULL = {
        'A': 'Ace', '2': '2', '3': '3', '4': '4', '5': '5',
        '6': '6', '7': '7', '8': '8', '9': '9', '0': '10',
        'J': 'Jack', 'Q': 'Queen', 'K': 'King',
    }
    _SUIT_FULL = {'C': 'Clubs ♣', 'H': 'Hearts ♥', 'D': 'Diamonds ♦', 'S': 'Spades ♠'}
    _r, _s   = show_card[0], show_card[1]
    card_label = f"{_RANK_FULL.get(_r, _r)} of {_SUIT_FULL.get(_s, _s)}"

    card_str  = bj_card_display(show_card, emoji_map)
    remaining = deck[card_idx + 1:] if card_idx + 1 < len(deck) else []
    odds      = calc_hilo_odds(deck[card_idx], remaining) if phase == "playing" else {}
    current_win = int(bet * multiplier)

    # ── Son hamle durum satırı ────────────────────────────────────
    status_line = ""
    if last_result == "win" and phase == "playing":
        lm    = history[-1]["mult"] if history else 1.0
        arrow = "📈" if last_choice == "higher" else "📉"
        status_line = f"\n{arrow} **Correct!**  `×{lm:.2f}` → total **×{multiplier:.4g}**"
    elif last_result == "push" and phase == "playing":
        status_line = "\n🔄  **Same rank — pushed forward**"
    elif last_result == "lose" and history:
        arrow  = "📈" if last_choice == "higher" else "📉"
        actual = history[-1].get("actual", "?")
        status_line = (
            f"\n{arrow} Guessed **{last_choice.title()}** → "
            f"revealed **{actual.title()}**"
        )

    # ── Oran satırı (sadece oyun sırasında) ──────────────────────
    odds_line = ""
    if phase == "playing" and odds and odds["total"] > 0:
        _RV2 = {'A': 14, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6,
                '7': 7,  '8': 8, '9': 9, '0': 10, 'J': 11, 'Q': 12, 'K': 13}
        _cv2        = _RV2.get(deck[card_idx][0], 0)
        _same_count = odds.get("same_count", 0)
        _same_pct   = _same_count / odds["total"] if odds["total"] > 0 else 0
        _same_mult  = round((1 - HILO_HOUSE_EDGE) / _same_pct, 2) if _same_pct > 0 else 0.0

        if _cv2 == 14:  # Ace
            h_lbl  = "📈 **Same or Higher**"
            h_mult = f"**{_same_mult:.2f}x**" if _same_mult > 0 else "**—**"
            h_pct  = f"`{_same_pct * 100:.1f}%`"
        else:
            h_lbl  = "📈 **Higher**"
            h_mult = f"**{odds['higher_mult']:.2f}x**" if odds["higher_mult"] > 0 else "**—**"
            h_pct  = f"`{odds['higher_pct']:.1f}%`"

        if _cv2 == 2:  # 2
            l_lbl  = "📉 **Same or Lower**"
            l_mult = f"**{_same_mult:.2f}x**" if _same_mult > 0 else "**—**"
            l_pct  = f"`{_same_pct * 100:.1f}%`"
        else:
            l_lbl  = "📉 **Lower**"
            l_mult = f"**{odds['lower_mult']:.2f}x**"  if odds["lower_mult"]  > 0 else "**—**"
            l_pct  = f"`{odds['lower_pct']:.1f}%`"

        odds_line = (
            f"\n\n"
            f"┌ {h_lbl}   {h_pct}   {h_mult}\n"
            f"└ {l_lbl}    {l_pct}   {l_mult}"
        )

    # ── Çarpan satırı ─────────────────────────────────────────────
    mult_line = (
        f"\n\n📊 **Multiplier:** `×{multiplier:.4g}`  ·  "
        f"⛓ **Rounds:** `{rnd}`"
    )

    # ── Para satırı ───────────────────────────────────────────────
    if phase == "done" and last_result == "lose":
        money_line = f"\n💀 **Lost:** {format_balance(bet, mode)}"
    elif phase == "done":
        profit = current_win - bet
        sign   = "+" if profit >= 0 else ""
        money_line = (
            f"\n💰 **Bet:** {format_balance(bet, mode)}  ·  "
            f"🏆 **Won:** {format_balance(current_win, mode)}\n"
            f"**🎉 Net:** __{sign}{format_balance(profit, mode)}__"
        )
    else:
        money_line = (
            f"\n💰 **Bet:** {format_balance(bet, mode)}  ·  "
            f"💵 **To Win:** {format_balance(current_win, mode)}"
        )

    # ── Geçmiş şerit ─────────────────────────────────────────────
    history_strip = ""
    if history:
        icons = {"win": "✅", "lose": "❌", "push": "🔄"}
        parts = [icons.get(h["result"], "⬜") for h in history]
        history_strip = f"\n{''.join(parts)}"

    # ── Game ID ───────────────────────────────────────────────────
    uid_line = f"\n\n🔐 **Game ID:** `{game_uid}`" if game_uid else ""

    desc = (
        f"{header}{status_line}\n"
        f"# {card_str}\n"
        f"-# **{card_label}**"
        f"{odds_line}"
        f"{mult_line}"
        f"{money_line}"
        f"{history_strip}"
        f"{uid_line}"
    )

    embed = discord.Embed(
        title="🎴  H I — L O",
        description=desc,
        color=color,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="Vegas Casino | HiLo • Provably Fair")
    return embed


class _HiLoView(discord.ui.View):
    """HiLo oynama sırasındaki buton görünümü."""

    def __init__(self, message_id: str, state: dict):
        super().__init__(timeout=None)
        self.message_id = message_id
        if state["phase"] != "playing":
            return

        deck      = state["deck"]
        card_idx  = state["card_idx"]
        remaining = deck[card_idx + 1:] if card_idx + 1 < len(deck) else []
        odds      = calc_hilo_odds(deck[card_idx], remaining)

        _RV = {'A': 14, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6,
               '7': 7,  '8': 8, '9': 9, '0': 10, 'J': 11, 'Q': 12, 'K': 13}
        cv = _RV.get(deck[card_idx][0], 0)

        same_count = odds["same_count"]
        total      = odds["total"]
        same_pct   = same_count / total if total > 0 else 0
        same_mult  = round((1 - HILO_HOUSE_EDGE) / same_pct, 2) if same_pct > 0 else 0.0

        if cv == 14:  # Ace — Higher impossible, show "Same or Higher"
            h_label    = f"≥ Same/Higher  {same_mult:.2f}x" if same_mult > 0 else "≥ Same or Higher"
            l_label    = f"Lower  {odds['lower_mult']:.2f}x" if odds["lower_mult"] > 0 else "Lower  —"
            h_disabled = False
            l_disabled = (odds["lower_mult"] == 0)
        elif cv == 2:  # 2 — Lower impossible, show "Same or Lower"
            h_label    = f"Higher  {odds['higher_mult']:.2f}x" if odds["higher_mult"] > 0 else "Higher  —"
            l_label    = f"≤ Same/Lower  {same_mult:.2f}x" if same_mult > 0 else "≤ Same or Lower"
            h_disabled = (odds["higher_mult"] == 0)
            l_disabled = False
        else:
            h_label    = f"Higher  {odds['higher_mult']:.2f}x" if odds["higher_mult"] > 0 else "Higher  —"
            l_label    = f"Lower   {odds['lower_mult']:.2f}x"  if odds["lower_mult"]  > 0 else "Lower   —"
            h_disabled = (odds["higher_mult"] == 0)
            l_disabled = (odds["lower_mult"] == 0)

        self.add_item(_HiLoHigherBtn(message_id, h_label, disabled=h_disabled))
        self.add_item(_HiLoLowerBtn(message_id,  l_label, disabled=l_disabled))

        if state["round"] > 0:
            self.add_item(_HiLoCashOutBtn(message_id))


class _HiLoHigherBtn(discord.ui.Button):
    def __init__(self, message_id: str, label: str, *, disabled: bool = False):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.success,
            emoji="📈",
            row=0,
            custom_id=f"hilo_higher:{message_id}",
            disabled=disabled,
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        await _hilo_handle_action(interaction, self.message_id, "higher")


class _HiLoLowerBtn(discord.ui.Button):
    def __init__(self, message_id: str, label: str, *, disabled: bool = False):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.danger,
            emoji="📉",
            row=0,
            custom_id=f"hilo_lower:{message_id}",
            disabled=disabled,
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        await _hilo_handle_action(interaction, self.message_id, "lower")


class _HiLoCashOutBtn(discord.ui.Button):
    def __init__(self, message_id: str):
        super().__init__(
            label="Cash Out",
            style=discord.ButtonStyle.secondary,
            emoji="💰",
            row=1,
            custom_id=f"hilo_cashout:{message_id}",
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        await _hilo_handle_action(interaction, self.message_id, "cashout")


class _HiLoPlayAgainBtn(discord.ui.Button):
    def __init__(self, message_id: str):
        super().__init__(
            label="Play Again",
            style=discord.ButtonStyle.success,
            emoji="🔄",
            row=0,
            custom_id=f"hilo_again:{message_id}",
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)
        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=user_lang_id), ephemeral=True
            )
        _active_hilo.pop(self.message_id, None)
        GameSession.update_session(self.message_id, in_game=False)
        GameSession.touch_session(self.message_id)
        session = GameSession.get_session(self.message_id)
        layout = hub_active_layout(self.message_id, interaction.user, session, "hilo")
        await interaction.response.edit_message(embed=None, content=None, view=layout)


async def _hilo_handle_action(
    interaction: discord.Interaction, message_id: str, action: str
):
    """HiLo buton aksiyonları için merkezi handler."""
    user_lang_id = str(interaction.user.id)

    if not GameSession.check_owner(message_id, interaction.user.id):
        return await interaction.response.send_message(
            t("games.errors.not_your_session", user_id=user_lang_id), ephemeral=True
        )
    if not GameSession.is_session_active(message_id):
        return await interaction.response.send_message(
            embed=discord.Embed(
                title=t("games.errors.session_expired_title", user_id=user_lang_id),
                description=t("games.errors.session_expired_desc", user_id=user_lang_id),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )

    active = _active_hilo.get(message_id)
    if not active:
        return await interaction.response.send_message(
            "No active HiLo round.", ephemeral=True
        )

    state = active["state"]
    mode  = active["mode"]

    if state["phase"] != "playing":
        return await interaction.response.send_message(
            "This round has already ended.", ephemeral=True
        )

    player    = Player(interaction.user.id)
    emoji_map = _get_bj_emoji_map(interaction.client)

    await interaction.response.defer()

    is_free_round = active.get("is_free_round", False)

    if action == "cashout":
        state["phase"]       = "done"
        state["last_result"] = "cashout"
        multiplier = state["multiplier"]
        bet        = state["bet"]
        payout     = int(bet * multiplier)
        profit     = payout - bet

        if not _is_tracking_exempt_user(interaction.user.id):
            if not is_free_round:
                player.add_balance(mode, payout)
                player.update_stats("hilo", bet, "win", profit, mode)
            player.record_game_history({
                "game": "HiLo", "bet": 0 if is_free_round else bet, "result": "win",
                "amount": 0 if is_free_round else profit,
                "multiplier": round(multiplier, 2), "mode": mode,
                **({"free_round": True} if is_free_round else {}),
            })
            if is_free_round and mode == "real":
                _rounds_left, _all_done = promo_engine.on_freeround_result(interaction.user.id, payout)
                if _all_done:
                    promo_engine.complete_freegame_promo(interaction.user.id)

        GameSession.update_session(
            message_id, in_game=False,
            last_game={"game": "HiLo", "result": "win",
                       "amount": profit, "multiplier": round(multiplier, 2)},
        )
        GameSession.touch_session(message_id)

        from modules.provably_fair import log_game_end
        await log_game_end(
            active.get("log_msg"), "HiLo", "🎴", interaction.user,
            bet, mode,
            active["server_seed"], active["client_seed"], active["nonce"],
            active["game_uid"], "win",
            {"rounds": state["round"], "multiplier": round(multiplier, 2)},
            profit,
        )

        embed = _hilo_build_embed(interaction.user, state, emoji_map, mode, is_result=True)
        await interaction.edit_original_response(
            embed=None, content=None, view=play_layout(embed, [], timeout=None)
        )

        await asyncio.sleep(3.0)
        _active_hilo.pop(message_id, None)
        session_now = GameSession.get_session(message_id)
        if session_now:
            layout = hub_active_layout(message_id, interaction.user, session_now, "hilo")
            try:
                await interaction.message.edit(embed=None, content=None, view=layout)
            except Exception:
                pass

    else:
        # Higher or Lower
        state = hilo_guess(state, action)
        active["state"] = state

        if state["phase"] == "done" and state["last_result"] == "lose":
            bet    = state["bet"]
            profit = -bet if not is_free_round else 0

            if not _is_tracking_exempt_user(interaction.user.id):
                if not is_free_round:
                    player.update_stats("hilo", bet, "lose", -bet, mode)
                player.record_game_history({
                    "game": "HiLo", "bet": 0 if is_free_round else bet, "result": "lose",
                    "amount": 0 if is_free_round else bet, "multiplier": 0.0, "mode": mode,
                    **({"free_round": True} if is_free_round else {}),
                })
                if is_free_round and mode == "real":
                    _rounds_left, _all_done = promo_engine.on_freeround_result(interaction.user.id, 0)
                    if _all_done:
                        promo_engine.complete_freegame_promo(interaction.user.id)

            GameSession.update_session(
                message_id, in_game=False,
                last_game={"game": "HiLo", "result": "lose", "amount": bet, "multiplier": 0.0},
            )
            GameSession.touch_session(message_id)

            from modules.provably_fair import log_game_end
            await log_game_end(
                active.get("log_msg"), "HiLo", "🎴", interaction.user,
                bet, mode,
                active["server_seed"], active["client_seed"], active["nonce"],
                active["game_uid"], "lose",
                {"rounds": state["round"], "choice": action},
                profit,
            )

            embed = _hilo_build_embed(interaction.user, state, emoji_map, mode, is_result=True)
            await interaction.edit_original_response(
                embed=None, content=None, view=play_layout(embed, [], timeout=None)
            )

            await asyncio.sleep(3.0)
            _active_hilo.pop(message_id, None)
            session_now = GameSession.get_session(message_id)
            if session_now:
                layout = hub_active_layout(message_id, interaction.user, session_now, "hilo")
                try:
                    await interaction.message.edit(embed=None, content=None, view=layout)
                except Exception:
                    pass

        elif state["phase"] == "done":
            # Deste bitti, zorla cashout
            multiplier = state["multiplier"]
            bet        = state["bet"]
            payout     = int(bet * multiplier)
            profit     = payout - bet

            if not _is_tracking_exempt_user(interaction.user.id):
                if not is_free_round:
                    player.add_balance(mode, payout)
                    player.update_stats("hilo", bet, "win", profit, mode)
                player.record_game_history({
                    "game": "HiLo", "bet": 0 if is_free_round else bet, "result": "win",
                    "amount": 0 if is_free_round else profit,
                    "multiplier": round(multiplier, 2), "mode": mode,
                    **({"free_round": True} if is_free_round else {}),
                })
                if is_free_round and mode == "real":
                    _rounds_left, _all_done = promo_engine.on_freeround_result(interaction.user.id, payout)
                    if _all_done:
                        promo_engine.complete_freegame_promo(interaction.user.id)

            GameSession.update_session(
                message_id, in_game=False,
                last_game={"game": "HiLo", "result": "win",
                           "amount": profit, "multiplier": round(multiplier, 2)},
            )
            GameSession.touch_session(message_id)

            from modules.provably_fair import log_game_end
            await log_game_end(
                active.get("log_msg"), "HiLo", "🎴", interaction.user,
                bet, mode,
                active["server_seed"], active["client_seed"], active["nonce"],
                active["game_uid"], "win",
                {"rounds": state["round"], "multiplier": round(multiplier, 2), "note": "deck_exhausted"},
                profit,
            )

            embed = _hilo_build_embed(interaction.user, state, emoji_map, mode, is_result=True)
            await interaction.edit_original_response(
                embed=None, content=None, view=play_layout(embed, [], timeout=None)
            )

            await asyncio.sleep(3.0)
            _active_hilo.pop(message_id, None)
            session_now = GameSession.get_session(message_id)
            if session_now:
                layout = hub_active_layout(message_id, interaction.user, session_now, "hilo")
                try:
                    await interaction.message.edit(embed=None, content=None, view=layout)
                except Exception:
                    pass

        else:
            # Tur kazanıldı veya push — devam et
            GameSession.touch_session(message_id)
            embed = _hilo_build_embed(interaction.user, state, emoji_map, mode)
            from modules.games_play_v2 import hilo_play_items

            await interaction.edit_original_response(
                embed=None,
                content=None,
                view=play_layout(embed, hilo_play_items(message_id, state), timeout=None),
            )


class StartHiLoButton(discord.ui.Button):
    """HiLo başlat — deste karıştır ve ilk kartı göster."""

    def __init__(self, message_id: str):
        self.message_id = message_id
        session  = GameSession.get_session(message_id)
        owner_id = str(session.get("owner", 0)) if session else "0"
        super().__init__(
            label=t("games.start_game", user_id=owner_id),
            style=discord.ButtonStyle.success,
            emoji="🎴",
            row=3,
            custom_id=f"hilo_start:{message_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        user_lang_id = str(interaction.user.id)

        if not GameSession.check_owner(self.message_id, interaction.user.id):
            return await interaction.response.send_message(
                t("games.errors.not_your_session", user_id=user_lang_id), ephemeral=True
            )
        if not GameSession.is_session_active(self.message_id):
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("games.errors.session_expired_title", user_id=user_lang_id),
                    description=t("games.errors.session_expired_desc", user_id=user_lang_id),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )

        session = GameSession.get_session(self.message_id)
        if not session:
            return await interaction.response.send_message(
                t("games.errors.session_not_found", user_id=user_lang_id), ephemeral=True
            )

        mode = session.get("mode", "demo")
        bet  = int(session.get("bet", 100))

        player    = Player(interaction.user.id)

        # Free-round promo detection (HiLo doesn't extend BaseGame.deduct_bet, handle manually)
        _hilo_promo = promo_engine.get_active_promo(interaction.user.id)
        is_free_round = (
            _hilo_promo
            and _hilo_promo.get("status") == "active"
            and _hilo_promo.get("type") == "freegame"
            and _hilo_promo.get("game", "").lower() == "hilo"
        )
        if is_free_round:
            bet = int(_hilo_promo.get("bet_amount", bet))
        else:
            balance = player.get_balance(mode)
            if balance < bet:
                return await interaction.response.send_message(
                    embed=discord.Embed(
                        title=t("games.errors.insufficient_balance_title", user_id=user_lang_id),
                        description=t(
                            "games.errors.insufficient_balance_desc",
                            user_id=user_lang_id,
                            need=format_balance(bet, mode),
                            have=format_balance(balance, mode),
                        ),
                        color=discord.Color.red(),
                    ),
                    ephemeral=True,
                )

        from modules.provably_fair import consume_pf_round, hash_seed, log_game_start, new_game_uid
        server_seed, client_seed, nonce, floats = consume_pf_round(interaction.user.id)
        game_uid = new_game_uid()

        if not is_free_round:
            player.remove_balance(mode, bet)

        state = new_hilo_state(bet, floats, game_uid=game_uid)

        _active_hilo[self.message_id] = {
            "state":        state,
            "mode":         mode,
            "game_uid":     game_uid,
            "server_seed":  server_seed,
            "client_seed":  client_seed,
            "nonce":        nonce,
            "log_msg":      None,
            "is_free_round": is_free_round,
        }

        GameSession.update_session(self.message_id, in_game=True)
        GameSession.touch_session(self.message_id)

        log_msg = await log_game_start(
            interaction, "HiLo", "🎴", interaction.user,
            bet, mode, hash_seed(server_seed), client_seed, nonce, game_uid,
        )
        _active_hilo[self.message_id]["log_msg"] = log_msg

        emoji_map = _get_bj_emoji_map(interaction.client)
        embed = _hilo_build_embed(interaction.user, state, emoji_map, mode)
        from modules.games_play_v2 import hilo_play_items

        await interaction.response.edit_message(
            embed=None,
            content=None,
            view=play_layout(embed, hilo_play_items(self.message_id, state), timeout=None),
        )


class ActiveGameView(View):
    """Legacy hub view — delegates controls to games_hub_v2 collector."""

    def __init__(self, message_id: str, game: str):
        super().__init__(timeout=None)
        from modules.games_hub_v2 import collect_active_game_controls

        for item in collect_active_game_controls(message_id, game):
            self.add_item(item)


class GameMenuView(View):
    """Legacy oyun menüsü — prefer build_game_menu_layout (V2)."""

    def __init__(self, message_id: str):
        super().__init__(timeout=None)
        self.add_item(GameSelectDropdown(message_id))


def hub_active_layout(message_id: str, user: discord.Member, session: dict, game: str):
    from modules.games_hub_v2 import build_active_game_hub_layout

    return build_active_game_hub_layout(message_id, user, session, game)


def hub_menu_layout(message_id: str, user: discord.abc.User, lang: str | None = None):
    from modules.games_hub_v2 import build_game_menu_layout

    return build_game_menu_layout(message_id, user, lang)


def play_layout(
    embed: discord.Embed,
    controls,
    *,
    timeout: float | None = 600,
):
    from modules.games_play_v2 import build_game_play_layout

    return build_game_play_layout(embed, controls, timeout=timeout)


def create_game_embed(user: discord.Member, session: dict) -> discord.Embed:
    """Oyun embed'i oluştur"""
    game = session.get("game", "none")
    bet = int(session.get("bet", 0))
    mode = session.get("mode", "demo")
    
    player = Player(user.id)
    balance = player.get_balance(mode)
    user_lang_id = str(user.id)
    
    if game == "mines":
        mines_settings = _get_mines_settings()
        mine_count = int(session.get("mines_count", 3))
        safe = MinesGame.TOTAL - mine_count
        first_mult = MinesGame.calc_multiplier(mine_count, 1, mines_settings["house_edge_decimal"])
        info = {
            "name": f"{mines_settings['game']} Mines",
            "desc": (
                f"{t('games.mines.embed_intro', user_id=user_lang_id, safe=safe)}\n"
                f"{mines_settings['mine']} **{t('games.mines.mines_label', user_id=user_lang_id)}:** {mine_count}  │  {mines_settings['gem']} **{t('games.mines.safe_cells_label', user_id=user_lang_id)}:** {safe}\n"
                f"📊 **{t('games.mines.first_gem', user_id=user_lang_id)}:** `{first_mult:.2f}x` — {t('games.mines.more_gems_more_multiplier', user_id=user_lang_id)}"
            ),
            "multiplier": f"{first_mult:.2f}x+",
        }
    elif game == "crystals":
        cs = _get_crystals_settings()
        m = cs["multipliers"]
        h = cs["hidden"]
        info = {
            "name": f"{cs['game']} Crystals",
            "desc": (
                f"{t('games.crystals.embed_intro', user_id=user_lang_id)}\n"
                f"{h} **{t('games.crystals.hidden_reveal', user_id=user_lang_id)}**\n\n"
                f"💠 {t('games.crystals.one_pair', user_id=user_lang_id)}: **{m['one_pair']:.2f}x**\n⭐ {t('games.crystals.two_pair', user_id=user_lang_id)}: **{m['two_pair']:.2f}x**\n"
                f"✨ {t('games.crystals.triple', user_id=user_lang_id)}: **{m['triple']:.2f}x**\n💫 {t('games.crystals.full_house', user_id=user_lang_id)}: **{m['full_house']:.2f}x**\n"
                f"🔥 {t('games.crystals.quadruple', user_id=user_lang_id)}: **{m['quadruple']:.2f}x**\n🌟 {t('games.crystals.quintuple', user_id=user_lang_id)}: **{m['quintuple']:.2f}x**"
            ),
            "multiplier": t("games.crystals.up_to_multiplier", user_id=user_lang_id, value=f"{m['quintuple']:.0f}x"),
        }
    elif game == "towers":
        ts          = _get_towers_settings()
        tower_mode  = session.get("towers_mode", "easy")
        mults       = TowersGame.MULTIPLIERS[tower_mode]
        cols        = TowersGame.COLS[tower_mode]
        max_mult    = mults[-1]
        mode_label  = t(f"games.towers.modes.{tower_mode}", user_id=user_lang_id)
        info = {
            "name": f"{ts['game']} Towers",
            "desc": (
                f"{t('games.towers.embed_intro', user_id=user_lang_id, cols=cols, floors=TowersGame.FLOORS)}\n"
                f"💣 **{t('games.towers.bomb_per_floor', user_id=user_lang_id)}:** 1  │  "
                f"🏢 **{t('games.towers.floors_label', user_id=user_lang_id)}:** {TowersGame.FLOORS}\n"
                f"📊 **{t('games.towers.mode_label', user_id=user_lang_id)}:** {mode_label}\n"
                f"🎯 **{t('games.towers.max_mult', user_id=user_lang_id)}:** `{max_mult:.2f}x`"
            ),
            "multiplier": t("games.towers.up_to_multiplier", user_id=user_lang_id, value=f"{max_mult:.2f}x"),
        }
    elif game == "limbo":
        target = float(session.get("limbo_multiplier", 2.0))
        chance = LimboGame.win_chance(target) * 100
        info = {
            "name": "🚀 Limbo",
            "desc": (
                f"{t('games.limbo.embed_intro', user_id=user_lang_id)}\n"
                f"🎯 **{t('games.limbo.target', user_id=user_lang_id)}:** `{target:.2f}x`\n"
                f"📊 **{t('games.limbo.win_chance', user_id=user_lang_id)}:** `{chance:.2f}%`\n"
                
            ),
            "multiplier": f"{target:.2f}x",
        }
    elif game == "slot":
        num_lines = int(session.get("slot_paylines", 30))
        line_bet  = int(bet / num_lines) if num_lines else bet
        cov_pct   = round(num_lines / 30 * 100)
        from Games.slot import get_slot_emojis as _gse
        _emap, _spin = _gse()
        cherry  = _emap.get("cherry",  "🍒")
        lemon   = _emap.get("lemon",   "🍋")
        orange  = _emap.get("orange",  "🍊")
        grapes  = _emap.get("grapes",  "🍇")
        bell    = _emap.get("bell",    "🔔")
        star    = _emap.get("star",    "⭐")
        diamond = _emap.get("diamond", "💎")
        seven   = _emap.get("seven",   "7️⃣")
        # game emoji from DB
        _slot_db = (get_data("server/games") or {}).get("slot", {})
        _game_e  = str(_slot_db.get("emoji") or "🎰")
        info = {
            "name": f"{_game_e} Slot Machine",
            "desc": (
                f"**3×5 grid  ·  {num_lines} active paylines**  (`{cov_pct}%` coverage)\n"
                f"🎲 **Line bet:** {format_balance(line_bet, mode)} per line\n\n"
                f"{cherry} `10x / 25x / 50x`  {lemon} `15x / 36x / 72x`\n"
                f"{orange} `25x / 60x / 125x`  {grapes} `40x / 100x / 200x`\n"
                f"{bell} `60x / 175x / 350x`  {star} `100x / 300x / 600x`\n"
                f"{diamond} `200x / 600x / 1250x`  {seven} `250x / 750x / 2500x`\n"
                f"*Multipliers: 3-match / 4-match / 5-match per line*"
            ),
            "multiplier": "up to 2500x per line",
        }
    elif game == "blackjack":
        _bj_db  = (get_data("server/games") or {}).get("blackjack", {})
        _bj_e   = str(_bj_db.get("emoji") or "🃏")
        pp_bet  = int(session.get("bj_pp_bet", 0))
        t3_bet  = int(session.get("bj_t3_bet", 0))
        _side_lines = []
        if pp_bet:
            _side_lines.append(f"🃏 Perfect Pairs: **{format_balance(pp_bet, mode)}**")
        if t3_bet:
            _side_lines.append(f"🎴 21+3: **{format_balance(t3_bet, mode)}**")
        _side_str = "\n" + "\n".join(_side_lines) if _side_lines else ""
        info = {
            "name": f"{_bj_e} Blackjack",
            "desc": t("games.game_descriptions.blackjack", user_id=user_lang_id) + _side_str,
            "multiplier": "3:2 (BJ) / 2:1 (Insurance)",
        }
    elif game == "hilo":
        info = {
            "name": "🎴 HiLo",
            "desc": (
                f"{t('games.game_descriptions.hilo', user_id=user_lang_id)}\n\n"
                f"📈 **Higher** — next card has a greater value\n"
                f"📉 **Lower**  — next card has a lower value\n"
                f"🔄 Same rank → **push** (no loss, continue)\n"
                f"　*Exception: Ace or 2 — same rank = loss*\n"
                f"💰 **Cash Out** anytime after round 1"
            ),
            "multiplier": "∞ (unlimited)",
        }
    elif game == "case_battle":
        from modules.case_battle import get_allowed_battle_cases

        cases = get_allowed_battle_cases()
        case_id = session.get("case_battle_case_id")
        opponent = session.get("case_battle_opponent", "bot")
        case = cases.get(case_id) if case_id else None
        opp_label = "🤖 Bot" if opponent == "bot" else str(opponent)
        if case:
            price = int(case.get("price", 0))
            items = case.get("items", [])
            info = {
                "name": "⚔️ Case Battle",
                "desc": (
                    f"**Case:** {case.get('emoji', '📦')} {case.get('name', 'Case')}\n"
                    f"**Entry:** {format_balance(price, mode)} *(case price)*\n"
                    f"**Opponent:** {opp_label}\n\n"
                    "Both sides open the same case. **Higher item value wins** the combined loot.\n"
                    "Tie → stake refunded.\n\n"
                    f"**Drops:** {len(items)} items in this case"
                ),
                "multiplier": "varies",
            }
            bet = price
        elif cases:
            info = {
                "name": "⚔️ Case Battle",
                "desc": (
                    f"**{len(cases)}** cases available.\n\n"
                    "Pick a case and opponent, then press **Start Battle**.\n"
                    "🤖 Bot battles play here in your room — **not** posted to the log channel."
                ),
                "multiplier": "varies",
            }
        else:
            info = {
                "name": "⚔️ Case Battle",
                "desc": "⚠️ No cases configured for battle. Contact an admin.",
                "multiplier": "—",
            }
    elif game == "case_opening":
        data      = _get_cases_data()
        all_cases = data.get("cases", {})
        case_id   = session.get("case_opening_case_id")
        case      = all_cases.get(case_id) if case_id else None
        view_mode = session.get("case_opening_view_mode", "house")

        official_count  = sum(1 for c in all_cases.values() if not c.get("is_community"))
        community_count = sum(1 for c in all_cases.values() if c.get("is_community"))
        favs_count      = len(_get_user_favorites(user.id))
        mode_tag        = "🏠 House Cases" if view_mode == "house" else "🌐 Community Cases"

        if case:
            price = case.get("price", 0)
            items = case.get("items", [])
            count = int(session.get("case_opening_count", 1))
            is_community = case.get("is_community", False)

            # Calculate item drop chances
            sorted_items = sorted(items, key=lambda i: i.get("value", 0))
            weights = [1.0 / max(item.get("value", 1), 1) for item in sorted_items]
            total_w = sum(weights) or 1.0
            item_lines = []
            for item, w in zip(sorted_items[:15], weights[:15]):
                prob = (w / total_w) * 100.0
                item_lines.append(
                    f"{item.get('emoji', '❓')} **{item.get('name', '?')}** — "
                    f"{format_balance(item.get('value', 0), mode)} "
                    f"({prob:.2f}%)"
                )
            items_text = "\n".join(item_lines) if item_lines else "No items."
            type_tag = "🌐 Community" if is_community else "🏠 House"
            fee_line = "💸 **Fee:** 2.5% to creator\n" if is_community else ""
            info = {
                "name": f"{case.get('emoji', '📦')} {case.get('name', 'Case Opening')}",
                "desc": (
                    f"🏷️ **Type:** {type_tag}\n"
                    f"💰 **Price:** {format_balance(price, mode)}\n"
                    f"{fee_line}"
                    f"\n**📦 Contents:**\n{items_text}\n\n"
                    f"🏠 **{official_count}** house  ·  🌐 **{community_count}** community  ·  ⭐ **{favs_count}** favourited\n"
                    f"🎲 **Quantity:** ×{count}  *(use the dropdown below to change)*"
                ),
                "multiplier": "varies",
            }
        elif all_cases:
            info = {
                "name": "📦 Case Opening",
                "desc": (
                    f"📂 **Viewing:** {mode_tag}\n"
                    f"🏠 **{official_count}** house cases  ·  🌐 **{community_count}** community cases\n"
                    f"⭐ **{favs_count}** favourited\n\n"
                    "Select a case from the dropdown below.\n"
                    "Use the toggle button to switch between house and community cases."
                ),
                "multiplier": "varies",
            }
        else:
            info = {
                "name": "📦 Case Opening",
                "desc": "⚠️ No cases available yet. Contact an admin.",
                "multiplier": "—",
            }
    else:
        game_info = {
            "roulette": {
                "name": "🎰 Roulette",
                "desc": t("games.game_descriptions.roulette", user_id=user_lang_id),
                "multiplier": "2x"
            },
            "dice": {
                "name": "🎲 Dice",
                "desc": t("games.game_descriptions.dice", user_id=user_lang_id),
                "multiplier": "2x"
            },
            "coinflip": {
                "name": "🪙 Coin Flip",
                "desc": t("games.game_descriptions.coinflip", user_id=user_lang_id),
                "multiplier": "2x"
            },
            "slot": {
                "name": "🎰 Slot Machine",
                "desc": (
                    "**3×5 grid · 30 paylines** *(line bet = bet ÷ 30)*\n"
                    "🍒 Cherry `10x/25x/50x`  🍋 Lemon `15x/36x/72x`\n"
                    "🍊 Orange `25x/60x/125x`  🍇 Grapes `40x/100x/200x`\n"
                    "🔔 Bell `60x/175x/350x`  ⭐ Star `100x/300x/600x`\n"
                    "💎 Diamond `200x/600x/1250x`  7️⃣ Seven `250x/750x/2500x`"
                ),
                "multiplier": "up to 2500x per line"
            },
        }
        info = game_info.get(
            game,
            {
                "name": t("games.game", user_id=user_lang_id),
                "desc": t("games.select_game", user_id=user_lang_id),
                "multiplier": "?",
            },
        )
        game_info = {
            "roulette": {
                "name": "🎰 Roulette",
                "desc": t("games.game_descriptions.roulette", user_id=user_lang_id),
                "multiplier": "2x"
            },
            "dice": {
                "name": "🎲 Dice",
                "desc": t("games.game_descriptions.dice", user_id=user_lang_id),
                "multiplier": "2x"
            },
            "coinflip": {
                "name": "🪙 Coin Flip",
                "desc": t("games.game_descriptions.coinflip", user_id=user_lang_id),
                "multiplier": "2x"
            },
            "slot": {
                "name": "🎰 Slot Machine",
                "desc": (
                    "**3×5 grid · 30 paylines** *(line bet = bet ÷ 30)*\n"
                    "🍒 Cherry `10x/25x/50x`  🍋 Lemon `15x/36x/72x`\n"
                    "🍊 Orange `25x/60x/125x`  🍇 Grapes `40x/100x/200x`\n"
                    "🔔 Bell `60x/175x/350x`  ⭐ Star `100x/300x/600x`\n"
                    "💎 Diamond `200x/600x/1250x`  7️⃣ Seven `250x/750x/2500x`"
                ),
                "multiplier": "up to 2500x per line"
            },
        }
        info = game_info.get(
            game,
            {
                "name": t("games.game", user_id=user_lang_id),
                "desc": t("games.select_game", user_id=user_lang_id),
                "multiplier": "?",
            },
        )
    mode_display = t("games.mode_demo", user_id=user_lang_id) if mode == "demo" else t("games.mode_real", user_id=user_lang_id)

    # ── Free-round promo check ────────────────────────────────────────────────
    active_promo = promo_engine.get_active_promo(user.id)
    _free_round_block = None
    if (
        active_promo
        and active_promo.get("status") == "active"
        and active_promo.get("type") == "freegame"
        and active_promo.get("game", "").lower() == game.lower()
    ):
        _pr_total   = int(active_promo.get("rounds_total", 0))
        _pr_played  = int(active_promo.get("rounds_played", 0))
        _pr_left    = _pr_total - _pr_played
        _pr_won     = int(active_promo.get("total_winnings", 0))
        _pr_bet     = int(active_promo.get("bet_amount", 0))
        _bar_filled = round(_pr_played / _pr_total * 10) if _pr_total > 0 else 0
        _bar        = "🟣" * _bar_filled + "⬛" * (10 - _bar_filled)
        _free_round_block = (
            f"╔══ 🎟️ **FREE ROUND ACTIVE** ══╗\n"
            f"  🎲 **Bet / Round:** {format_balance(_pr_bet, 'real')}\n"
            f"  📊 **Progress:** {_bar}  **{_pr_played}/{_pr_total}**\n"
            f"  🔁 **Remaining:** {_pr_left} round{'s' if _pr_left != 1 else ''}\n"
            f"  💰 **Total Won:** {format_balance(_pr_won, 'real')}\n"
            f"╚══════════════════════════════╝"
        )

    if _free_round_block:
        description = (
            f"**📖 {t('games.game_info', user_id=user_lang_id)}**\n{info['desc']}\n\n"
            f"{_free_round_block}\n\n"
            f"**💵 {t('games.your_balance', user_id=user_lang_id)}:** {format_balance(balance, mode)}\n"
            f"**🎯 {t('games.win_multiplier', user_id=user_lang_id)}:** {info['multiplier']}\n"
            f"**🎮 {t('games.mode', user_id=user_lang_id)}:** {mode_display}"
        )
    else:
        description = (
            f"**📖 {t('games.game_info', user_id=user_lang_id)}**\n{info['desc']}\n\n"
            f"**💰 {t('games.current_bet', user_id=user_lang_id)}:** {format_balance(bet, mode)}\n"
            f"**💵 {t('games.your_balance', user_id=user_lang_id)}:** {format_balance(balance, mode)}\n"
            f"**🎯 {t('games.win_multiplier', user_id=user_lang_id)}:** {info['multiplier']}\n"
            f"**🎮 {t('games.mode', user_id=user_lang_id)}:** {mode_display}"
        )
    
    # Son oyun bilgisini ekle
    last_game = session.get("last_game")
    if last_game:
        result_emoji = {"win": "🎉", "lose": "😢", "tie": "🤝"}
        emoji = result_emoji.get(last_game["result"], "🎮")
        game_name = last_game.get("game", t("games.unknown", user_id=user_lang_id))
        description += f"\n\n**{emoji} {t('games.last_game', user_id=user_lang_id)} - {game_name}**\n"
        description += f"{t('games.result', user_id=user_lang_id)}: {last_game['result'].upper()} ({last_game.get('multiplier', '0')}x)\n"
        if last_game['result'] == 'win':
            description += f"{t('games.won', user_id=user_lang_id)}: {format_balance(last_game['amount'], mode)}"
        elif last_game['result'] == 'lose':
            description += f"{t('games.lost', user_id=user_lang_id)}: {format_balance(last_game['amount'], mode)}"
        else:
            description += f"{t('games.returned', user_id=user_lang_id)}: {format_balance(last_game['amount'], mode)}"
    
    embed = discord.Embed(
        title=info["name"],
        description=description,
        color=discord.Color.green()
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=t("games.footer", user_id=user_lang_id))
    
    return embed


class Games(commands.Cog):
    """Oyun sistemi"""
    
    def __init__(self, bot):
        self.bot = bot
        self.cleanup_sessions.start()
    
    def cog_unload(self):
        """Cog kaldırıldığında task'ı durdur"""
        self.cleanup_sessions.cancel()
    
    @tasks.loop(seconds=30)
    async def cleanup_sessions(self):
        """Süresi dolmuş session'ları temizle (30 saniyede bir)"""
        sessions = get_data("server/game_sessions")
        expired_sessions = []
        
        for message_id, session in sessions.items():
            # 60 saniye timeout kontrolü
            if not GameSession.is_session_active(message_id, timeout=60):
                expired_sessions.append((message_id, session))
        
        # Süresi dolmuş session'ları temizle
        for message_id, session in expired_sessions:
            await self.cleanup_expired_session(message_id, session)
    
    @cleanup_sessions.before_loop
    async def before_cleanup_sessions(self):
        """Bot hazır olana kadar bekle"""
        await self.bot.wait_until_ready()
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Bot hazır olduğunda persistent view'ları kaydet"""
        print("✅ Games cog loaded - Persistent views ready")
    
    async def cleanup_expired_session(self, message_id: str, session: dict):
        """Süresi dolmuş session'ı temizle ve mesajı güncelle"""
        try:
            # Channel ve message bul
            channel = self.bot.get_channel(session["channel_id"])
            if channel:
                try:
                    message = await channel.fetch_message(int(message_id))
                    
                    # Session sahibini bul
                    owner = await self.bot.fetch_user(session["owner"])
                    
                    # Timeout mesajı
                    owner_lang_id = str(session.get("owner", 0))
                    embed = discord.Embed(
                        title=t("games.errors.game_session_expired_title", user_id=owner_lang_id),
                        description=t(
                            "games.errors.game_session_expired_desc",
                            user_id=owner_lang_id,
                            owner=owner.mention,
                        ),
                        color=discord.Color.orange(),
                        timestamp=discord.utils.utcnow()
                    )
                    embed.set_footer(text=t("games.footer", user_id=owner_lang_id))
                    
                    # Mesajı güncelle — V2 timeout panel (no controls)
                    await message.edit(
                        embed=None,
                        content=None,
                        view=play_layout(embed, [], timeout=None),
                    )
                    
                    # 15 saniye sonra mesajı sil
                    await asyncio.sleep(15)
                    try:
                        await message.delete()
                    except:
                        pass  # Mesaj zaten silinmiş olabilir
                except:
                    pass  # Mesaj silinmiş veya erişilemez
            
            # Session'ı sil
            GameSession.delete_session(message_id)
            # Orphaned mines/towers/hilo state temizle
            _active_mines.pop(message_id, None)
            _active_towers.pop(message_id, None)
            _active_hilo.pop(message_id, None)
        except Exception as e:
            print(f"Error cleaning up session {message_id}: {e}")
    
    @commands.Cog.listener()
    async def on_message(self, message):
        """Mesaj gönderildiğinde session aktivitesini güncelle"""
        # Bot mesajlarını atla
        if message.author.bot:
            return
        
        # Session'ı bul
        sessions = get_data("server/game_sessions")
        for message_id, session in sessions.items():
            if session["channel_id"] == message.channel.id and session["owner"] == message.author.id:
                # Bu kullanıcının bu kanaldaki session'ını güncelle
                GameSession.touch_session(message_id)


async def setup(bot):
    """Cog yükleme fonksiyonu"""
    await bot.add_cog(Games(bot))

