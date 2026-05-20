"""Components V2 layouts for crypto withdrawal user flow."""

from __future__ import annotations

import discord
from discord import ui

import modules.crypto_deposit as engine
from cogs.crypto_withdraw import (
    NewAddressModal,
    WithdrawAmountModal,
    _get_emojis,
    _get_user_addresses,
)
from modules.ui_v2 import ACCENT_CRYPTO, ACCENT_ERROR, ACCENT_WARNING, panel_with_controls, send_ephemeral


class WithdrawCoinSelectV2(ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        s = engine.get_settings()
        options: list[discord.SelectOption] = []
        if s.get("sol_enabled", True):
            options.append(discord.SelectOption(label="Solana (SOL)", value="SOL", emoji="🟣"))
        if s.get("ltc_enabled", True):
            options.append(discord.SelectOption(label="Litecoin (LTC)", value="LTC", emoji="🔘"))
        if not options:
            options.append(discord.SelectOption(label="No coins available", value="none"))
        super().__init__(
            placeholder="Select coin to withdraw…",
            options=options,
            custom_id="crypto_withdraw_coin_v2",
        )

    async def callback(self, interaction: discord.Interaction):
        chain = self.values[0]
        if chain == "none":
            from modules.ui_v2 import error_panel

            return await send_ephemeral(interaction, error_panel("Unavailable", "❌ No crypto is enabled."))

        saved = _get_user_addresses(self.user_id).get(chain.lower(), [])
        if not saved:
            await interaction.response.send_modal(NewAddressModal(chain=chain, user_id=self.user_id))
            return

        await interaction.response.edit_message(
            view=build_withdraw_address_layout(self.user_id, chain, saved),
        )


class WithdrawAddressSelectV2(ui.Select):
    def __init__(self, user_id: int, chain: str, saved: list[str]):
        self.user_id = user_id
        self.chain = chain
        options = [
            discord.SelectOption(
                label=(addr[:22] + "…" + addr[-6:]) if len(addr) > 30 else addr,
                value=addr,
            )
            for addr in saved[:25]
        ]
        super().__init__(
            placeholder="Select a saved address…",
            options=options,
            custom_id="crypto_withdraw_addr_v2",
        )

    async def callback(self, interaction: discord.Interaction):
        addr = self.values[0]
        await interaction.response.send_modal(
            WithdrawAmountModal(chain=self.chain, address=addr, user_id=self.user_id)
        )


class WithdrawNewAddressButton(ui.Button):
    def __init__(self, user_id: int, chain: str):
        self.user_id = user_id
        self.chain = chain
        super().__init__(label="Add New Address", style=discord.ButtonStyle.secondary, emoji="➕")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NewAddressModal(chain=self.chain, user_id=self.user_id))


class WithdrawBackToCoinButton(ui.Button):
    def __init__(self, user_id: int):
        self.user_id = user_id
        super().__init__(label="Back", style=discord.ButtonStyle.secondary, emoji="⬅️")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=build_withdraw_coin_layout(self.user_id))


def build_withdraw_coin_layout(user_id: int) -> ui.LayoutView:
    return panel_with_controls(
        title="Crypto Withdrawal",
        body=(
            "Select the cryptocurrency you want to withdraw.\n"
            "Your balance will be deducted when you submit."
        ),
        footer="Vegas Casino | Crypto",
        emoji="💸",
        accent=ACCENT_CRYPTO,
        controls=[WithdrawCoinSelectV2(user_id)],
        section_label="Coin",
    )


def build_withdraw_address_layout(user_id: int, chain: str, saved: list[str]) -> ui.LayoutView:
    sol_emoji, ltc_emoji = _get_emojis()
    emoji = sol_emoji if chain == "SOL" else ltc_emoji
    addr_lines = "\n".join(f"`{a}`" for a in saved[:10])
    if len(saved) > 10:
        addr_lines += f"\n*…and {len(saved) - 10} more*"
    return panel_with_controls(
        title=f"Withdraw {chain}",
        body=f"{emoji} Choose a saved address or add a new one.\n\n{addr_lines}",
        footer="Vegas Casino | Crypto",
        emoji=emoji,
        accent=ACCENT_CRYPTO,
        controls=[
            WithdrawAddressSelectV2(user_id, chain, saved),
            WithdrawNewAddressButton(user_id, chain),
            WithdrawBackToCoinButton(user_id),
        ],
        section_label="Address",
    )


def build_withdraw_disabled_layout(title: str, body: str, *, warning: bool = False) -> ui.LayoutView:
    return panel_with_controls(
        title=title,
        body=body,
        footer="Vegas Casino | Crypto",
        emoji="⚠️" if warning else "❌",
        accent=ACCENT_WARNING if warning else ACCENT_ERROR,
    )
