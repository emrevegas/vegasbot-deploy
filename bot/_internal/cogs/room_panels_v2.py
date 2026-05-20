"""Components V2 panels for private-room entertainment / settings flows."""

from __future__ import annotations

import discord
from discord import ui

from modules.database import get_data, get_user_data, set_data
from modules.translator import t
from modules.ui_v2 import (
    ACCENT_BRAND,
    ACCENT_INFO,
    ACCENT_NEUTRAL,
    ACCENT_SUCCESS,
    ACCENT_WARNING,
    add_action_row,
    add_section,
    build_detail_panel,
    build_layout,
    new_container,
    panel_markdown,
    panel_with_controls,
    send_ephemeral,
)
from modules.utils import format_balance


# ── Promo ─────────────────────────────────────────────────────────────────────


class PromoEnterButton(ui.Button):
    def __init__(self, user_id: int):
        super().__init__(label="Enter Promo Code", style=discord.ButtonStyle.primary, emoji="🎟️")
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your panel!", ephemeral=True)
        from cogs.private_rooms import PromoCodeInputModal

        await interaction.response.send_modal(PromoCodeInputModal(self.user_id))


class PromoCheckButton(ui.Button):
    def __init__(self, user_id: int):
        super().__init__(label="Check Status", style=discord.ButtonStyle.secondary, emoji="🔄")
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your panel!", ephemeral=True)
        import modules.promo as promo_engine
        from cogs.private_rooms import _build_active_promo_embed_and_view

        active = promo_engine.get_active_promo(self.user_id)
        if active:
            view = build_active_promo_layout(self.user_id, active, interaction.user)
            await interaction.response.edit_message(view=view)
        else:
            await interaction.response.send_message(
                "✅ No active promo — enter a code to get started!", ephemeral=True
            )


def build_promo_redeem_layout(user_id: int, lang: str = "en") -> ui.LayoutView:
    body = (
        "Enter a promo code to claim a balance reward or free-game rounds!\n\n"
        "**Balance codes** → coins added instantly (1× wager req)\n"
        "**Free-game codes** → free rounds; winnings credited after (1× wager req)"
    )
    return panel_with_controls(
        title="Promo / Free Bet",
        body=body,
        footer="Vegas Casino | Promo",
        emoji="🎟️",
        accent=0x9B59B6,
        controls=[PromoEnterButton(user_id), PromoCheckButton(user_id)],
        section_label="Actions",
    )


def build_active_promo_layout(user_id: int, active: dict, user: discord.User) -> ui.LayoutView:
    """Simplified V2 active promo summary."""
    import modules.promo as promo_engine

    template = promo_engine.get_promo_template(active.get("code", "")) or {}
    ptype = template.get("type", "balance")
    code = active.get("code", "?")
    if ptype == "balance":
        body = f"**Code:** `{code}`\nActive balance promo — play to clear wager."
    else:
        game = template.get("game", "game").title()
        left = int(active.get("rounds_left", 0))
        body = f"**Code:** `{code}`\n**{left}** free **{game}** rounds remaining."
    return build_detail_panel(
        title="Active Promo",
        body=body,
        accent=0x9B59B6,
        emoji="🎟️",
        footer="Vegas Casino | Promo",
    )


# ── Rakeback ──────────────────────────────────────────────────────────────────


class RakebackWithdrawButton(ui.Button):
    def __init__(self, user_id: int, can_withdraw: bool, min_withdrawal: int, lang: str):
        label = (
            t("rakeback.withdraw_button", lang=lang)
            if can_withdraw
            else t("rakeback.withdraw_button_disabled", lang=lang, min=format_balance(min_withdrawal, "real"))
        )
        super().__init__(
            label=label[:80],
            style=discord.ButtonStyle.success if can_withdraw else discord.ButtonStyle.secondary,
            disabled=not can_withdraw,
        )
        self.user_id = user_id
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your menu.", ephemeral=True)
        from modules.player import Player
        from modules.utils import get_user_lang

        player = Player(self.user_id)
        accumulated = player.get_accumulated_rakeback()
        settings = get_data("server/rakeback_settings") or {}
        min_w = int(settings.get("min_withdrawal", 100))
        if accumulated < min_w:
            await send_ephemeral(
                interaction,
                build_detail_panel(
                    title=t("rakeback.title", lang=self.lang),
                    body=t(
                        "rakeback.withdraw_error_insufficient",
                        lang=self.lang,
                        min=format_balance(min_w, "real"),
                        current=format_balance(accumulated, "real"),
                    ),
                    accent=ACCENT_WARNING,
                    emoji="⚠️",
                ),
            )
            return
        player.withdraw_rakeback(accumulated)
        await send_ephemeral(
            interaction,
            build_detail_panel(
                title="Rakeback Withdrawn",
                body=f"**{format_balance(accumulated, 'real')}** added to your balance.",
                accent=ACCENT_SUCCESS,
                emoji="✅",
            ),
        )


def build_rakeback_layout(
    user_id: int,
    *,
    best_tier,
    accumulated: int,
    total_earned: int,
    min_withdrawal: int,
    total_wagered: int,
    can_withdraw: bool,
    lang: str,
) -> ui.LayoutView:
    tier_txt = (
        f"<@&{best_tier['role_id']}> — **{best_tier['percentage']}%** per bet"
        if best_tier
        else t("rakeback.no_tier", lang=lang)
    )
    fields = {
        t("rakeback.tier_field", lang=lang): tier_txt,
        t("rakeback.accumulated_field", lang=lang): format_balance(accumulated, "real"),
        t("rakeback.total_earned_field", lang=lang): format_balance(total_earned, "real"),
        t("rakeback.min_withdrawal_field", lang=lang): format_balance(min_withdrawal, "real"),
        t("rakeback.total_wagered_field", lang=lang): format_balance(total_wagered, "real"),
    }
    c = new_container(accent=ACCENT_BRAND)
    c.add_item(
        ui.TextDisplay(
            panel_markdown(
                title=t("rakeback.title", lang=lang),
                body=t("rakeback.description", lang=lang),
                footer=t("rakeback.footer", lang=lang),
                emoji="💸",
            )
        )
    )
    c.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
    lines = "\n".join(f"**{k}**\n{v}" for k, v in fields.items())
    c.add_item(ui.TextDisplay(lines[:4000]))
    add_section(c, t("private_rooms.section_actions", lang=lang), RakebackWithdrawButton(user_id, can_withdraw, min_withdrawal, lang))
    return build_layout(c, timeout=120)


# ── Referral ──────────────────────────────────────────────────────────────────


class ReferralCreateButton(ui.Button):
    def __init__(self, user_id: str):
        super().__init__(label="Create Referral Code", style=discord.ButtonStyle.primary, emoji="✨")
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id:
            return await interaction.response.send_message("❌ This is not your panel!", ephemeral=True)
        from cogs.private_rooms import CreateReferralCodeModal

        await interaction.response.send_modal(CreateReferralCodeModal(self.user_id))


class ReferralClaimButton(ui.Button):
    def __init__(self, user_id: str, available: int):
        super().__init__(
            label="Claim Earnings",
            style=discord.ButtonStyle.success,
            emoji="💸",
            disabled=available <= 0,
        )
        self.user_id = user_id
        self.available = available

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id:
            return await interaction.response.send_message("❌ This is not your panel!", ephemeral=True)
        if self.available <= 0:
            return await interaction.response.send_message("❌ No earnings to claim.", ephemeral=True)
        from modules.player import Player

        player = Player(int(self.user_id))
        player.add_balance("real", self.available)
        referrals_data = get_data("server/referrals") or {}
        if self.user_id in referrals_data:
            referrals_data[self.user_id]["available_balance"] = 0
            set_data("server/referrals", referrals_data)
        await send_ephemeral(
            interaction,
            build_detail_panel(
                title="Earnings Claimed",
                body=f"**{format_balance(self.available, 'real')}** added to your balance.",
                accent=ACCENT_SUCCESS,
                emoji="✅",
            ),
        )


def build_referral_create_layout(user_id: str, lang: str = "en") -> ui.LayoutView:
    return panel_with_controls(
        title=t("referral.title", lang=lang),
        body=t("referral.no_code", lang=lang) + "\n\n" + t("referral.referral_info", lang=lang).format(rate=5),
        footer="Vegas Casino | Referral",
        emoji="🎁",
        accent=ACCENT_WARNING,
        controls=[ReferralCreateButton(user_id)],
        section_label=t("private_rooms.section_actions", lang=lang),
    )


def build_referral_dashboard_layout(
    user_id: str,
    *,
    code: str,
    total_referrals: int,
    commission_rate,
    available_balance: int,
    today_earned: int,
    total_earned: int,
    lang: str = "en",
) -> ui.LayoutView:
    fields = {
        t("referral.your_code", lang=lang): f"`{code}`",
        t("referral.total_referrals", lang=lang): str(total_referrals),
        t("referral.commission_rate", lang=lang): f"{commission_rate}%",
        t("referral.total_earned", lang=lang): format_balance(available_balance, "real"),
        t("referral.today_earned", lang=lang): format_balance(today_earned, "real"),
        t("referral.all_time_earned", lang=lang): format_balance(total_earned, "real"),
    }
    view = build_detail_panel(
        title=t("referral.title", lang=lang),
        body=t("referral.referral_info", lang=lang).format(rate=commission_rate),
        fields=fields,
        accent=ACCENT_BRAND,
        emoji="🎁",
        footer="Vegas Casino | Referral",
    )
    layout = view
    container = layout.children[0]  # type: ignore
    add_section(container, t("private_rooms.section_actions", lang=lang), ReferralClaimButton(user_id, available_balance))
    return layout


# ── Support category ──────────────────────────────────────────────────────────


class SupportCategorySelect(discord.ui.Select):
    def __init__(self, lang: str = "en"):
        self._lang = lang
        super().__init__(
            placeholder=t("support.select_category", lang=lang),
            options=[
                discord.SelectOption(label=t("support.category_balance", lang=lang), emoji="💰", value="balance"),
                discord.SelectOption(label=t("support.category_technical", lang=lang), emoji="🔧", value="technical"),
                discord.SelectOption(label=t("support.category_bug", lang=lang), emoji="🐛", value="bug"),
                discord.SelectOption(label=t("support.category_general", lang=lang), emoji="💬", value="general"),
            ],
            custom_id="private_room:support_category_v2",
        )

    async def callback(self, interaction: discord.Interaction):
        from cogs.private_rooms import TicketDescriptionModal

        tickets_data = get_data("server/tickets") or {}
        guild_id = str(interaction.guild.id)
        if guild_id not in tickets_data:
            tickets_data[guild_id] = {}
        for ticket_id, ticket_info in tickets_data[guild_id].items():
            if ticket_info.get("user_id") == interaction.user.id and ticket_info.get("status") == "open":
                channel = interaction.guild.get_channel(int(ticket_id))
                if channel:
                    from modules.ui_v2 import warning_panel, send_ephemeral

                    await send_ephemeral(
                        interaction,
                        warning_panel(
                            "Active Ticket",
                            t("support.already_has_ticket", lang=self._lang).format(channel=channel.mention),
                        ),
                    )
                    return
        ticket_settings = get_data("server/ticket_settings") or {}
        if not ticket_settings.get("category_id"):
            from modules.ui_v2 import error_panel, send_ephemeral

            await send_ephemeral(
                interaction,
                error_panel(
                    "Not Configured",
                    t("support.no_category_configured", lang=self._lang),
                ),
            )
            return
        await interaction.response.send_modal(TicketDescriptionModal(self.values[0]))


def build_support_category_layout(lang: str = "en") -> ui.LayoutView:
    return panel_with_controls(
        title=t("support.title", lang=lang),
        body=t("support.description", lang=lang),
        footer="Vegas Casino | Support",
        emoji="🎫",
        accent=ACCENT_INFO,
        controls=[SupportCategorySelect(lang)],
        section_label=t("support.select_category", lang=lang),
    )


# ── Language settings ───────────────────────────────────────────────────────


class LanguageSelectV2(discord.ui.Select):
    def __init__(self, user_id: int, current_lang: str):
        self.user_id = user_id
        super().__init__(
            placeholder=t("private_rooms.placeholder_language", lang=current_lang),
            options=[
                discord.SelectOption(label="English", emoji="🇬🇧", value="en"),
                discord.SelectOption(label="Türkçe", emoji="🇹🇷", value="tr"),
                discord.SelectOption(label="Bahasa Indonesia", emoji="🇮🇩", value="id"),
            ],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your settings panel!", ephemeral=True)
        selected = self.values[0]
        set_user_data(self.user_id, "lang", {"language": selected})
        names = {"en": "🇬🇧 English", "tr": "🇹🇷 Türkçe", "id": "🇮🇩 Bahasa Indonesia"}
        await send_ephemeral(
            interaction,
            build_detail_panel(
                title="Language Updated",
                body=f"Your language is now **{names.get(selected, selected)}**.",
                accent=ACCENT_SUCCESS,
                emoji="✅",
            ),
        )


def build_language_settings_layout(user_id: int, current_lang: str) -> ui.LayoutView:
    flag = {"en": "🇬🇧", "tr": "🇹🇷", "id": "🇮🇩"}.get(current_lang, "🏴")
    return panel_with_controls(
        title="Language Settings",
        body=f"Current: {flag} **{current_lang.upper()}**\nChoose a new language below.",
        footer="Vegas Casino | Settings",
        emoji="🌐",
        accent=ACCENT_NEUTRAL,
        controls=[LanguageSelectV2(user_id, current_lang)],
        section_label=t("private_rooms.placeholder_language", lang=current_lang),
    )


# ── Player statistics ─────────────────────────────────────────────────────────


class _PlayerStatsTabButton(ui.Button):
    def __init__(
        self,
        user_id: int,
        stats: dict,
        player,
        member: discord.Member,
        lang: str,
        tab: str,
        *,
        active: bool,
        viewer_id: int,
    ):
        labels = {
            "overview": t("player_stats.btn_overview", lang=lang),
            "games": t("player_stats.btn_games", lang=lang),
            "rakeback": t("player_stats.btn_rakeback", lang=lang),
        }
        super().__init__(
            label=labels[tab][:80],
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
        )
        self._user_id = user_id
        self._stats = stats
        self._player = player
        self._member = member
        self._lang = lang
        self._tab = tab
        self._viewer_id = viewer_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self._viewer_id:
            return await interaction.response.send_message(
                t("player_stats.not_your_panel", lang=self._lang), ephemeral=True
            )
        layout = build_player_stats_layout(
            self._user_id,
            self._stats,
            self._player,
            self._member,
            lang=self._lang,
            tab=self._tab,
            viewer_id=self._viewer_id,
        )
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class _PlayerStatsRefreshButton(ui.Button):
    def __init__(
        self,
        user_id: int,
        member: discord.Member,
        lang: str,
        viewer_id: int,
    ):
        super().__init__(
            label=t("player_stats.btn_refresh", lang=lang)[:80],
            style=discord.ButtonStyle.success,
        )
        self._user_id = user_id
        self._member = member
        self._lang = lang
        self._viewer_id = viewer_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self._viewer_id:
            return await interaction.response.send_message(
                t("player_stats.not_your_panel", lang=self._lang), ephemeral=True
            )
        from modules.database import get_user_stats
        from modules.player import Player

        stats = get_user_stats(self._user_id) or {}
        player = Player(self._user_id)
        layout = build_player_stats_layout(
            self._user_id,
            stats,
            player,
            self._member,
            lang=self._lang,
            tab="overview",
            viewer_id=self._viewer_id,
        )
        await interaction.response.edit_message(embed=None, content=None, view=layout)


class _AdminDepositHistoryButton(ui.Button):
    def __init__(self, target_user_id: int, viewer_id: int):
        super().__init__(label="Deposit History", style=discord.ButtonStyle.secondary, emoji="📥")
        self._target = target_user_id
        self._viewer_id = viewer_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self._viewer_id:
            return await interaction.response.defer()
        from cogs.user_management import AdminPlayerStatsView

        embed = AdminPlayerStatsView._build_dep_history_embed(self._target)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class _AdminWithdrawHistoryButton(ui.Button):
    def __init__(self, target_user_id: int, viewer_id: int):
        super().__init__(label="Withdraw History", style=discord.ButtonStyle.secondary, emoji="📤")
        self._target = target_user_id
        self._viewer_id = viewer_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self._viewer_id:
            return await interaction.response.defer()
        from cogs.user_management import AdminPlayerStatsView

        embed = AdminPlayerStatsView._build_wdr_history_embed(self._target)
        await interaction.response.send_message(embed=embed, ephemeral=True)


def build_player_stats_layout(
    user_id: int,
    stats: dict,
    player,
    member: discord.Member,
    *,
    lang: str = "en",
    tab: str = "overview",
    viewer_id: int | None = None,
    admin_viewer_id: int | None = None,
) -> ui.LayoutView:
    from cogs.private_rooms import PlayerStatsView
    from modules.games_hub_v2 import embed_to_panel_text

    viewer = viewer_id or user_id
    if tab == "games":
        embed = PlayerStatsView.build_breakdown_embed(member, stats, lang)
    elif tab == "rakeback":
        embed = PlayerStatsView.build_rakeback_embed(member, player, lang)
    else:
        embed = PlayerStatsView.build_overview_embed(member, stats, player, lang)

    body = embed_to_panel_text(embed)
    accent = int(embed.color.value) if embed.color else ACCENT_BRAND
    c = new_container(accent=accent)
    if embed.thumbnail and embed.thumbnail.url:
        c.add_item(ui.Section(ui.TextDisplay(body), accessory=ui.Thumbnail(media=embed.thumbnail.url)))
    else:
        c.add_item(ui.TextDisplay(body))

    tab_btns = [
        _PlayerStatsTabButton(user_id, stats, player, member, lang, "overview", active=tab == "overview", viewer_id=viewer),
        _PlayerStatsTabButton(user_id, stats, player, member, lang, "games", active=tab == "games", viewer_id=viewer),
        _PlayerStatsTabButton(user_id, stats, player, member, lang, "rakeback", active=tab == "rakeback", viewer_id=viewer),
        _PlayerStatsRefreshButton(user_id, member, lang, viewer),
    ]
    c.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
    add_action_row(c, *tab_btns[:4])
    if admin_viewer_id is not None:
        add_action_row(
            c,
            _AdminDepositHistoryButton(user_id, admin_viewer_id),
            _AdminWithdrawHistoryButton(user_id, admin_viewer_id),
        )
    return build_layout(c, timeout=180)


# ── Finance (exchange / withdraw) ─────────────────────────────────────────────


class ExchangeRatesCalculatorButton(ui.Button):
    def __init__(self, user_id: int, lang: str):
        super().__init__(
            label=t("exchange_rates.calculator_button", lang=lang)[:80],
            style=discord.ButtonStyle.primary,
            emoji="🧮",
        )
        self.user_id = user_id
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                t("exchange_rates.not_your_panel", lang=self.lang), ephemeral=True
            )
        from cogs.private_rooms import CoinCalculatorModal

        await interaction.response.send_modal(CoinCalculatorModal(self.user_id, self.lang))


def _exchange_rates_body(lang: str) -> str:
    from cogs.private_rooms import _fmt_num

    rates_data = get_data("server/exchange_rates") or {}
    server_data = get_data("server/server") or {}
    coin_emoji = server_data.get("coin_emoji", "🪙")
    coin_usd_rate = rates_data.get("coin_usd_rate", 0.10)
    custom_rates = rates_data.get("custom_rates", [])

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"{t('exchange_rates.usd_label', lang=lang)}",
        f"> {coin_emoji}  1 Coin  =  **${coin_usd_rate:.4g}**",
    ]
    for rate in custom_rates:
        r_emoji = rate.get("emoji", coin_emoji)
        r_name = rate.get("name", "Unknown")
        r_amount = rate.get("amount", 0)
        lines.append(f"**{r_emoji}  {r_name}**")
        lines.append(f"> {coin_emoji}  1 Coin  =  {r_emoji} **{_fmt_num(r_amount)} {r_name}**")
    lines.extend(["━━━━━━━━━━━━━━━━━━━━━━", t("exchange_rates.rates_by_admin", lang=lang)])
    return "\n\n".join(lines)


def build_exchange_rates_layout(user_id: int, lang: str = "en") -> ui.LayoutView:
    return panel_with_controls(
        title=t("exchange_rates.title", lang=lang),
        body=_exchange_rates_body(lang),
        footer="Vegas Casino | Exchange Rates",
        emoji="💱",
        accent=0xF5A623,
        controls=[ExchangeRatesCalculatorButton(user_id, lang)],
        section_label=t("private_rooms.section_actions", lang=lang),
    )


def build_withdraw_method_layout(
    user_id: str,
    methods: dict,
    min_withdrawal: int,
    balance: int,
    wager_info: str,
) -> ui.LayoutView:
    from cogs.private_rooms import WithdrawMethodSelect

    min_label = format_balance(min_withdrawal, "real")
    body = (
        "Select your preferred payment method below.\n\n"
        f"💵 Your Balance: **{format_balance(balance, 'real')}**\n"
        f"📉 Min Withdrawal: {min_label}"
        f"{wager_info}"
    )
    return panel_with_controls(
        title="Withdrawal",
        body=body,
        footer="Vegas Casino | Withdrawal System",
        emoji="🏦",
        accent=ACCENT_WARNING,
        controls=[WithdrawMethodSelect(user_id, methods, min_withdrawal)],
        section_label="Payment method",
    )


# ── Room management ───────────────────────────────────────────────────────────


class RemoveMemberSelectV2(discord.ui.Select):
    def __init__(
        self,
        channel: discord.TextChannel,
        guild: discord.Guild,
        users_list: list,
        lang: str = "en",
    ):
        self.channel = channel
        self.guild = guild
        self.lang = lang
        options = []
        for uid in users_list:
            member = guild.get_member(uid)
            if member:
                options.append(
                    discord.SelectOption(
                        label=member.display_name[:100],
                        value=str(member.id),
                        description=f"@{member.name}"[:100],
                        emoji="👤",
                    )
                )
        if not options:
            options.append(
                discord.SelectOption(
                    label="No members to remove",
                    value="none",
                    description="Room has no additional members",
                )
            )
        super().__init__(
            placeholder=t("private_rooms.placeholder_remove_member", lang=lang),
            options=options,
            custom_id="private_room:remove_member_select_v2",
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            from modules.ui_v2 import error_panel, send_ephemeral

            await send_ephemeral(
                interaction,
                error_panel("No Members", "❌ No members to remove!"),
            )
            return

        member_id = int(self.values[0])
        member = self.guild.get_member(member_id)
        if not member:
            from modules.ui_v2 import error_panel, send_ephemeral

            await send_ephemeral(interaction, error_panel("Not Found", "❌ Member not found!"))
            return

        try:
            await self.channel.set_permissions(member, overwrite=None)
            rooms_data = get_data("server/private_rooms")
            guild_id = str(self.guild.id)
            channel_id = str(self.channel.id)
            if guild_id in rooms_data and channel_id in rooms_data[guild_id]:
                users = rooms_data[guild_id][channel_id].get("users", [])
                if member_id in users:
                    users.remove(member_id)
                    rooms_data[guild_id][channel_id]["users"] = users
                    set_data("server/private_rooms", rooms_data)

            from modules.ui_v2 import success_panel, send_ephemeral

            await send_ephemeral(
                interaction,
                success_panel(
                    t("private_rooms.member_removed_title", lang=self.lang),
                    t("private_rooms.member_removed_description", lang=self.lang).format(
                        member=member.mention
                    )
                    + f"\n\n**User ID:** `{member.id}`\n**Username:** `{member.name}`",
                ),
            )
            await self.channel.send(
                embed=discord.Embed(
                    description=f"➖ {member.mention} has been removed from the room.",
                    color=discord.Color.orange(),
                )
            )
        except Exception as e:
            from modules.ui_v2 import error_panel, send_ephemeral

            await send_ephemeral(
                interaction,
                error_panel("Error", f"❌ Error removing member: {e}"),
            )


class CloseRoomConfirmButton(ui.Button):
    def __init__(self, channel: discord.TextChannel, channel_id: str, guild_id: str, lang: str = "en"):
        self.channel = channel
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.lang = lang
        super().__init__(
            label=t("private_rooms.close_confirm_btn", lang=lang),
            style=discord.ButtonStyle.danger,
            emoji="🔒",
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            rooms_data = get_data("server/private_rooms")
            if self.guild_id in rooms_data and self.channel_id in rooms_data[self.guild_id]:
                del rooms_data[self.guild_id][self.channel_id]
                if not rooms_data[self.guild_id]:
                    del rooms_data[self.guild_id]
                set_data("server/private_rooms", rooms_data)

            from modules.ui_v2 import send_ephemeral, warning_panel

            await send_ephemeral(
                interaction,
                warning_panel(
                    t("private_rooms.room_closing_title", lang=self.lang),
                    t("private_rooms.room_closing_description", lang=self.lang),
                ),
            )
            await self.channel.send(
                embed=discord.Embed(
                    description="🔒 This room is being closed by the owner...",
                    color=discord.Color.red(),
                )
            )
            await self.channel.delete(reason="Room closed by owner")
        except Exception as e:
            from modules.ui_v2 import error_panel, send_ephemeral

            await send_ephemeral(
                interaction,
                error_panel("Error", f"❌ Error closing room: {e}"),
            )


class CloseRoomCancelButton(ui.Button):
    def __init__(self, lang: str = "en"):
        super().__init__(
            label=t("private_rooms.close_cancel_btn", lang=lang),
            style=discord.ButtonStyle.secondary,
            emoji="❌",
        )
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        from modules.ui_v2 import info_panel, send_ephemeral

        await send_ephemeral(
            interaction,
            info_panel("Cancelled", t("private_rooms.close_cancelled", lang=self.lang)),
        )


def build_remove_member_layout(
    channel: discord.TextChannel,
    guild: discord.Guild,
    users_list: list,
    lang: str = "en",
) -> ui.LayoutView:
    return panel_with_controls(
        title=t("private_rooms.remove_member_title", lang=lang),
        body=t("private_rooms.remove_member_body", lang=lang, count=len(users_list)),
        footer="Vegas Casino | Room",
        emoji="➖",
        accent=ACCENT_WARNING,
        controls=[RemoveMemberSelectV2(channel, guild, users_list, lang)],
        section_label=t("private_rooms.section_room", lang=lang),
    )


def build_close_room_layout(
    channel: discord.TextChannel,
    channel_id: str,
    guild_id: str,
    lang: str = "en",
) -> ui.LayoutView:
    return panel_with_controls(
        title=t("private_rooms.close_room_title", lang=lang),
        body=t("private_rooms.confirm_close_room", lang=lang),
        footer="Vegas Casino | Room",
        emoji="🔒",
        accent=0xE74C3C,
        controls=[
            CloseRoomConfirmButton(channel, channel_id, guild_id, lang),
            CloseRoomCancelButton(lang),
        ],
        section_label=t("private_rooms.section_actions", lang=lang),
    )
