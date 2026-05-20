"""Server Setup Cog — Full automated Discord server setup for Vegas Casino bot.

Creates a complete, organized server structure with categories, channels, and roles,
then wires all IDs into the bot's database automatically.

Usage:
  /server_setup  → Confirmation embed → Confirm button (guild owner only)

Only the server OWNER can run this command.
"""

import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands

from modules.database import get_server_data, set_server_data, set_data, get_data
import modules.crypto_deposit as crypto_engine


# ── Server structure definition ────────────────────────────────────────────────
# Each category has a name and a list of (channel_name, type, topic, permissions_preset)
# permissions_preset: "public" | "private" | "admin_only" | "log"

STRUCTURE = [
    {
        "name": "🏠 ─── WELCOME ───",
        "overwrites_preset": "read_only",
        "channels": [
            ("📢┃announcements",  "text",  "Official announcements from the casino.",         "read_only"),
            ("📋┃rules",          "text",  "Server rules and casino terms of service.",        "read_only"),
            ("🤖┃commands",       "text",  "Bot commands and how to use them.",                "read_only"),
            ("💬┃general",        "text",  "General chat for all members.",                    "public"),
        ],
    },
    {
        "name": "🎰 ─── CASINO ───",
        "overwrites_preset": "members_only",
        "channels": [
            ("🎮┃register",       "text",  "Register here to start playing.",                  "public"),
            ("🎲┃games",          "text",  "Play casino games here.",                          "members_only"),
            ("🏆┃leaderboard",    "text",  "Top players and stats.",                           "read_only"),
            ("📊┃live-stats",     "text",  "Live casino statistics.",                          "read_only"),
        ],
    },
    {
        "name": "💰 ─── FINANCE ───",
        "overwrites_preset": "members_only",
        "channels": [
            ("💳┃deposit",        "text",  "Deposit funds to your account.",                   "members_only"),
            ("💸┃withdraw",       "text",  "Withdraw requests are processed here.",            "members_only"),
            ("🏎️┃races",          "text",  "Active casino races and competitions.",            "members_only"),
            ("🎁┃giveaways",      "text",  "Casino giveaways and promotions.",                 "members_only"),
        ],
    },
    {
        "name": "🔒 ─── STAFF ───",
        "overwrites_preset": "staff_only",
        "channels": [
            ("🛡️┃admin-panel",    "text",  "Admin panel and management.",                      "staff_only"),
            ("📝┃withdraw-log",   "text",  "Withdrawal request logs.",                         "staff_only"),
            ("💰┃deposit-log",    "text",  "Deposit confirmation logs.",                       "staff_only"),
            ("🔄┃sweep-log",      "text",  "Auto-sweep transaction logs.",                     "staff_only"),
            ("🎰┃fairness-log",   "text",  "Provably fair game verification logs.",            "staff_only"),
            ("📊┃staff-chat",     "text",  "Internal staff communication.",                    "staff_only"),
        ],
    },
    {
        "name": "🏦 ─── PRIVATE ROOMS ───",
        "overwrites_preset": "admin_only",
        "channels": [],  # created dynamically by private_rooms cog
    },
    {
        "name": "📥 ─── DEPOSIT TICKETS ───",
        "overwrites_preset": "admin_only",
        "channels": [],  # tickets created here
    },
]

ROLES = [
    # (name, color_hex, hoist, mentionable)
    ("🎰 Casino Member",  0x2ecc71, True,  False),
    ("💳 Cashier",        0xe67e22, True,  True),
    ("🛡️ Moderator",      0x3498db, True,  True),
    ("👑 Admin",          0xe74c3c, True,  True),
    ("🤖 Bot",            0x95a5a6, False, False),
]


# ── Permission helpers ─────────────────────────────────────────────────────────

def _base_overwrites(guild: discord.Guild, member_role: discord.Role | None,
                     staff_role: discord.Role | None, admin_role: discord.Role | None,
                     preset: str) -> dict:
    ev = guild.default_role
    ow: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}

    if preset == "read_only":
        # Everyone can read, cannot send
        ow[ev] = discord.PermissionOverwrite(
            read_messages=True, send_messages=False, add_reactions=False
        )
    elif preset == "public":
        ow[ev] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    elif preset == "members_only":
        ow[ev] = discord.PermissionOverwrite(read_messages=False, send_messages=False)
        if member_role:
            ow[member_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    elif preset in ("staff_only", "admin_only", "log"):
        ow[ev] = discord.PermissionOverwrite(read_messages=False, send_messages=False)

    # Staff always sees staff_only and log channels
    if staff_role and preset in ("staff_only", "log"):
        ow[staff_role] = discord.PermissionOverwrite(
            read_messages=True, send_messages=True, attach_files=True
        )
    if admin_role:
        ow[admin_role] = discord.PermissionOverwrite(
            read_messages=True, send_messages=True, manage_messages=True, attach_files=True
        )
    return ow


# ── Main setup logic ───────────────────────────────────────────────────────────

async def _run_setup(guild: discord.Guild, progress_msg: discord.WebhookMessage | discord.Message):
    """Create full server structure and configure bot settings."""

    async def _update(text: str):
        try:
            await progress_msg.edit(content=text)
        except Exception:
            pass

    await _update("⏳ **Step 1/6** — Creating roles…")

    # ── 1. Create roles ──────────────────────────────────────────────────────
    created_roles: dict[str, discord.Role] = {}
    existing_roles = {r.name: r for r in guild.roles}

    for rname, color, hoist, mention in ROLES:
        if rname in existing_roles:
            created_roles[rname] = existing_roles[rname]
        else:
            try:
                r = await guild.create_role(
                    name=rname, color=discord.Color(color),
                    hoist=hoist, mentionable=mention,
                    reason="Vegas Casino server setup",
                )
                created_roles[rname] = r
                await asyncio.sleep(0.4)
            except Exception as e:
                print(f"[Setup] Role create failed ({rname}): {e}")

    member_role = created_roles.get("🎰 Casino Member")
    cashier_role = created_roles.get("💳 Cashier")
    mod_role = created_roles.get("🛡️ Moderator")
    admin_role = created_roles.get("👑 Admin")

    # staff_role used for overwrites — union of cashier + mod + admin
    # We apply overwrites per role individually
    await _update("⏳ **Step 2/6** — Creating categories and channels…")

    # ── 2. Create categories + channels ─────────────────────────────────────
    created_channels: dict[str, discord.TextChannel] = {}
    created_cats: dict[str, discord.CategoryChannel] = {}
    existing_cats = {c.name: c for c in guild.categories}

    for cat_def in STRUCTURE:
        cat_name = cat_def["name"]
        cat_preset = cat_def["overwrites_preset"]
        cat_ow = _base_overwrites(guild, member_role, cashier_role, admin_role, cat_preset)
        # Add mod role to staff categories
        if cat_preset in ("staff_only", "admin_only") and mod_role:
            cat_ow[mod_role] = discord.PermissionOverwrite(
                read_messages=True, send_messages=True, manage_messages=True
            )

        if cat_name in existing_cats:
            cat = existing_cats[cat_name]
        else:
            try:
                cat = await guild.create_category(
                    cat_name, overwrites=cat_ow, reason="Vegas Casino server setup"
                )
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[Setup] Category create failed ({cat_name}): {e}")
                continue

        created_cats[cat_name] = cat

        for ch_name, ch_type, topic, ch_preset in cat_def.get("channels", []):
            if ch_type != "text":
                continue
            ch_ow = _base_overwrites(guild, member_role, cashier_role, admin_role, ch_preset)
            if ch_preset in ("staff_only", "admin_only", "log") and mod_role:
                ch_ow[mod_role] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, manage_messages=True
                )
            try:
                ch = await cat.create_text_channel(
                    ch_name, topic=topic, overwrites=ch_ow,
                    reason="Vegas Casino server setup",
                )
                created_channels[ch_name] = ch
                await asyncio.sleep(0.4)
            except Exception as e:
                print(f"[Setup] Channel create failed ({ch_name}): {e}")

    await _update("⏳ **Step 3/6** — Sending welcome / rules content…")

    # ── 3. Send starter messages ──────────────────────────────────────────────
    rules_ch = created_channels.get("📋┃rules")
    if rules_ch:
        try:
            await rules_ch.purge(limit=5)
            em = discord.Embed(
                title="📋  Server Rules",
                description=(
                    "1. Be respectful to all members.\n"
                    "2. No spam or self-promotion.\n"
                    "3. All bets placed are final.\n"
                    "4. Do not share personal information.\n"
                    "5. Casino decisions are final.\n"
                    "6. Users found exploiting bugs will be banned.\n"
                    "7. Enjoy and play responsibly! 🎰"
                ),
                color=0x2b2d31,
            )
            em.set_footer(text=f"{guild.name}  ·  Casino Terms of Service")
            await rules_ch.send(embed=em)
        except Exception as e:
            print(f"[Setup] Rules message failed: {e}")

    ann_ch = created_channels.get("📢┃announcements")
    if ann_ch:
        try:
            await ann_ch.purge(limit=5)
            em = discord.Embed(
                title=f"🎰  Welcome to {guild.name}!",
                description=(
                    f"We're excited to have you here!\n\n"
                    f"**Get started:**\n"
                    f"1. Head to <#{created_channels.get('🎮┃register', ann_ch).id}> and register\n"
                    f"2. Deposit funds in <#{created_channels.get('💳┃deposit', ann_ch).id}>\n"
                    f"3. Play in <#{created_channels.get('🎲┃games', ann_ch).id}> — Good luck! 🍀"
                ),
                color=0x9945FF,
            )
            em.set_footer(text="Powered by Vegas Casino Bot  ·  Provably Fair")
            await ann_ch.send(embed=em)
        except Exception as e:
            print(f"[Setup] Announcement message failed: {e}")

    await _update("⏳ **Step 4/6** — Sending registration panel…")

    # ── 4. Send registration panel ────────────────────────────────────────────
    reg_ch = created_channels.get("🎮┃register")
    if reg_ch:
        try:
            from cogs.registration import RegistrationView
            await reg_ch.purge(limit=5)
            em = discord.Embed(
                title="🎰  Casino Registration",
                description=(
                    "Welcome! Click the button below to create your casino account.\n\n"
                    "📌 Registration is **free** and takes less than a minute.\n"
                    "🎁 Get **demo coins** to try games risk-free!"
                ),
                color=0x9945FF,
            )
            em.set_footer(text=f"{guild.name}  ·  Provably Fair Casino")
            await reg_ch.send(embed=em, view=RegistrationView())
        except Exception as e:
            print(f"[Setup] Registration panel failed: {e}")

    await _update("⏳ **Step 5/6** — Configuring bot database settings…")

    # ── 5. Wire IDs into bot database ─────────────────────────────────────────
    guild_id = str(guild.id)

    def _ch_id(name: str) -> int | None:
        ch = created_channels.get(name)
        return ch.id if ch else None

    def _cat_id(name: str) -> int | None:
        cat = created_cats.get(name)
        return cat.id if cat else None

    # server_data settings
    sd = get_server_data(guild_id)
    sd["registration_channel"]  = _ch_id("🎮┃register")
    sd["withdraw_channel"]      = _ch_id("💸┃withdraw")
    sd["private_category_id"]   = _cat_id("🏦 ─── PRIVATE ROOMS ───")
    sd["deposit_category"]      = _cat_id("📥 ─── DEPOSIT TICKETS ───")
    sd["cashier_role"]          = cashier_role.id if cashier_role else None
    sd["member_role"]           = member_role.id if member_role else None
    sd["pf_log_channel"]        = _ch_id("🎰┃fairness-log")
    sd["live_stats_channel"]    = _ch_id("📊┃live-stats")
    set_server_data(guild_id, sd)

    # crypto deposit settings
    cs = crypto_engine.get_settings()
    cs["deposit_log_channel_id"]  = _ch_id("💰┃deposit-log")
    cs["withdraw_log_channel_id"] = _ch_id("📝┃withdraw-log")
    cs["sweep_log_channel_id"]    = _ch_id("🔄┃sweep-log")
    crypto_engine.save_settings(cs)

    await _update("⏳ **Step 6/6** — Finalizing and verifying…")
    await asyncio.sleep(1)

    # ── 6. Build result embed ─────────────────────────────────────────────────
    def _mention_ch(name: str) -> str:
        ch = created_channels.get(name)
        return ch.mention if ch else "`—`"

    def _mention_cat(name: str) -> str:
        cat = created_cats.get(name)
        return f"**{cat.name}**" if cat else "`—`"

    def _mention_role(r: discord.Role | None) -> str:
        return r.mention if r else "`—`"

    total_ch = len(created_channels)
    total_cat = len(created_cats)

    embed = discord.Embed(
        title="✅  Server Setup Complete!",
        description=(
            f"**{guild.name}** has been fully configured as a Vegas Casino server.\n"
            f"Created **{total_cat}** categories and **{total_ch}** channels."
        ),
        color=0x2ecc71,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="🎰 Casino Channels",
        value=(
            f"Register: {_mention_ch('🎮┃register')}\n"
            f"Games: {_mention_ch('🎲┃games')}\n"
            f"Live Stats: {_mention_ch('📊┃live-stats')}\n"
            f"Leaderboard: {_mention_ch('🏆┃leaderboard')}"
        ),
        inline=True,
    )
    embed.add_field(
        name="💰 Finance Channels",
        value=(
            f"Deposit: {_mention_ch('💳┃deposit')}\n"
            f"Withdraw: {_mention_ch('💸┃withdraw')}\n"
            f"Races: {_mention_ch('🏎️┃races')}\n"
            f"Giveaways: {_mention_ch('🎁┃giveaways')}"
        ),
        inline=True,
    )
    embed.add_field(
        name="🔒 Staff Channels",
        value=(
            f"Withdraw Log: {_mention_ch('📝┃withdraw-log')}\n"
            f"Deposit Log: {_mention_ch('💰┃deposit-log')}\n"
            f"Sweep Log: {_mention_ch('🔄┃sweep-log')}\n"
            f"Fairness Log: {_mention_ch('🎰┃fairness-log')}"
        ),
        inline=True,
    )
    embed.add_field(
        name="👥 Roles Created",
        value=(
            f"{_mention_role(member_role)}\n"
            f"{_mention_role(cashier_role)}\n"
            f"{_mention_role(mod_role)}\n"
            f"{_mention_role(admin_role)}"
        ),
        inline=True,
    )
    embed.add_field(
        name="📁 Categories",
        value="\n".join(f"`{c}`" for c in created_cats),
        inline=True,
    )
    embed.add_field(
        name="⚙️ Bot Settings Wired",
        value=(
            f"✅ Registration channel\n"
            f"✅ Withdraw channel\n"
            f"✅ Private rooms category\n"
            f"✅ Deposit tickets category\n"
            f"✅ Cashier & Member roles\n"
            f"✅ PF / Live stats / Log channels\n"
            f"✅ Crypto deposit/withdraw/sweep logs"
        ),
        inline=True,
    )
    embed.set_footer(text="Vegas Casino  ·  Full setup complete  ·  Ready to use!")
    return embed


# ── Confirmation view ──────────────────────────────────────────────────────────

class SetupConfirmView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self._owner_id = owner_id
        self._done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "❌ Only the server owner can confirm this.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Confirm — Setup Server", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self._done:
            return
        self._done = True
        self.stop()

        await interaction.response.defer(ephemeral=False, thinking=True)
        progress = await interaction.followup.send(
            "⏳ **Setting up your server…** This may take 30–60 seconds, please wait.",
        )

        try:
            embed = await _run_setup(interaction.guild, progress)
            await progress.edit(content=None, embed=embed)
        except Exception as e:
            await progress.edit(
                content=None,
                embed=discord.Embed(
                    title="❌ Setup Failed",
                    description=f"```{e}```\nPlease check bot permissions and try again.",
                    color=discord.Color.red(),
                ),
            )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        self._done = True
        self.stop()
        await interaction.response.edit_message(
            content="❌ Server setup cancelled.", embed=None, view=None
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class ServerSetup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="server_setup",
        description="[Owner only] Fully configure this server for Vegas Casino bot.",
    )
    @app_commands.guild_only()
    async def server_setup(self, interaction: discord.Interaction):
        # Only server owner
        if interaction.guild.owner_id != interaction.user.id:
            return await interaction.response.send_message(
                "❌ Only the **server owner** can run this command.", ephemeral=True
            )

        # Check bot permissions
        me = interaction.guild.me
        needed = ["manage_channels", "manage_roles", "send_messages",
                  "read_messages", "embed_links", "attach_files"]
        missing = [p for p in needed if not getattr(me.guild_permissions, p, False)]
        if missing:
            return await interaction.response.send_message(
                f"❌ Bot is missing required permissions:\n`{', '.join(missing)}`\n"
                "Please give the bot **Administrator** or at least the above permissions.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title="⚠️  Vegas Casino — Full Server Setup",
            description=(
                "This will **create the following** in your server:\n\n"
                "📁 **6 Categories**\n"
                "💬 **20+ Channels** (with proper permissions)\n"
                "👥 **4 Roles** (Member, Cashier, Moderator, Admin)\n"
                "⚙️ **Auto-configure** all bot settings\n\n"
                "**Existing channels/roles won't be deleted.**\n"
                "New items will be added alongside existing ones.\n\n"
                "⏱️ Takes approximately **30–60 seconds**."
            ),
            color=0xe67e22,
        )
        embed.add_field(
            name="📁 Categories to create",
            value=(
                "🏠 Welcome · 🎰 Casino · 💰 Finance\n"
                "🔒 Staff · 🏦 Private Rooms · 📥 Deposit Tickets"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ Bot settings auto-wired",
            value=(
                "Registration, Withdraw, Deposit, Live Stats,\n"
                "Fairness Log, Sweep Log, Crypto Logs, Roles…"
            ),
            inline=False,
        )
        embed.set_footer(text="Only the server owner can confirm this action.")

        await interaction.response.send_message(
            embed=embed,
            view=SetupConfirmView(interaction.user.id),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerSetup(bot))
