"""Remote Admin Cog — Owner-only bot management via Discord DMs.

Usage (DM the client bot or use in any channel):
  .rs             → status, uptime, ping, last commits
  .rp             → git pull
  .rsh <command>  → run any shell command
  .rr             → restart bot process
  .rl [N]         → last N log lines (default: 30)
  .rh             → show this help

Set OWNER_ID=<your_discord_id> in the client bot's .env.
Only that user ID can use any of these commands.
"""

import discord
from discord.ext import commands
import subprocess
import platform
import time
import os
import sys

from dotenv import load_dotenv
load_dotenv()

_OWNER_ID = int(os.getenv("OWNER_ID", "0"))


def _is_owner(user_id: int) -> bool:
    return _OWNER_ID != 0 and user_id == _OWNER_ID


class RemoteAdmin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._start_time = time.time()

    # ── Owner guard ────────────────────────────────────────────────────────────

    async def cog_check(self, ctx: commands.Context) -> bool:
        """All commands in this cog are owner-only. Silently ignore others."""
        return _is_owner(ctx.author.id)

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        """Suppress CheckFailure so non-owners get no response."""
        if isinstance(error, commands.CheckFailure):
            return
        raise error

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.command(name="rs", aliases=["radmin_status"])
    async def status(self, ctx: commands.Context):
        """Bot status: uptime, ping, servers, last commits."""
        uptime_sec = int(time.time() - self._start_time)
        h, rem = divmod(uptime_sec, 3600)
        m, s = divmod(rem, 60)

        embed = discord.Embed(title="🤖  Remote Bot Status", color=0x00FF88,
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="⏱ Uptime",   value=f"`{h}h {m}m {s}s`",               inline=True)
        embed.add_field(name="📶 Ping",    value=f"`{round(self.bot.latency*1000)}ms`", inline=True)
        embed.add_field(name="🌐 Servers", value=f"`{len(self.bot.guilds)}`",          inline=True)
        embed.add_field(name="🐍 Python",  value=f"`{platform.python_version()}`",    inline=True)
        embed.add_field(name="💻 OS",      value=f"`{platform.system()} {platform.release()}`", inline=True)

        # CWD
        embed.add_field(name="📁 CWD", value=f"`{os.getcwd()}`", inline=False)

        # Last 3 commits
        try:
            r = subprocess.run(
                ["git", "log", "--oneline", "-3"],
                capture_output=True, text=True, timeout=5,
            )
            if r.stdout.strip():
                embed.add_field(name="📝 Last Commits",
                                value=f"```{r.stdout.strip()}```", inline=False)
        except Exception:
            pass

        # Current branch
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            embed.add_field(name="🌿 Branch", value=f"`{r.stdout.strip()}`", inline=True)
        except Exception:
            pass

        embed.set_footer(text=f"Bot: {self.bot.user}")
        await ctx.send(embed=embed)

    @commands.command(name="rp", aliases=["radmin_pull"])
    async def git_pull(self, ctx: commands.Context):
        """Run git pull and show output."""
        msg = await ctx.send("⏳ Running `git pull`…")
        try:
            r = subprocess.run(
                ["git", "pull"],
                capture_output=True, text=True, timeout=30,
            )
            output = r.stdout.strip() or r.stderr.strip() or "(no output)"
            color  = 0x00FF88 if r.returncode == 0 else 0xFF4444
            embed  = discord.Embed(
                title=f"{'✅' if r.returncode == 0 else '❌'}  Git Pull",
                description=f"```{output[:1900]}```",
                color=color,
            )
            embed.add_field(name="Return code", value=f"`{r.returncode}`", inline=True)
            await msg.edit(content=None, embed=embed)
        except subprocess.TimeoutExpired:
            await msg.edit(content="❌ `git pull` timed out (30s).")
        except Exception as e:
            await msg.edit(content=f"❌ Error: `{e}`")

    @commands.command(name="rsh", aliases=["radmin_shell"])
    async def shell(self, ctx: commands.Context, *, command: str):
        """Run any shell command. Output returned as embed."""
        msg = await ctx.send(f"⏳ `{command[:120]}`")
        try:
            r = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=60,
            )
            stdout = r.stdout.strip()
            stderr = r.stderr.strip()
            output = stdout or stderr or "(no output)"
            color  = 0x00FF88 if r.returncode == 0 else 0xFF4444

            embed = discord.Embed(
                title="💻  Shell Output",
                description=f"```{output[:1900]}```",
                color=color,
            )
            embed.add_field(name="Command",     value=f"`{command[:200]}`", inline=False)
            embed.add_field(name="Return code", value=f"`{r.returncode}`",  inline=True)
            if stderr and stdout:
                embed.add_field(name="stderr", value=f"```{stderr[:500]}```", inline=False)
            await msg.edit(content=None, embed=embed)
        except subprocess.TimeoutExpired:
            await msg.edit(content="❌ Command timed out (60s limit).")
        except Exception as e:
            await msg.edit(content=f"❌ Error: `{e}`")

    @commands.command(name="rr", aliases=["radmin_restart"])
    async def restart(self, ctx: commands.Context):
        """Restart the bot process (uses os.execv — works with pm2/systemd)."""
        await ctx.send("🔄 Restarting…")
        await self.bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    @commands.command(name="rl", aliases=["radmin_logs"])
    async def logs(self, ctx: commands.Context, lines: int = 30):
        """Show last N lines from bot.log (default 30)."""
        log_file = "bot.log"
        if not os.path.exists(log_file):
            return await ctx.send("❌ No `bot.log` found in CWD.")
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.readlines()
            last = "".join(content[-max(1, min(lines, 200)):]).strip()
            if not last:
                return await ctx.send("📋 Log file is empty.")
            # Send in chunks if long
            for i in range(0, min(len(last), 5700), 1900):
                await ctx.send(f"```{last[i:i+1900]}```")
        except Exception as e:
            await ctx.send(f"❌ Error reading log: `{e}`")

    @commands.command(name="rh", aliases=["radmin_help"])
    async def help_cmd(self, ctx: commands.Context):
        """Show all remote admin commands."""
        p = ctx.prefix
        embed = discord.Embed(title="🛠️  Remote Admin Commands", color=0x9945FF)
        embed.add_field(name=f"`{p}rs`  /  `{p}radmin_status`",
                        value="Bot status, uptime, ping, last commits", inline=False)
        embed.add_field(name=f"`{p}rp`  /  `{p}radmin_pull`",
                        value="Run `git pull`", inline=False)
        embed.add_field(name=f"`{p}rsh <cmd>`  /  `{p}radmin_shell <cmd>`",
                        value="Run any shell command (60s timeout)", inline=False)
        embed.add_field(name=f"`{p}rr`  /  `{p}radmin_restart`",
                        value="Restart the bot process", inline=False)
        embed.add_field(name=f"`{p}rl [N]`  /  `{p}radmin_logs [N]`",
                        value="Show last N lines of `bot.log` (max 200)", inline=False)
        embed.set_footer(text="Only works for the OWNER_ID set in .env")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(RemoteAdmin(bot))
