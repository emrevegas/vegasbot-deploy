"""Crypto Withdrawal Cog — user-initiated withdrawals with admin approval.

Flow:
  1. User runs /withdraw → selects coin (SOL / LTC)
  2. If no saved addresses → modal to enter one (auto-saved)
     If saved addresses   → embed + select menu + "Add New" button
  3. User enters USD amount
  4. Balance deducted immediately (held)
  5. Withdraw log channel receives Approve / Reject embed
  6. Admin Approves → bot sends from treasury HD wallet → DMs user TX hash
  7. Background task polls on-chain confirmation → DMs user when confirmed
  8. Admin Rejects → balance refunded → DMs user
"""

import uuid
import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from modules.database import get_data, replace_data, get_user_data, set_user_data, check_permission, get_server_data
from modules.player import Player
from modules.utils import format_balance
import modules.crypto_deposit as engine
import modules.bonus as bonus_engine
from modules.translator import t

WITHDRAW_KEY    = "server/crypto_withdrawals"
PENDING_TX_KEY  = "server/crypto_pending_withdrawals"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_emojis() -> tuple[str, str]:
    s = engine.get_settings()
    return s.get("sol_emoji", "🟣"), s.get("ltc_emoji", "🔘")


def _get_withdrawals() -> dict:
    return get_data(WITHDRAW_KEY) or {}


def _save_withdrawals(d: dict) -> None:
    replace_data(WITHDRAW_KEY, d)


def _get_pending_txs() -> dict:
    return get_data(PENDING_TX_KEY) or {}


def _save_pending_txs(d: dict) -> None:
    replace_data(PENDING_TX_KEY, d)


def _get_user_addresses(user_id: int) -> dict:
    return get_user_data(user_id, "crypto_addresses") or {"sol": [], "ltc": []}


def _save_user_addresses(user_id: int, addrs: dict) -> None:
    set_user_data(user_id, "crypto_addresses", addrs)


def _build_approval_embed(w: dict) -> discord.Embed:
    sol_emoji, ltc_emoji = _get_emojis()
    chain = w["chain"]
    emoji = sol_emoji if chain == "SOL" else ltc_emoji
    embed = discord.Embed(
        title=f"💸  Withdrawal Request — {emoji} {chain}",
        color=0xFFA500,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="👤  User",     value=f"<@{w['user_id']}>  (`{w['user_id']}`)", inline=True)
    embed.add_field(name="💰  Amount",   value=f"`{w['amount_crypto']} {chain}`  (~${w['amount_usd']:.2f})", inline=True)
    embed.add_field(name="🎰  Deducted", value=f"`{format_balance(w['amount_coins'], 'real')}`", inline=True)
    embed.add_field(name="📍  Destination", value=f"```{w['address']}```", inline=False)
    embed.add_field(name="🆔  ID", value=f"`{w['id']}`", inline=True)
    embed.set_footer(text="Vegas Casino  ·  Approve or Reject below")
    return embed


# ── Modals ────────────────────────────────────────────────────────────────────

async def _process_withdrawal(interaction: discord.Interaction, chain: str, address: str, user_id: int, usd_raw: str) -> None:
    """Shared logic: validate amount, wager gate, deduct balance, create withdrawal record, post to log channel."""
    try:
        usd = float(usd_raw.replace(",", "."))
        if usd <= 0:
            raise ValueError
    except ValueError:
        return await interaction.response.send_message(
            "❌ Enter a valid positive USD amount.", ephemeral=True
        )

    s       = engine.get_settings()
    min_usd = float(s.get("min_deposit_usd", 1.0))
    if usd < min_usd:
        return await interaction.response.send_message(
            f"❌ Minimum withdrawal is **${min_usd:.2f} USD**.", ephemeral=True
        )

    rates = engine.get_rates()
    price = rates.get("sol_usd" if chain == "SOL" else "ltc_usd", 0)
    if price <= 0:
        return await interaction.response.send_message(
            "❌ Could not fetch exchange rate. Try again.", ephemeral=True
        )

    exch     = get_data("server/exchange_rates") or {}
    coin_usd = float(exch.get("coin_usd_rate", 0))
    if coin_usd <= 0:
        return await interaction.response.send_message(
            "❌ Server exchange rate not configured.", ephemeral=True
        )

    coins_needed  = int(usd / coin_usd)
    amount_crypto = round(usd / price, 8 if chain == "LTC" else 6)

    p   = Player(user_id)
    bal = p.get_balance("real")

    # ── Min withdrawal in coins (server setting) ──────────────────────────
    guild_id    = str(interaction.guild_id) if interaction.guild_id else ""
    server_data = get_server_data(guild_id) if guild_id else {}
    min_coins   = int(server_data.get("min_withdrawal", 0) or 0)
    if min_coins > 0 and coins_needed < min_coins:
        return await interaction.response.send_message(
            f"❌ Minimum withdrawal is **{format_balance(min_coins, 'real')}** (~${min_coins * coin_usd:.2f} USD).",
            ephemeral=True,
        )

    if bal < coins_needed:
        return await interaction.response.send_message(
            f"❌ Insufficient balance.\n"
            f"You have `{format_balance(bal, 'real')}` but need `{format_balance(coins_needed, 'real')}`.",
            ephemeral=True,
        )

    # ── Wager gate ────────────────────────────────────────────────────────
    stats = get_user_data(user_id, "stats") or {}
    multiplier = float(server_data.get("withdraw_min_multiplier", 0) or 0)
    if multiplier > 0:
        last_deposit = int(stats.get("last_deposit_amount", 0))
        if last_deposit > 0:
            required_wager = int(last_deposit * multiplier)
            total_wagered  = int(stats.get("total_wagered", 0))
            wagered_at_dep = int(stats.get("wagered_at_last_deposit", 0))
            wagered_since  = max(0, total_wagered - wagered_at_dep)

            # Add bonus wager requirement if active
            active_bonus = bonus_engine.get_active_bonus(str(user_id))
            if active_bonus:
                bonus_req = int(active_bonus.get("wager_requirement", 0))
                required_wager += bonus_req

            wager_remaining = max(0, required_wager - wagered_since)
            if wager_remaining > 0:
                wg_pct = int(wagered_since / required_wager * 100) if required_wager else 0
                return await interaction.response.send_message(
                    embed=discord.Embed(
                        title="🎲 Wager Requirement Not Met",
                        description=(
                            f"You must wager **{format_balance(required_wager, 'real')}** before withdrawing.\n\n"
                            f"Progress: **{format_balance(wagered_since, 'real')}** / **{format_balance(required_wager, 'real')}** ({wg_pct}%)\n"
                            f"Still needed: **{format_balance(wager_remaining, 'real')}**"
                        ),
                        color=0xf5a623,
                    ).set_footer(text="Vegas Casino | Withdrawal System"),
                    ephemeral=True,
                )

    # ── Bonus block (percentage type) ─────────────────────────────────────
    active_bonus = bonus_engine.get_active_bonus(str(user_id))
    if active_bonus:
        _lang = get_user_data(user_id, "lang") or {}
        _lang = _lang.get("language", "en") if isinstance(_lang, dict) else "en"
        btype = active_bonus.get("type", "fixed")
        if btype == "percentage":
            req  = int(active_bonus.get("wager_requirement", 0))
            done = int(active_bonus.get("wagered_so_far", 0))
            if done < req:
                remaining = req - done
                pct = int(done / req * 100) if req else 0
                return await interaction.response.send_message(
                    embed=discord.Embed(
                        title=t("bonus.wager_not_met_title", lang=_lang),
                        description=t("bonus.wager_not_met_desc", lang=_lang,
                            bonus_name=active_bonus["bonus_name"],
                            done=format_balance(done, "real"),
                            req=format_balance(req, "real"),
                            pct=pct,
                            remaining=format_balance(remaining, "real")),
                        color=0xf5a623,
                    ).set_footer(text=t("bonus.wager_not_met_footer", lang=_lang)),
                    ephemeral=True,
                )

    # Deduct balance immediately (held) + stats
    p.remove_balance("real", coins_needed)
    p.record_withdraw(coins_needed)
    bonus_engine.complete_bonus_on_withdraw(str(user_id))

    # Create withdrawal record
    wid = str(uuid.uuid4())[:8].upper()
    w = {
        "id":             wid,
        "user_id":        user_id,
        "chain":          chain,
        "address":        address,
        "amount_usd":     round(usd, 2),
        "amount_crypto":  amount_crypto,
        "amount_coins":   coins_needed,
        "status":         "pending",
        "tx_id":          None,
        "created_at":     int(time.time()),
        "log_channel_id": None,
        "log_message_id": None,
    }
    withdrawals      = _get_withdrawals()
    withdrawals[wid] = w
    _save_withdrawals(withdrawals)

    sol_emoji, ltc_emoji = _get_emojis()
    emoji = sol_emoji if chain == "SOL" else ltc_emoji

    await interaction.response.send_message(
        embed=discord.Embed(
            title="⏳  Withdrawal Submitted",
            description=(
                f"{emoji} **{amount_crypto} {chain}** (~${usd:.2f})\n"
                f"📍 To: `{address}`\n\n"
                f"Your balance has been deducted. An admin will review your request.\n"
                f"🆔 Withdrawal ID: `{wid}`"
            ),
            color=discord.Color.orange(),
        ),
        ephemeral=True,
    )

    # Post approval embed to log channel
    log_ch_id = s.get("withdraw_log_channel_id") or s.get("sweep_log_channel_id")
    if log_ch_id:
        channel = interaction.client.get_channel(int(log_ch_id))
        if channel:
            view = WithdrawApprovalView(wid)
            msg  = await channel.send(embed=_build_approval_embed(w), view=view)
            withdrawals = _get_withdrawals()
            if wid in withdrawals:
                withdrawals[wid]["log_channel_id"] = channel.id
                withdrawals[wid]["log_message_id"] = msg.id
                _save_withdrawals(withdrawals)


class WithdrawAmountModal(discord.ui.Modal):
    def __init__(self, chain: str, address: str, user_id: int):
        super().__init__(title=f"Withdraw {chain} — Enter Amount", timeout=300)
        self.chain   = chain
        self.address = address
        self.user_id = user_id
        self.amount_input = discord.ui.TextInput(
            label="Amount in USD",
            placeholder="e.g.  10.00",
            required=True,
            max_length=12,
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        await _process_withdrawal(interaction, self.chain, self.address, self.user_id, self.amount_input.value)


class NewAddressModal(discord.ui.Modal):
    """Collects address + amount in one modal (Discord forbids modal-in-modal)."""
    def __init__(self, chain: str, user_id: int):
        super().__init__(title=f"Withdraw {chain} — Address & Amount", timeout=300)
        self.chain   = chain
        self.user_id = user_id
        self.addr_input = discord.ui.TextInput(
            label=f"Your {chain} withdrawal address",
            placeholder="Destination wallet address",
            required=True,
            max_length=120,
        )
        self.amount_input = discord.ui.TextInput(
            label="Amount in USD",
            placeholder="e.g.  10.00",
            required=True,
            max_length=12,
        )
        self.add_item(self.addr_input)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        addr = self.addr_input.value.strip()
        if not addr:
            return await interaction.response.send_message("❌ Address cannot be empty.", ephemeral=True)

        # Save address
        addrs     = _get_user_addresses(self.user_id)
        chain_key = self.chain.lower()
        if addr not in addrs.get(chain_key, []):
            addrs.setdefault(chain_key, []).append(addr)
            _save_user_addresses(self.user_id, addrs)

        await _process_withdrawal(interaction, self.chain, addr, self.user_id, self.amount_input.value)


# ── Views ─────────────────────────────────────────────────────────────────────

class WithdrawCoinView(discord.ui.View):
    """Step 1 — user picks coin."""
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id

        s = engine.get_settings()
        options: list[discord.SelectOption] = []
        if s.get("sol_enabled", True):
            options.append(discord.SelectOption(label="Solana (SOL)", value="SOL", emoji="🟣"))
        if s.get("ltc_enabled", True):
            options.append(discord.SelectOption(label="Litecoin (LTC)", value="LTC", emoji="🔘"))
        if not options:
            options.append(discord.SelectOption(label="No coins available", value="none"))

        sel           = discord.ui.Select(placeholder="Select coin to withdraw…", options=options, row=0)
        sel.callback  = self._select_coin
        self._select  = sel
        self.add_item(sel)

    async def _select_coin(self, interaction: discord.Interaction):
        chain = self._select.values[0]
        if chain == "none":
            return await interaction.response.send_message("❌ No crypto is enabled.", ephemeral=True)

        saved = _get_user_addresses(self.user_id).get(chain.lower(), [])
        if not saved:
            # No saved addresses → go straight to modal
            await interaction.response.send_modal(NewAddressModal(chain=chain, user_id=self.user_id))
        else:
            sol_emoji, ltc_emoji = _get_emojis()
            emoji = sol_emoji if chain == "SOL" else ltc_emoji
            embed = discord.Embed(
                title=f"{emoji}  Withdraw {chain} — Select Address",
                description="Choose a saved address or add a new one.",
                color=0x9945FF,
            )
            for addr in saved:
                embed.add_field(name="\u200b", value=f"`{addr}`", inline=False)
            await interaction.response.edit_message(
                embed=embed,
                view=WithdrawAddressView(chain=chain, user_id=self.user_id, saved=saved),
            )


class WithdrawAddressView(discord.ui.View):
    """Step 2 — user picks a saved address or adds a new one."""
    def __init__(self, chain: str, user_id: int, saved: list[str]):
        super().__init__(timeout=120)
        self.chain   = chain
        self.user_id = user_id

        options = [
            discord.SelectOption(
                label=(addr[:22] + "…" + addr[-6:]) if len(addr) > 30 else addr,
                value=addr,
            )
            for addr in saved[:25]
        ]
        sel           = discord.ui.Select(placeholder="Select a saved address…", options=options, row=0)
        sel.callback  = self._pick
        self._select  = sel
        self.add_item(sel)

        new_btn          = discord.ui.Button(label="➕ Add New Address", style=discord.ButtonStyle.secondary, row=1)
        new_btn.callback = self._new
        self.add_item(new_btn)

        back_btn          = discord.ui.Button(label="⬅️ Back", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self._back
        self.add_item(back_btn)

    async def _pick(self, interaction: discord.Interaction):
        addr = self._select.values[0]
        await interaction.response.send_modal(
            WithdrawAmountModal(chain=self.chain, address=addr, user_id=self.user_id)
        )

    async def _new(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NewAddressModal(chain=self.chain, user_id=self.user_id))

    async def _back(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="💸  Crypto Withdrawal",
            description="Select the cryptocurrency you want to withdraw.",
            color=0x9945FF,
        )
        await interaction.response.edit_message(embed=embed, view=WithdrawCoinView(self.user_id))


class WithdrawApprovalView(discord.ui.View):
    """Persistent view in the log channel — admin approves or rejects."""
    def __init__(self, withdrawal_id: str):
        super().__init__(timeout=None)
        self.withdrawal_id = withdrawal_id

        approve          = discord.ui.Button(
            label="✅ Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"withdraw_approve_{withdrawal_id}",
        )
        approve.callback = self._approve
        self.add_item(approve)

        reject           = discord.ui.Button(
            label="❌ Reject",
            style=discord.ButtonStyle.danger,
            custom_id=f"withdraw_reject_{withdrawal_id}",
        )
        reject.callback  = self._reject
        self.add_item(reject)

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    async def _approve(self, interaction: discord.Interaction):
        has_perm = not check_permission(interaction.user.id, "admin") or not check_permission(interaction.user.id, "cashier")
        if not has_perm:
            return await interaction.response.send_message("❌ No permission.", ephemeral=True)

        withdrawals = _get_withdrawals()
        w = withdrawals.get(self.withdrawal_id)
        if not w:
            return await interaction.response.send_message("❌ Withdrawal record not found.", ephemeral=True)
        if w["status"] != "pending":
            return await interaction.response.send_message(f"❌ Already **{w['status']}**.", ephemeral=True)

        await interaction.response.defer()

        s            = engine.get_settings()
        chain        = w["chain"]
        amount_crypto = w["amount_crypto"]

        sol_emoji, ltc_emoji = _get_emojis()
        emoji = sol_emoji if chain == "SOL" else ltc_emoji

        # Send on-chain from treasury wallet (TREASURY_MNEMONIC)
        tx_id = None
        error = None
        loop  = asyncio.get_event_loop()
        try:
            if chain == "SOL":
                amount_lamports = int(amount_crypto * 1e9)
                tx_id = await loop.run_in_executor(
                    None, engine.send_sol_from_treasury, w["address"], amount_lamports
                )
            elif chain == "LTC":
                amount_satoshis = int(amount_crypto * 1e8)
                tx_id = await loop.run_in_executor(
                    None, engine.send_ltc_from_treasury, w["address"], amount_satoshis
                )
        except Exception as e:
            error = str(e)

        if error or not tx_id:
            # Refund user on failure
            Player(w["user_id"]).add_balance("real", w["amount_coins"])
            withdrawals[self.withdrawal_id]["status"] = "failed"
            _save_withdrawals(withdrawals)

            embed = _build_approval_embed(w)
            embed.color = discord.Color.red()
            embed.add_field(
                name="❌  FAILED — Balance Refunded",
                value=f"`{error or 'No TX returned'}`",
                inline=False,
            )
            self._disable_all()
            await interaction.edit_original_response(embed=embed, view=self)
            return

        # Mark approved
        withdrawals[self.withdrawal_id]["status"]      = "approved"
        withdrawals[self.withdrawal_id]["tx_id"]       = tx_id
        withdrawals[self.withdrawal_id]["approved_by"] = interaction.user.id
        _save_withdrawals(withdrawals)

        # Queue for confirmation polling
        pending = _get_pending_txs()
        pending[self.withdrawal_id] = {
            "chain":   chain,
            "tx_id":   tx_id,
            "user_id": w["user_id"],
        }
        _save_pending_txs(pending)

        # Update log embed
        embed = _build_approval_embed(w)
        embed.color = discord.Color.green()
        embed.add_field(
            name="✅  APPROVED",
            value=f"By <@{interaction.user.id}>\n🔗 TX: `{tx_id}`",
            inline=False,
        )
        self._disable_all()
        await interaction.edit_original_response(embed=embed, view=self)

        # DM user
        rates = engine.get_rates()
        price = rates.get("sol_usd" if chain == "SOL" else "ltc_usd", 0)
        user  = interaction.client.get_user(int(w["user_id"]))
        if user:
            try:
                await user.send(embed=discord.Embed(
                    title=f"✅  Withdrawal Approved — {emoji} {chain}",
                    description=(
                        f"Your withdrawal of **{amount_crypto} {chain}** (~${w['amount_usd']:.2f}) has been approved!\n\n"
                        f"📍 To: `{w['address']}`\n"
                        f"🔗 TX: `{tx_id}`\n\n"
                        f"You'll receive another message once the transaction is confirmed on-chain."
                    ),
                    color=discord.Color.green(),
                ))
            except discord.Forbidden:
                pass

    async def _reject(self, interaction: discord.Interaction):
        has_perm = not check_permission(interaction.user.id, "admin") or not check_permission(interaction.user.id, "cashier")
        if not has_perm:
            return await interaction.response.send_message("❌ No permission.", ephemeral=True)

        withdrawals = _get_withdrawals()
        w = withdrawals.get(self.withdrawal_id)
        if not w:
            return await interaction.response.send_message("❌ Withdrawal record not found.", ephemeral=True)
        if w["status"] != "pending":
            return await interaction.response.send_message(f"❌ Already **{w['status']}**.", ephemeral=True)

        # Refund
        Player(w["user_id"]).add_balance("real", w["amount_coins"])
        withdrawals[self.withdrawal_id]["status"]      = "rejected"
        withdrawals[self.withdrawal_id]["rejected_by"] = interaction.user.id
        _save_withdrawals(withdrawals)

        sol_emoji, ltc_emoji = _get_emojis()
        emoji = sol_emoji if w["chain"] == "SOL" else ltc_emoji

        embed = _build_approval_embed(w)
        embed.color = discord.Color.red()
        embed.add_field(
            name="❌  REJECTED — Balance Refunded",
            value=f"By <@{interaction.user.id}>",
            inline=False,
        )
        self._disable_all()
        await interaction.response.edit_message(embed=embed, view=self)

        # DM user
        user = interaction.client.get_user(int(w["user_id"]))
        if user:
            try:
                await user.send(embed=discord.Embed(
                    title=f"❌  Withdrawal Rejected — {emoji} {w['chain']}",
                    description=(
                        f"Your withdrawal of **{w['amount_crypto']} {w['chain']}** (~${w['amount_usd']:.2f}) "
                        f"was rejected by an admin.\n\n"
                        f"`{format_balance(w['amount_coins'], 'real')}` has been refunded to your balance."
                    ),
                    color=discord.Color.red(),
                ))
            except discord.Forbidden:
                pass


# ── Cog ───────────────────────────────────────────────────────────────────────

class CryptoWithdraw(commands.Cog):
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self._confirm_task.start()

    def cog_unload(self):
        self._confirm_task.cancel()

    @tasks.loop(seconds=60)
    async def _confirm_task(self):
        """Poll pending approved withdrawals for on-chain confirmation."""
        pending = _get_pending_txs()
        if not pending:
            return

        confirmed: list[str] = []
        loop = asyncio.get_event_loop()

        for wid, info in list(pending.items()):
            chain  = info["chain"]
            tx_id  = info["tx_id"]
            uid    = int(info["user_id"])
            try:
                if chain == "SOL":
                    done = await loop.run_in_executor(None, engine.check_sol_tx_finalized, tx_id)
                else:
                    done = await loop.run_in_executor(None, engine.check_ltc_tx_confirmed, tx_id)
            except Exception:
                continue

            if done:
                confirmed.append(wid)
                withdrawals = _get_withdrawals()
                if wid in withdrawals:
                    withdrawals[wid]["status"] = "confirmed"
                    _save_withdrawals(withdrawals)

                w = (_get_withdrawals()).get(wid, {})
                sol_emoji, ltc_emoji = _get_emojis()
                emoji = sol_emoji if chain == "SOL" else ltc_emoji
                user  = self.bot.get_user(uid)
                if user:
                    try:
                        await user.send(embed=discord.Embed(
                            title=f"🎉  Withdrawal Confirmed — {emoji} {chain}",
                            description=(
                                f"**{w.get('amount_crypto', '?')} {chain}** has arrived at your address!\n\n"
                                f"📍 `{w.get('address', '?')}`\n"
                                f"🔗 TX: `{tx_id}`"
                            ),
                            color=discord.Color.green(),
                        ))
                    except discord.Forbidden:
                        pass

        if confirmed:
            pending = _get_pending_txs()
            for wid in confirmed:
                pending.pop(wid, None)
            _save_pending_txs(pending)

    @_confirm_task.before_loop
    async def _before_confirm(self):
        await self.bot.wait_until_ready()

    async def start_withdrawal(self, interaction: discord.Interaction) -> None:
        """Entry point for the crypto withdrawal flow (called from private room menu)."""
        from cogs.crypto_withdraw_v2 import (
            build_withdraw_coin_layout,
            build_withdraw_disabled_layout,
        )
        from modules.ui_v2 import send_ephemeral

        s = engine.get_settings()
        if not s.get("enabled", False):
            return await send_ephemeral(
                interaction,
                build_withdraw_disabled_layout(
                    "Crypto Disabled",
                    "Crypto is currently disabled. Contact an admin.",
                ),
            )
        if not engine.MNEMONIC:
            return await send_ephemeral(
                interaction,
                build_withdraw_disabled_layout(
                    "Not Configured",
                    "The bot owner has not set up `CRYPTO_MNEMONIC` yet.",
                    warning=True,
                ),
            )
        if not s.get("sol_enabled") and not s.get("ltc_enabled"):
            return await send_ephemeral(
                interaction,
                build_withdraw_disabled_layout(
                    "Unavailable",
                    "❌ No crypto chains are currently enabled.",
                ),
            )

        await send_ephemeral(interaction, build_withdraw_coin_layout(interaction.user.id))


async def setup(bot: discord.Client):
    await bot.add_cog(CryptoWithdraw(bot))
