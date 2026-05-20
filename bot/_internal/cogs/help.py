import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Select

from modules.constants import FOOTER_TEXT
from modules.translator import t
from modules.utils import get_user_lang

_COLOR_MAIN    = 0x5865F2
_COLOR_START   = 0x57F287
_COLOR_ROOMS   = 0x1ABC9C
_COLOR_GAMES   = 0xFEE75C
_COLOR_WALLET  = 0x2ECC71
_COLOR_LEVELS  = 0xF1C40F
_COLOR_RACES   = 0xE91E63
_COLOR_CASES   = 0xF39C12
_COLOR_GIVE    = 0x9B59B6
_COLOR_FAIR    = 0x3498DB

_CAT_KEYS = [
    "getting_started", "rooms", "games", "wallet",
    "levels", "races", "cases", "giveaways", "fairness",
]

def _main_embed(lang="en"):
    embed = discord.Embed(
        title=t("help.main_title", lang=lang),
        description=t("help.main_description", lang=lang),
        color=_COLOR_MAIN,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def _getting_started_embed(lang="en"):
    embed = discord.Embed(title=t("help.getting_started_title", lang=lang), color=_COLOR_START)
    embed.add_field(name=t("help.gs_register_name", lang=lang),  value=t("help.gs_register_value", lang=lang),  inline=False)
    embed.add_field(name=t("help.gs_language_name", lang=lang),  value=t("help.gs_language_value", lang=lang),  inline=False)
    embed.add_field(name=t("help.gs_room_name", lang=lang),      value=t("help.gs_room_value", lang=lang),      inline=False)
    embed.add_field(name=t("help.gs_fund_name", lang=lang),      value=t("help.gs_fund_value", lang=lang),      inline=False)
    embed.add_field(name=t("help.gs_mode_name", lang=lang),      value=t("help.gs_mode_value", lang=lang),      inline=False)
    embed.add_field(name=t("help.gs_commands_name", lang=lang),  value=t("help.gs_commands_value", lang=lang),  inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def _rooms_embed(lang="en"):
    embed = discord.Embed(title=t("help.rooms_title", lang=lang), description=t("help.rooms_description", lang=lang), color=_COLOR_ROOMS)
    embed.add_field(name=t("help.rooms_create_name", lang=lang),    value=t("help.rooms_create_value", lang=lang),    inline=False)
    embed.add_field(name=t("help.rooms_entertain_name", lang=lang), value=t("help.rooms_entertain_value", lang=lang), inline=False)
    embed.add_field(name=t("help.rooms_finance_name", lang=lang),   value=t("help.rooms_finance_value", lang=lang),   inline=False)
    embed.add_field(name=t("help.rooms_manage_name", lang=lang),    value=t("help.rooms_manage_value", lang=lang),    inline=False)
    embed.add_field(name=t("help.rooms_settings_name", lang=lang),  value=t("help.rooms_settings_value", lang=lang),  inline=False)
    embed.add_field(name=t("help.rooms_timer_name", lang=lang),     value=t("help.rooms_timer_value", lang=lang),     inline=False)
    embed.set_footer(text=f"{FOOTER_TEXT}  -  {t('help.rooms_footer', lang=lang)}")
    return embed

def _games_embed(lang="en"):
    embed = discord.Embed(title=t("help.games_title", lang=lang), description=t("help.games_description", lang=lang), color=_COLOR_GAMES)
    embed.add_field(name=t("help.games_access_name", lang=lang), value=t("help.games_access_value", lang=lang), inline=False)
    embed.add_field(
        name="\U0001f3ae\u2003Game List",
        value=(
            "\U0001fa99 **Hot & Cold**  \u00b7  \U0001f3b2 **Dice**  \u00b7  \U0001f0cf **Blackjack**  \u00b7  \U0001f534 **Roulette**\n"
            "\U0001f4a3 **Mines**  \u00b7  \U0001f48e **Crystals**  \u00b7  \U0001f5fc **Towers**\n"
            "\U0001f680 **Limbo**  \u00b7  \U0001f0e0 **Hi-Lo**  \u00b7  \U0001f3b0 **Slots**"
        ),
        inline=False,
    )
    embed.set_footer(text=f"{FOOTER_TEXT}  -  {t('help.games_footer', lang=lang)}")
    return embed

def _wallet_embed(lang="en"):
    embed = discord.Embed(title=t("help.wallet_title", lang=lang), description=t("help.wallet_description", lang=lang), color=_COLOR_WALLET)
    embed.add_field(name=t("help.wallet_types_name", lang=lang),      value=t("help.wallet_types_value", lang=lang),      inline=False)
    embed.add_field(name=t("help.wallet_crypto_dep_name", lang=lang), value=t("help.wallet_crypto_dep_value", lang=lang), inline=False)
    embed.add_field(name=t("help.wallet_manual_dep_name", lang=lang), value=t("help.wallet_manual_dep_value", lang=lang), inline=False)
    embed.add_field(name=t("help.wallet_crypto_wd_name", lang=lang),  value=t("help.wallet_crypto_wd_value", lang=lang),  inline=False)
    embed.add_field(name=t("help.wallet_manual_wd_name", lang=lang),  value=t("help.wallet_manual_wd_value", lang=lang),  inline=False)
    embed.add_field(name=t("help.wallet_bonus_name", lang=lang),      value=t("help.wallet_bonus_value", lang=lang),      inline=False)
    embed.set_footer(text=f"{FOOTER_TEXT}  -  {t('help.wallet_footer', lang=lang)}")
    return embed

def _levels_embed(lang="en"):
    embed = discord.Embed(title=t("help.levels_title", lang=lang), color=_COLOR_LEVELS)
    embed.add_field(name=t("help.levels_how_name", lang=lang),      value=t("help.levels_how_value", lang=lang),      inline=False)
    embed.add_field(name=t("help.levels_chest_name", lang=lang),    value=t("help.levels_chest_value", lang=lang),    inline=False)
    embed.add_field(name=t("help.levels_rakeback_name", lang=lang), value=t("help.levels_rakeback_value", lang=lang), inline=False)
    embed.add_field(name=t("help.levels_tiers_name", lang=lang),    value=t("help.levels_tiers_value", lang=lang),    inline=False)
    embed.add_field(name=t("help.levels_commands_name", lang=lang), value=t("help.levels_commands_value", lang=lang), inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def _races_embed(lang="en"):
    embed = discord.Embed(title=t("help.races_title", lang=lang), description=t("help.races_description", lang=lang), color=_COLOR_RACES)
    embed.add_field(name=t("help.races_wager_name", lang=lang),   value=t("help.races_wager_value", lang=lang),   inline=False)
    embed.add_field(name=t("help.races_deposit_name", lang=lang), value=t("help.races_deposit_value", lang=lang), inline=False)
    embed.add_field(name=t("help.races_period_name", lang=lang),  value=t("help.races_period_value", lang=lang),  inline=False)
    embed.add_field(name=t("help.races_prizes_name", lang=lang),  value=t("help.races_prizes_value", lang=lang),  inline=False)
    embed.add_field(name=t("help.races_join_name", lang=lang),    value=t("help.races_join_value", lang=lang),    inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def _cases_embed(lang="en"):
    embed = discord.Embed(title=t("help.cases_title", lang=lang), color=_COLOR_CASES)
    embed.add_field(name=t("help.cases_what_name", lang=lang),      value=t("help.cases_what_value", lang=lang),      inline=False)
    embed.add_field(name=t("help.cases_official_name", lang=lang),  value=t("help.cases_official_value", lang=lang),  inline=False)
    embed.add_field(name=t("help.cases_community_name", lang=lang), value=t("help.cases_community_value", lang=lang), inline=False)
    embed.add_field(name=t("help.cases_fair_name", lang=lang),      value=t("help.cases_fair_value", lang=lang),      inline=False)
    embed.add_field(name=t("help.cases_commands_name", lang=lang),  value=t("help.cases_commands_value", lang=lang),  inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def _giveaways_embed(lang="en"):
    embed = discord.Embed(title=t("help.giveaways_title", lang=lang), color=_COLOR_GIVE)
    embed.add_field(name=t("help.giveaways_auto_name", lang=lang),   value=t("help.giveaways_auto_value", lang=lang),   inline=False)
    embed.add_field(name=t("help.giveaways_enter_name", lang=lang),  value=t("help.giveaways_enter_value", lang=lang),  inline=False)
    embed.add_field(name=t("help.giveaways_prizes_name", lang=lang), value=t("help.giveaways_prizes_value", lang=lang), inline=False)
    embed.add_field(name=t("help.giveaways_notif_name", lang=lang),  value=t("help.giveaways_notif_value", lang=lang),  inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def _fairness_embed(lang="en"):
    embed = discord.Embed(title=t("help.fairness_title", lang=lang), description=t("help.fairness_description", lang=lang), color=_COLOR_FAIR)
    embed.add_field(name=t("help.fairness_seeds_name", lang=lang),    value=t("help.fairness_seeds_value", lang=lang),    inline=False)
    embed.add_field(name=t("help.fairness_gen_name", lang=lang),      value=t("help.fairness_gen_value", lang=lang),      inline=False)
    embed.add_field(name=t("help.fairness_formulas_name", lang=lang), value=t("help.fairness_formulas_value", lang=lang), inline=False)
    embed.add_field(name=t("help.fairness_verify_name", lang=lang),   value=t("help.fairness_verify_value", lang=lang),   inline=False)
    embed.set_footer(text=f"{FOOTER_TEXT}  -  {t('help.fairness_footer', lang=lang)}")
    return embed

_CAT_EMBED_FNS = {
    "getting_started": _getting_started_embed,
    "rooms":           _rooms_embed,
    "games":           _games_embed,
    "wallet":          _wallet_embed,
    "levels":          _levels_embed,
    "races":           _races_embed,
    "cases":           _cases_embed,
    "giveaways":       _giveaways_embed,
    "fairness":        _fairness_embed,
}

def _build_cat_options(lang, active_key=None):
    return [
        discord.SelectOption(
            label=t(f"help.cat_{key}", lang=lang),
            value=key,
            description=t(f"help.cat_{key}_desc", lang=lang),
            default=(key == active_key),
        )
        for key in _CAT_KEYS
    ]

class HelpSelect(Select):
    def __init__(self, lang="en"):
        self._lang = lang
        super().__init__(
            placeholder=t("help.select_placeholder", lang=lang),
            options=_build_cat_options(lang),
            min_values=1,
            max_values=1,
            custom_id="help_category_select",
        )

    async def callback(self, interaction):
        key = self.values[0]
        embed = _CAT_EMBED_FNS[key](self._lang)
        view = HelpCategoryView(active_key=key, lang=self._lang)
        await interaction.response.edit_message(embed=embed, view=view)

class HelpCategorySelect(Select):
    def __init__(self, active_key=None, lang="en"):
        self._lang = lang
        super().__init__(
            placeholder=t("help.switch_placeholder", lang=lang),
            options=_build_cat_options(lang, active_key=active_key),
            min_values=1,
            max_values=1,
            custom_id="help_cat_select_inner",
        )

    async def callback(self, interaction):
        key = self.values[0]
        embed = _CAT_EMBED_FNS[key](self._lang)
        view = HelpCategoryView(active_key=key, lang=self._lang)
        await interaction.response.edit_message(embed=embed, view=view)

class HelpCategoryView(View):
    def __init__(self, active_key=None, lang="en"):
        super().__init__(timeout=300)
        self._active_key = active_key
        self._lang = lang
        self.add_item(HelpCategorySelect(active_key=active_key, lang=lang))
        back_btn = discord.ui.Button(
            label=t("help.back_btn", lang=lang),
            style=discord.ButtonStyle.secondary,
            custom_id="help_back_btn",
            row=1,
        )
        back_btn.callback = self._on_back
        self.add_item(back_btn)

    async def _on_back(self, interaction):
        await interaction.response.edit_message(
            embed=_main_embed(self._lang),
            view=HelpMainView(lang=self._lang),
        )

class HelpMainView(View):
    def __init__(self, lang="en"):
        super().__init__(timeout=300)
        self.add_item(HelpSelect(lang=lang))

class HelpCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Open the interactive help menu")
    async def help_command(self, interaction: discord.Interaction):
        from cogs.help_v2 import build_help_main_layout
        from modules.ui_v2 import send_ephemeral

        lang = get_user_lang(interaction.user.id)
        await send_ephemeral(interaction, build_help_main_layout(lang))

    @app_commands.command(name="balance", description="View your balance and stats")
    async def balance_command(self, interaction: discord.Interaction):
        from modules.player import Player
        from modules.utils import format_balance
        from modules.database import get_user_data

        uid = interaction.user.id
        lang = get_user_lang(uid)

        account = get_user_data(uid, "account") or {}
        if not (account and account.get("name")):
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=t("balance.not_registered", lang=lang),
                    color=0xED4245,
                ).set_footer(text=FOOTER_TEXT),
                ephemeral=True,
            )
            return

        player = Player(uid)
        real_balance = player.balance
        demo_balance = (get_user_data(uid, "balance") or {}).get("demo", 0)

        stats = player.stats or {}
        total_plays = stats.get("total_plays", 0)
        wins        = stats.get("wins", 0)
        losses      = stats.get("losses", 0)
        wagered     = stats.get("total_wagered", 0)
        winrate     = round(wins / total_plays * 100, 1) if total_plays > 0 else 0.0

        embed = discord.Embed(
            title=t("balance.title", lang=lang).format(username=interaction.user.display_name),
            color=0x2ECC71,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name=t("balance.real_field", lang=lang), value=format_balance(real_balance, mode="real"), inline=True)
        embed.add_field(name=t("balance.demo_field", lang=lang), value=format_balance(demo_balance, mode="demo"), inline=True)
        embed.add_field(
            name=t("balance.stats_field", lang=lang),
            value=t("balance.stats_value", lang=lang).format(
                wagered=f"{wagered:,}",
                wins=wins,
                losses=losses,
                winrate=winrate,
            ),
            inline=False,
        )
        embed.set_footer(text=t("balance.footer", lang=lang))
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(HelpCog(bot))
