"""
Promo Code Engine — Free-bet & balance reward system for Vegas Bot.

Promo types:
  balance   → instantly credit a fixed coin amount to user balance; 1× wager req applied
  freegame  → N rounds of a specified game at a fixed bet; winnings credited after all rounds
              with 1× wager requirement

Data layout:
  server/promo_codes           → all promo templates  {CODE_STRING: {...}}
  users/{id}/active_promo      → single active promo state for a user (or {})
"""
import os
import time
from modules.database import get_data, set_data, replace_data, get_user_data, set_user_data


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_promo_codes() -> dict:
    """Return all promo code templates."""
    data = get_data("server/promo_codes") or {}
    return data if isinstance(data, dict) else {}


def save_promo_codes(codes: dict):
    replace_data("server/promo_codes", codes)


def get_promo_code(code: str) -> dict | None:
    """Return a single promo code template, or None if not found."""
    return get_promo_codes().get(code.upper().strip())


def get_active_promo(user_id) -> dict:
    """Return the user's active promo state, or {} if none."""
    data = get_user_data(int(user_id), "active_promo") or {}
    if not isinstance(data, dict):
        return {}
    if data.get("status") not in ("active", "wagering"):
        return {}
    return data


def save_active_promo(user_id, data: dict):
    set_user_data(int(user_id), "active_promo", data)


def clear_active_promo(user_id):
    set_user_data(int(user_id), "active_promo", {"status": "none"})


def has_active_promo(user_id) -> bool:
    return bool(get_active_promo(user_id))


# ── Code management (admin) ────────────────────────────────────────────────────

def create_promo_code(
    *,
    code: str,
    promo_type: str,           # "balance" | "freegame"
    reward_amount: int = 0,    # balance type
    game: str = "",            # freegame type
    bet_amount: int = 0,       # freegame type — coins per round
    rounds: int = 0,           # freegame type — number of rounds
    wager_multiplier: float = 1.0,
    max_uses: int = 0,         # 0 = unlimited
    expires_at: int | None = None,
    expire_hours: int = 0,     # stored for reactivation
    description: str = "",
    created_by: str = "",
    # ── Requirements ────────────────────────────────────
    req_min_level: int = 0,           # 0 = no level requirement
    req_min_wagered: int = 0,         # 0 = no wagered requirement (matches rakeback tier threshold)
    min_balance_forfeit: int = 0,     # 0 = forfeit when balance hits 0; >0 = forfeit below threshold
) -> tuple[bool, str]:
    """
    Create a new promo code. Returns (success, error_message).
    """
    code = code.upper().strip()
    if not code:
        return False, "Code cannot be empty."
    if not code.replace("-", "").replace("_", "").isalnum():
        return False, "Code must be alphanumeric (letters, digits, hyphens, underscores)."

    codes = get_promo_codes()
    if code in codes:
        return False, f"Code `{code}` already exists."

    if promo_type not in ("balance", "freegame"):
        return False, "Type must be 'balance' or 'freegame'."

    if promo_type == "balance" and reward_amount <= 0:
        return False, "Reward amount must be > 0 for balance type."
    if promo_type == "freegame" and (bet_amount <= 0 or rounds <= 0 or not game):
        return False, "Freegame type requires game, bet_amount > 0, and rounds > 0."

    codes[code] = {
        "code": code,
        "type": promo_type,
        # balance-type fields
        "reward_amount": int(reward_amount),
        # freegame-type fields
        "game": game.lower(),
        "bet_amount": int(bet_amount),
        "rounds": int(rounds),
        # common
        "wager_multiplier": float(wager_multiplier),
        "max_uses": int(max_uses),
        "used_by": [],
        "enabled": True,
        "expires_at": expires_at,
        "expire_hours": int(expire_hours),   # stored for reactivation
        "description": description,
        "created_at": int(time.time()),
        "created_by": str(created_by),
        # requirements
        "req_min_level": int(req_min_level),
        "req_min_wagered": int(req_min_wagered),
        "min_balance_forfeit": int(min_balance_forfeit),
    }
    save_promo_codes(codes)
    return True, ""


def toggle_promo_code(code: str) -> bool | None:
    """Toggle enabled state. Returns new state or None if not found."""
    codes = get_promo_codes()
    code = code.upper()
    if code not in codes:
        return None
    codes[code]["enabled"] = not codes[code].get("enabled", True)
    save_promo_codes(codes)
    return codes[code]["enabled"]


def delete_promo_code(code: str) -> bool:
    codes = get_promo_codes()
    code = code.upper()
    if code not in codes:
        return False
    del codes[code]
    save_promo_codes(codes)
    return True


def update_promo_code(code: str, **fields) -> tuple[bool, str]:
    """
    Update fields of an existing promo code.
    Allowed fields: description, max_uses, wager_multiplier, reward_amount,
    rounds, bet_amount, expires_at, expire_hours, req_min_level, req_min_wagered.
    Returns (success, error_message).
    """
    codes = get_promo_codes()
    code = code.upper()
    if code not in codes:
        return False, "Code not found."

    allowed = {
        "description", "max_uses", "wager_multiplier", "reward_amount",
        "rounds", "bet_amount", "expires_at", "expire_hours",
        "req_min_level", "req_min_wagered",
    }
    for k, v in fields.items():
        if k in allowed:
            codes[code][k] = v
    save_promo_codes(codes)
    return True, ""


def reactivate_promo_code(code: str) -> tuple[bool, str]:
    """
    Re-enable a disabled/expired promo code.
    If expire_hours > 0, sets a fresh expires_at from now.
    Returns (success, error_message).
    """
    codes = get_promo_codes()
    code = code.upper()
    if code not in codes:
        return False, "Code not found."

    tmpl = codes[code]
    expire_hours = int(tmpl.get("expire_hours", 0))
    if expire_hours > 0:
        codes[code]["expires_at"] = int(time.time()) + expire_hours * 3600
    else:
        codes[code]["expires_at"] = None
    codes[code]["enabled"] = True
    save_promo_codes(codes)
    return True, ""


def auto_close_expired_promos() -> list[str]:
    """
    Scan all promo codes; disable any that have passed their expires_at.
    Returns list of codes that were just closed.
    """
    codes = get_promo_codes()
    now = int(time.time())
    closed = []
    changed = False
    for code, tmpl in codes.items():
        if not tmpl.get("enabled", True):
            continue
        exp = tmpl.get("expires_at")
        if exp and now > exp:
            codes[code]["enabled"] = False
            closed.append(code)
            changed = True
    if changed:
        save_promo_codes(codes)
    return closed


# ── Lifecycle ──────────────────────────────────────────────────────────────────

def redeem_promo_code(user_id, code: str) -> tuple[bool, str, dict]:
    """
    Validate and activate a promo code for a user.
    Returns (success, error_message, promo_template).
    Caller must credit balance for 'balance' type promos.
    """
    code = code.upper().strip()
    codes = get_promo_codes()
    template = codes.get(code)

    if not template:
        return False, "Invalid promo code.", {}
    if not template.get("enabled", True):
        return False, "This promo code is no longer active.", {}

    now = int(time.time())
    expires_at = template.get("expires_at")
    if expires_at and now > expires_at:
        return False, "This promo code has expired.", {}

    max_uses = int(template.get("max_uses", 0))
    used_by = template.get("used_by", [])
    if max_uses > 0 and len(used_by) >= max_uses:
        return False, "This promo code has reached its usage limit.", {}

    user_id_str = str(user_id)
    if user_id_str in used_by:
        return False, "You have already used this promo code.", {}

    # ── Requirement checks ────────────────────────────────────────────────
    req_min_level = int(template.get("req_min_level", 0))
    req_min_wagered = int(template.get("req_min_wagered", 0))
    if req_min_level > 0 or req_min_wagered > 0:
        user_level_data = get_user_data(int(user_id), "level") or {}
        user_level = int(user_level_data.get("level", 1))
        if req_min_level > 0 and user_level < req_min_level:
            return False, f"You need to be at least **Level {req_min_level}** to use this promo code.", {}
        if req_min_wagered > 0:
            user_stats = get_user_data(int(user_id), "stats") or {}
            total_wagered = int(user_stats.get("total_wagered", 0))
            if total_wagered < req_min_wagered:
                from modules.utils import format_balance
                return False, f"You need to wager at least **{format_balance(req_min_wagered, 'real')}** total to use this promo code.", {}

    if has_active_promo(user_id):
        return False, "You already have an active promo. Complete it before redeeming another.", {}

    # Mark code as used
    codes[code]["used_by"] = used_by + [user_id_str]
    save_promo_codes(codes)

    # Build active promo state
    promo_type = template.get("type", "balance")
    wager_mult = float(template.get("wager_multiplier", 1.0))

    min_forfeit = int(template.get("min_balance_forfeit", 0))

    if promo_type == "balance":
        reward = int(template.get("reward_amount", 0))
        active_state = {
            "code": code,
            "type": "balance",
            "reward_amount": reward,
            "wager_requirement": int(reward * wager_mult),
            "wagered_so_far": 0,
            "min_balance_forfeit": min_forfeit,
            "status": "wagering",   # immediately in wagering state after balance credit
            "activated_at": now,
        }
    else:  # freegame
        active_state = {
            "code": code,
            "type": "freegame",
            "game": template.get("game", ""),
            "bet_amount": int(template.get("bet_amount", 0)),
            "rounds_total": int(template.get("rounds", 0)),
            "rounds_played": 0,
            "total_winnings": 0,
            "wager_requirement": 0,   # set after all rounds
            "wagered_so_far": 0,
            "wager_multiplier": wager_mult,
            "min_balance_forfeit": min_forfeit,
            "status": "active",       # rounds not yet exhausted
            "activated_at": now,
        }

    save_active_promo(user_id, active_state)

    # Log to per-user transaction history
    try:
        from modules.player import Player as _Player
        _p = _Player(user_id)
        if promo_type == "balance":
            _p._write_transaction(
                ttype="promo",
                amount=int(template.get("reward_amount", 0)),
                reason=f"Promo code: {code}",
                by="system",
            )
        else:
            _p._write_transaction(
                ttype="promo",
                amount=0,
                reason=f"Free-game promo: {code} ({template.get('rounds', 0)}x {template.get('game', '')})",
                by="system",
            )
    except Exception:
        pass

    return True, "", template


def on_freeround_result(user_id, winnings: int) -> tuple[int, bool]:
    """
    Called after each free-game round completes.
    `winnings` = payout for that round (0 if lost).
    Returns (rounds_remaining, all_rounds_complete).
    """
    data = get_active_promo(user_id)
    if not data or data.get("type") != "freegame" or data.get("status") != "active":
        return 0, False

    data["rounds_played"] = int(data.get("rounds_played", 0)) + 1
    data["total_winnings"] = int(data.get("total_winnings", 0)) + int(winnings)

    rounds_total = int(data.get("rounds_total", 0))
    rounds_played = data["rounds_played"]
    remaining = rounds_total - rounds_played

    if remaining <= 0:
        # All rounds done — set wagering requirement and credit balance
        wager_mult = float(data.get("wager_multiplier", 1.0))
        total_won = int(data["total_winnings"])
        wager_req = int(total_won * wager_mult)
        data["status"] = "wagering"
        data["wager_requirement"] = wager_req
        data["wagered_so_far"] = 0

    save_active_promo(user_id, data)
    return max(remaining, 0), (remaining <= 0)


def complete_freegame_promo(user_id) -> int:
    """
    Credit total free-game winnings to user's real balance and return the amount.
    The caller is responsible for calling this AFTER on_freeround_result returns all_done=True.
    """
    from modules.player import Player
    data = get_user_data(int(user_id), "active_promo") or {}
    total_won = int(data.get("total_winnings", 0))
    if total_won > 0:
        player = Player(user_id)
        player.add_balance("real", total_won)
    return total_won


def on_real_bet_wagered(user_id, bet_amount: int) -> bool:
    """
    Called after every real-money bet (from base_game handle_result).
    Accumulates wagered amount toward promo wager requirement.
    Returns True when the promo wager requirement is fully met (promo completed).
    """
    data = get_active_promo(user_id)
    if not data or data.get("status") != "wagering":
        return False

    required = int(data.get("wager_requirement", 0))
    if required == 0:
        # No wager requirement — mark completed immediately
        data["status"] = "completed"
        save_active_promo(user_id, data)
        clear_active_promo(user_id)
        return True

    data["wagered_so_far"] = int(data.get("wagered_so_far", 0)) + int(bet_amount)
    if data["wagered_so_far"] >= required:
        data["status"] = "completed"
        save_active_promo(user_id, data)
        clear_active_promo(user_id)
        return True

    save_active_promo(user_id, data)
    return False


def check_forfeit_promo(user_id, current_balance: int) -> bool:
    """
    Check if balance dropped to/below the forfeit threshold.
    If min_balance_forfeit == 0, forfeits when balance reaches 0.
    If min_balance_forfeit > 0, forfeits when balance <= threshold.
    Returns True if promo was just forfeited.
    """
    data = get_active_promo(user_id)
    if not data or data.get("status") != "wagering":
        return False

    threshold = int(data.get("min_balance_forfeit", 0))
    if threshold > 0:
        should_forfeit = current_balance <= threshold
    else:
        should_forfeit = current_balance <= 0

    if should_forfeit:
        data["status"] = "forfeited"
        data["forfeited_at"] = int(time.time())
        save_active_promo(user_id, data)
        return True
    return False


def get_promo_wager_info(user_id) -> dict:
    """Return wagering progress info for the active promo, or {}."""
    data = get_active_promo(user_id)
    if not data:
        return {}
    req = int(data.get("wager_requirement", 0))
    done = int(data.get("wagered_so_far", 0))
    return {
        "code": data.get("code", ""),
        "type": data.get("type", ""),
        "status": data.get("status", ""),
        "wager_requirement": req,
        "wagered_so_far": done,
        "remaining": max(req - done, 0),
        "pct": int(done / req * 100) if req > 0 else 100,
    }
