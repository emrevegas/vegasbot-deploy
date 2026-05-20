"""Events cog — admin event management panel + /events player command."""

import discord
from discord import app_commands
from discord.ext import commands

from modules.database import check_permission
from modules.utils import format_balance
from modules.constants import FOOTER_TEXT
from modules.translator import t
from modules.event_manager import (
    GAME_LABELS, GAME_EVENT_TYPES, EVENT_TYPE_DEFS,
    get_all_events, get_active_events, create_event, delete_event, toggle_event_active,
    get_user_progress_for_event, get_type_label, get_type_desc,
    get_param_label, get_reward_display,
)
from modules.player import Player


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _progress_bar(current: int, target: int, length: int = 10) -> str:
    if target <= 0:
        return "█" * length
    filled = min(length, int(current / target * length))
    return "█" * filled + "░" * (length - filled)


def _parse_reward(raw: str) -> dict:
    """Parse '50x' → multiplier or '5000' → fixed."""
    raw = raw.strip()
    if raw.lower().endswith("x"):
        try:
            return {"type": "multiplier", "value": float(raw[:-1])}
        except ValueError:
            pass
    try:
        return {"type": "fixed", "value": int(raw.replace(",", "").replace(".", ""))}
    except ValueError:
        return {"type": "fixed", "value": 0}


# ── Create-event modal ──────────────────────────────────────────────────────────

class CreateEventModal(discord.ui.Modal):
    def __init__(self, game: str, event_type: str, user_id: str):
        uid   = user_id
        edef  = EVENT_TYPE_DEFS[event_type]
        title = get_type_label(event_type, user_id=uid)[:45]
        super().__init__(title=title)
        self.game          = game
        self.event_type_id = event_type
        self.edef          = edef
        self._uid          = uid
        self._param_fields: list[tuple[str, discord.ui.TextInput]] = []

        self.name_field = discord.ui.TextInput(
            label=t("events.modal_name_label", user_id=uid),
            placeholder=t("events.modal_name_placeholder", user_id=uid),
            max_length=50,
        )
        self.add_item(self.name_field)

        for key, default, ph in edef["params"]:
            lbl = get_param_label(key, user_id=uid)
            tf  = discord.ui.TextInput(label=lbl, placeholder=ph, default=str(default), max_length=12)
            self.add_item(tf)
            self._param_fields.append((key, tf))

        self.min_bet_field = discord.ui.TextInput(
            label=t("events.modal_min_bet_label", user_id=uid),
            placeholder="100",
            default=t("events.modal_min_bet_default", user_id=uid),
            max_length=15,
        )
        self.add_item(self.min_bet_field)

        self.reward_field = discord.ui.TextInput(
            label=t("events.modal_reward_label", user_id=uid),
            placeholder=t("events.modal_reward_placeholder", user_id=uid),
            max_length=20,
        )
        self.add_item(self.reward_field)

        self.max_winners_field: discord.ui.TextInput | None = None
        if len(edef["params"]) <= 1:
            self.max_winners_field = discord.ui.TextInput(
                label=t("events.modal_max_winners_label", user_id=uid),
                placeholder=t("events.modal_max_winners_placeholder", user_id=uid),
                required=False,
                max_length=10,
            )
            self.add_item(self.max_winners_field)

    async def on_submit(self, interaction: discord.Interaction):
        uid     = self._uid
        name    = self.name_field.value.strip()
        min_bet = int(self.min_bet_field.value.strip() or "0")
        reward  = _parse_reward(self.reward_field.value)
        max_w_raw = (self.max_winners_field.value or "").strip() if self.max_winners_field else ""
        max_w   = int(max_w_raw) if max_w_raw.isdigit() else None

        params: dict = {}
        for key, tf in self._param_fields:
            raw = tf.value.strip()
            try:
                params[key] = float(raw) if "." in raw else int(raw)
            except ValueError:
                params[key] = raw

        event_data = {
            "name":        name,
            "game":        self.game,
            "type":        self.event_type_id,
            "params":      params,
            "min_bet":     min_bet,
            "reward":      reward,
            "reward_mode": "real",
            "active":      True,
            "max_winners": max_w,
        }
        event_id  = create_event(event_data)
        reward_s  = get_reward_display(event_data, user_id=uid)
        max_str   = str(max_w) if max_w else t("events.unlimited", user_id=uid)
        type_lbl  = get_type_label(self.event_type_id, user_id=uid)

        embed = discord.Embed(
            title=t("events.created_title", user_id=uid),
            color=discord.Color.green(),
        )
        embed.add_field(name=t("events.field_name",        user_id=uid), value=name,                     inline=True)
        embed.add_field(name=t("events.field_game",        user_id=uid), value=GAME_LABELS[self.game],   inline=True)
        embed.add_field(name=t("events.field_type",        user_id=uid), value=type_lbl,                 inline=False)
        embed.add_field(name=t("events.field_reward",      user_id=uid), value=reward_s,                 inline=True)
        embed.add_field(name=t("events.field_min_bet",     user_id=uid), value=f"{min_bet:,}",           inline=True)
        embed.add_field(name=t("events.field_max_winners", user_id=uid), value=max_str,                  inline=True)
        if params:
            params_str = "  •  ".join(
                f"{get_param_label(k, user_id=uid)}: **{v}**"
                for k, v in params.items()
            )
            embed.add_field(name=t("events.field_params", user_id=uid), value=params_str, inline=False)
        embed.set_footer(text=f"ID: {event_id}  •  {FOOTER_TEXT}")

        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Step-2 view: pick event type ────────────────────────────────────────────────

class _EventTypeSelect(discord.ui.Select):
    def __init__(self, game: str, user_id: str):
        self.game = game
        self._uid = user_id
        type_ids  = GAME_EVENT_TYPES.get(game, [])
        options   = [
            discord.SelectOption(
                label=get_type_label(tid, user_id=user_id)[:100],
                value=tid,
                description=get_type_desc(tid, user_id=user_id)[:100],
            )
            for tid in type_ids
        ]
        super().__init__(
            placeholder=t("events.type_select_placeholder", user_id=user_id),
            options=options,
            min_values=1, max_values=1,
            custom_id=f"ev_type_sel:{game}",
        )

    async def callback(self, interaction: discord.Interaction):
        modal = CreateEventModal(self.game, self.values[0], user_id=str(interaction.user.id))
        await interaction.response.send_modal(modal)


class _BackToStep1Button(discord.ui.Button):
    def __init__(self, user_id: str):
        super().__init__(
            label=t("events.back_btn", user_id=user_id),
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        self._uid = user_id

    async def callback(self, interaction: discord.Interaction):
        uid   = str(interaction.user.id)
        embed = discord.Embed(
            title=t("events.create_step1_title", user_id=uid),
            description=t("events.create_step1_desc", user_id=uid),
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=CreateEventStep1View(uid))


class CreateEventStep2View(discord.ui.View):
    def __init__(self, game: str, user_id: str):
        super().__init__(timeout=300)
        self.add_item(_EventTypeSelect(game, user_id))
        self.add_item(_BackToStep1Button(user_id))


# ── Step-1 view: pick game ──────────────────────────────────────────────────────

class _GameSelectForEvent(discord.ui.Select):
    def __init__(self, user_id: str):
        self._uid = user_id
        options   = [
            discord.SelectOption(label=label, value=gid)
            for gid, label in GAME_LABELS.items()
            if gid in GAME_EVENT_TYPES
        ]
        super().__init__(
            placeholder=t("events.game_select_placeholder", user_id=user_id),
            options=options,
            min_values=1, max_values=1,
            custom_id="ev_game_sel",
        )

    async def callback(self, interaction: discord.Interaction):
        uid   = str(interaction.user.id)
        game  = self.values[0]
        embed = discord.Embed(
            title=t("events.create_step2_title", user_id=uid),
            description=t("events.create_step2_desc", user_id=uid),
            color=discord.Color.blurple(),
        )
        embed.set_author(name=GAME_LABELS[game])
        await interaction.response.edit_message(embed=embed, view=CreateEventStep2View(game, uid))


class CreateEventStep1View(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=300)
        self.add_item(_GameSelectForEvent(user_id))


# ── Event list — admin ──────────────────────────────────────────────────────────

class _DeleteEventSelect(discord.ui.Select):
    def __init__(self, event_ids: list[str], events: dict, user_id: str):
        uid     = user_id
        options = [
            discord.SelectOption(
                label=events[eid]["name"][:100],
                value=eid,
                description=(
                    f"{GAME_LABELS.get(events[eid]['game'], events[eid]['game'])}  •  "
                    f"{get_type_label(events[eid]['type'], user_id=uid)[:60]}"
                ),
            )
            for eid in event_ids
        ]
        super().__init__(
            placeholder=t("events.delete_placeholder", user_id=uid),
            options=options,
            min_values=1, max_values=min(25, len(options)),
            custom_id="ev_delete_sel",
        )
        self._uid = uid

    async def callback(self, interaction: discord.Interaction):
        if check_permission(interaction.user.id, "admin"):
            return await interaction.response.send_message(
                t("events.no_permission", user_id=str(interaction.user.id)), ephemeral=True
            )
        uid      = str(interaction.user.id)
        events   = get_all_events()
        deleted  = []
        for event_id in self.values:
            ev_name = events.get(event_id, {}).get("name", event_id)
            if delete_event(event_id):
                deleted.append(ev_name)
        names_str = "\n".join(f"• **{n}**" for n in deleted)
        await interaction.response.send_message(
            embed=discord.Embed(
                title=t("events.deleted_title", user_id=uid),
                description=names_str or "—",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )


class _ToggleEventSelect(discord.ui.Select):
    def __init__(self, event_ids: list[str], events: dict, user_id: str):
        uid     = user_id
        options = [
            discord.SelectOption(
                label=events[eid]["name"][:100],
                value=eid,
                description=(
                    t("events.status_active", user_id=uid)
                    if events[eid].get("active", True)
                    else t("events.status_paused", user_id=uid)
                ),
            )
            for eid in event_ids
        ]
        super().__init__(
            placeholder=t("events.toggle_placeholder", user_id=uid),
            options=options,
            min_values=1, max_values=1,
            custom_id="ev_toggle_sel",
        )
        self._uid = uid

    async def callback(self, interaction: discord.Interaction):
        if check_permission(interaction.user.id, "admin"):
            return await interaction.response.send_message(
                t("events.no_permission", user_id=str(interaction.user.id)), ephemeral=True
            )
        uid       = str(interaction.user.id)
        event_id  = self.values[0]
        new_state = toggle_event_active(event_id)
        state_str = (
            t("events.status_active", user_id=uid)
            if new_state
            else t("events.status_paused", user_id=uid)
        )
        events  = get_all_events()
        ev_name = events.get(event_id, {}).get("name", event_id)
        await interaction.response.send_message(
            embed=discord.Embed(
                title=t("events.toggled_title", user_id=uid),
                description=t("events.toggled_desc", user_id=uid, name=ev_name, state=state_str),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )


def _build_admin_event_list(user_id: str) -> tuple[discord.Embed, discord.ui.View | None]:
    uid    = user_id
    events = get_all_events()
    embed  = discord.Embed(title=t("events.all_events_title", user_id=uid), color=discord.Color.blurple())
    embed.set_footer(text=FOOTER_TEXT)

    if not events:
        embed.description = t("events.no_events", user_id=uid)
        return embed, None

    for ev in list(events.values())[:20]:
        rstr      = get_reward_display(ev, user_id=uid)
        winners_n = len(ev.get("winners", []))
        max_w     = ev.get("max_winners") or "∞"
        status    = (
            t("events.status_active", user_id=uid)
            if ev.get("active", True)
            else t("events.status_paused", user_id=uid)
        )
        min_bet_s = f"{int(ev.get('min_bet', 0)):,}"
        type_lbl  = get_type_label(ev["type"], user_id=uid)
        embed.add_field(
            name=ev["name"],
            value=(
                f"🎮 {GAME_LABELS.get(ev['game'], ev['game'])}  •  {status}\n"
                f"📋 {type_lbl}\n"
                f"💰 **{rstr}**  •  Min: {min_bet_s}  •  👥 {winners_n}/{max_w}"
            ),
            inline=False,
        )

    event_ids = list(events.keys())[:25]
    view = discord.ui.View(timeout=300)
    view.add_item(_DeleteEventSelect(event_ids, events, uid))
    view.add_item(_ToggleEventSelect(event_ids, events, uid))
    return embed, view


# ── Main admin panel view ───────────────────────────────────────────────────────

class EventAdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="➕", style=discord.ButtonStyle.success, row=0)
    async def create_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if check_permission(interaction.user.id, "admin"):
            return await interaction.response.send_message(
                t("events.no_permission", user_id=str(interaction.user.id)), ephemeral=True
            )
        uid   = str(interaction.user.id)
        button.label = t("events.create_btn", user_id=uid)
        embed = discord.Embed(
            title=t("events.create_step1_title", user_id=uid),
            description=t("events.create_step1_desc", user_id=uid),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=CreateEventStep1View(uid), ephemeral=True)

    @discord.ui.button(label="📋", style=discord.ButtonStyle.primary, row=0)
    async def list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if check_permission(interaction.user.id, "admin"):
            return await interaction.response.send_message(
                t("events.no_permission", user_id=str(interaction.user.id)), ephemeral=True
            )
        uid         = str(interaction.user.id)
        embed, view = _build_admin_event_list(uid)
        if view:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Player /events view ─────────────────────────────────────────────────────────

PAGE_SIZE = 3


def _build_player_events_embed(
    user: discord.User | discord.Member,
    page: int = 0,
) -> tuple[discord.Embed, int]:
    """Build the embed for given page. Returns (embed, total_pages)."""
    uid    = str(user.id)
    active = list(get_active_events().values())
    total  = len(active)
    pages  = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page   = max(0, min(page, pages - 1))

    embed = discord.Embed(
        title=t("events.player_title", user_id=uid),
        color=discord.Color.gold(),
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    if not active:
        embed.description = t("events.no_active", user_id=uid)
        embed.set_footer(text=FOOTER_TEXT)
        return embed, 1

    chunk = active[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    for ev in chunk:
        prog      = get_user_progress_for_event(user.id, ev)
        current   = prog["current"]
        target    = prog["target"]
        done      = prog["completed"]
        unit_key  = prog["unit_key"]
        from modules.database import get_data as _gd
        _coin_emoji = (_gd("server/server") or {}).get("coin_emoji", "💵")
        unit_str  = t(f"events.unit_{unit_key}", user_id=uid, coin_emoji=_coin_emoji)
        type_lbl  = get_type_label(ev["type"], user_id=uid)
        type_desc = get_type_desc(ev["type"], user_id=uid, event=ev)
        reward_s  = get_reward_display(ev, user_id=uid)
        min_bet   = int(ev.get("min_bet", 0))
        max_w     = ev.get("max_winners")
        winners_n = len(ev.get("winners", []))

        if done:
            status_line = t("events.player_done", user_id=uid)
        elif unit_key in ("streak", "count", "wins", "gems", "coins"):
            bar = _progress_bar(current, target)
            status_line = f"`{bar}` {current}/{target} {unit_str}"
        else:
            status_line = t("events.player_not_done", user_id=uid)

        footer_parts = [f"{t('events.label_min_bet', user_id=uid)}: {min_bet:,}"]
        if max_w:
            footer_parts.append(f"{t('events.label_winners', user_id=uid)}: {winners_n}/{max_w}")

        embed.add_field(
            name=f"🏆 {ev['name']}",
            value=(
                f"🎮 {GAME_LABELS.get(ev['game'], ev['game'])}  •  {type_lbl}\n"
                f"📝 *{type_desc}*\n"
                f"💰 {t('events.field_reward', user_id=uid)}: **{reward_s}**\n"
                f"{status_line}\n"
                f"*{' • '.join(footer_parts)}*"
            ),
            inline=False,
        )

    embed.set_footer(text=f"{FOOTER_TEXT}  •  {page + 1}/{pages}")
    return embed, pages


class EventsPageView(discord.ui.View):
    def __init__(self, user: discord.User | discord.Member, page: int, total_pages: int):
        super().__init__(timeout=120)
        self.user        = user
        self.page        = page
        self.total_pages = total_pages
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.defer()
        self.page -= 1
        embed, self.total_pages = _build_player_events_embed(self.user, self.page)
        self._update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.defer()
        self.page += 1
        embed, self.total_pages = _build_player_events_embed(self.user, self.page)
        self._update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)


# ── Notification helper (called from game hooks) ────────────────────────────────

async def send_event_completion(
    interaction: discord.Interaction,
    completed: list[dict],
) -> None:
    """Send a public channel message for each completed event."""
    uid = str(interaction.user.id)
    for c in completed:
        ev     = c["event"]
        reward = c["reward"]
        mode   = c["mode"]
        rstr   = format_balance(reward, mode)
        embed  = discord.Embed(
            title=t("events.completion_title", user_id=uid),
            description=t(
                "events.completion_desc",
                user_id=uid,
                mention=interaction.user.mention,
                name=ev["name"],
                game=GAME_LABELS.get(ev["game"], ev["game"]),
                reward=rstr,
            ),
            color=discord.Color.gold(),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text=FOOTER_TEXT)
        try:
            await interaction.channel.send(embed=embed)
        except Exception:
            pass


# ── Cog ─────────────────────────────────────────────────────────────────────────

class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="event", description="Admin: event yönetim paneli")
    async def event_admin(self, interaction: discord.Interaction):
        if check_permission(interaction.user.id, "admin"):
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=t("events.no_permission", user_id=str(interaction.user.id)),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
        uid   = str(interaction.user.id)
        embed = discord.Embed(
            title=t("events.admin_panel_title", user_id=uid),
            description=t("events.admin_panel_desc", user_id=uid),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=FOOTER_TEXT)
        await interaction.response.send_message(embed=embed, view=EventAdminView(), ephemeral=True)

    @app_commands.command(name="events", description="Aktif eventleri ve ilerlemenizi görün")
    async def events_list(self, interaction: discord.Interaction):
        embed, total_pages = _build_player_events_embed(interaction.user, 0)
        if total_pages > 1:
            view = EventsPageView(interaction.user, 0, total_pages)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EventsCog(bot))
