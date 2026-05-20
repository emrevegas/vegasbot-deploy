"""Components V2 layouts for /help command."""

from __future__ import annotations

import discord
from discord import ui

from cogs.help import (
    _CAT_EMBED_FNS,
    _CAT_KEYS,
    _build_cat_options,
    _main_embed,
)
from modules.constants import FOOTER_TEXT
from modules.games_hub_v2 import embed_to_panel_text
from modules.translator import t
from modules.ui_v2 import ACCENT_BRAND, ACCENT_INFO, add_section, build_layout, new_container, panel_with_controls

_CAT_ACCENTS = {
    "getting_started": 0x57F287,
    "rooms": 0x1ABC9C,
    "games": 0xFEE75C,
    "wallet": 0x2ECC71,
    "levels": 0xF1C40F,
    "races": 0xE91E63,
    "cases": 0xF39C12,
    "giveaways": 0x9B59B6,
    "fairness": 0x3498DB,
}


class HelpCategorySelectV2(ui.Select):
    def __init__(self, lang: str = "en", active_key: str | None = None):
        self._lang = lang
        super().__init__(
            placeholder=t("help.switch_placeholder", lang=lang),
            options=_build_cat_options(lang, active_key=active_key),
            min_values=1,
            max_values=1,
            custom_id="help_cat_select_v2",
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        await interaction.response.edit_message(view=build_help_category_layout(key, self._lang))


class HelpMainSelectV2(ui.Select):
    def __init__(self, lang: str = "en"):
        self._lang = lang
        super().__init__(
            placeholder=t("help.select_placeholder", lang=lang),
            options=_build_cat_options(lang),
            min_values=1,
            max_values=1,
            custom_id="help_category_select_v2",
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        await interaction.response.edit_message(view=build_help_category_layout(key, self._lang))


class HelpBackButton(ui.Button):
    def __init__(self, lang: str = "en"):
        super().__init__(
            label=t("help.back_btn", lang=lang),
            style=discord.ButtonStyle.secondary,
            custom_id="help_back_btn_v2",
        )
        self._lang = lang

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=build_help_main_layout(self._lang))


def build_help_main_layout(lang: str = "en") -> ui.LayoutView:
    embed = _main_embed(lang)
    body = embed_to_panel_text(embed)
    return panel_with_controls(
        title=t("help.main_title", lang=lang),
        body=body.split("\n\n", 1)[-1] if body.startswith("##") else body,
        footer=FOOTER_TEXT,
        emoji="📖",
        accent=ACCENT_BRAND,
        controls=[HelpMainSelectV2(lang)],
        section_label=t("help.select_placeholder", lang=lang),
    )


def build_help_category_layout(active_key: str, lang: str = "en") -> ui.LayoutView:
    embed = _CAT_EMBED_FNS[active_key](lang)
    body = embed_to_panel_text(embed)
    title = embed.title or t(f"help.cat_{active_key}", lang=lang)
    if body.startswith("##"):
        parts = body.split("\n\n", 1)
        body = parts[1] if len(parts) > 1 else ""
    c = new_container(accent=_CAT_ACCENTS.get(active_key, ACCENT_INFO))
    c.add_item(ui.TextDisplay(f"## {title}"))
    if body:
        c.add_item(ui.TextDisplay(body))
    if embed.footer and embed.footer.text:
        c.add_item(ui.TextDisplay(f"-# {embed.footer.text}"))
    add_section(
        c,
        t("help.switch_placeholder", lang=lang),
        HelpCategorySelectV2(lang, active_key=active_key),
        HelpBackButton(lang),
    )
    return build_layout(c, timeout=300)
