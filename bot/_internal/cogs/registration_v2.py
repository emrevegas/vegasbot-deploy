"""Components V2 layouts for registration flows."""

from __future__ import annotations

import discord
from discord import ui

from modules.translator import t
from modules.ui_v2 import ACCENT_BRAND, ACCENT_INFO, panel_with_controls


class RegisterButtonV2(ui.Button):
    """Persistent register button inside V2 channel panel."""

    def __init__(self) -> None:
        super().__init__(
            label="Register / Kayit Ol / Daftar",
            style=discord.ButtonStyle.primary,
            emoji="\U0001f4dd",
            custom_id="registration:register_button",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from cogs.registration import _is_registered

        uid = interaction.user.id
        if _is_registered(uid):
            from modules.ui_v2 import send_ephemeral, warning_panel

            await send_ephemeral(
                interaction,
                warning_panel(
                    "Already Registered",
                    t("registration.error_already_registered", user_id=str(uid)),
                ),
            )
            return

        from modules.ui_v2 import send_ephemeral

        await send_ephemeral(interaction, build_registration_language_layout(uid))


class RegistrationChannelLayout(ui.LayoutView):
    """Persistent channel registration menu (Components V2)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        from modules.ui_v2 import add_section, new_container, panel_markdown

        c = new_container(accent=ACCENT_BRAND)
        c.add_item(
            ui.TextDisplay(
                panel_markdown(
                    title=t("registration.menu_title", lang="en"),
                    body=t("registration.menu_description", lang="en"),
                    footer="Vegas Casino",
                    emoji="\U0001f4dd",
                )
            )
        )
        add_section(c, "Get started", RegisterButtonV2())
        self.add_item(c)


def build_registration_language_layout(user_id: int) -> ui.LayoutView:
    from cogs.registration import RegistrationLanguageSelect

    return panel_with_controls(
        title="Select Language / Dilinizi Secin / Pilih Bahasa",
        body=(
            "Please select your language before registration.\n"
            "Lutfen kayit olmadan once dilinizi secin.\n"
            "Silakan pilih bahasa Anda sebelum mendaftar."
        ),
        footer="Vegas Casino | Language Selection",
        emoji="\U0001f310",
        accent=ACCENT_INFO,
        controls=[RegistrationLanguageSelect(user_id)],
        section_label="Language",
    )
