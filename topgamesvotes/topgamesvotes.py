import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

API_BASE = "https://api.top-games.net/v1/servers"

DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_TOP_COUNT = 10
DEFAULT_LAST_MONTH_COUNT = 3


class TopGamesVotes(commands.Cog):
    """Live leaderboard embed for top-games.net voter rankings.

    Polls one or more top-games.net API keys, sums the vote counts of
    every player across all keys, stores the aggregated results locally
    and keeps a Discord embed in a configured channel updated on a
    timer.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x746F7076, force_registration=True)

        self.config.register_global(
            api_keys=[],
            channel_id=None,
            message_id=None,
            update_interval=DEFAULT_INTERVAL_MINUTES,
            top_count=DEFAULT_TOP_COUNT,
            last_month_count=DEFAULT_LAST_MONTH_COUNT,
            last_refresh=None,
            next_refresh=None,
            enabled=False,
        )

        self.session: aiohttp.ClientSession = None
        self._data_dir = cog_data_path(self)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._current_file = self._data_dir / "current_votes.json"
        self._last_month_file = self._data_dir / "last_month_votes.json"

    # -------------------------------------------------------------
    # LIFECYCLE
    # -------------------------------------------------------------
    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        interval = await self.config.update_interval()
        self.update_loop.change_interval(minutes=interval)
        if await self.config.enabled():
            self.update_loop.start()

    async def cog_unload(self):
        self.update_loop.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    # -------------------------------------------------------------
    # LOCAL FILE HELPERS
    # -------------------------------------------------------------
    def _write_json(self, path, rankings, fetched_at: datetime):
        payload = {
            "fetched_at": fetched_at.isoformat(),
            "rankings": [
                {"placement": i, "playername": name, "votes": votes}
                for i, (name, votes) in enumerate(rankings, start=1)
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)

    def _read_json(self, path):
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    # -------------------------------------------------------------
    # API HELPERS
    # -------------------------------------------------------------
    async def _fetch_players(self, api_key: str, last_month: bool = False):
        """Fetch the players-ranking array for a single API key.

        Returns a list of {"votes": int, "playername": str} dicts, or
        None if the request failed.
        """
        url = f"{API_BASE}/{api_key}/players-ranking"
        if last_month:
            url += "?type=lastMonth"

        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return None

                if not data.get("success"):
                    return None

                players = data.get("players", [])
                if not isinstance(players, list):
                    return None
                return players
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def _aggregate(self, api_keys, last_month: bool = False):
        """Query every API key and sum vote counts per player name.

        Returns a list of (playername, total_votes) tuples sorted by
        vote count descending, plus the number of keys that failed.
        """
        totals = {}
        failures = 0

        for key in api_keys:
            players = await self._fetch_players(key, last_month=last_month)
            if players is None:
                failures += 1
                continue

            for entry in players:
                name = str(entry.get("playername", "Unknown"))
                votes = entry.get("votes", 0)
                try:
                    votes = int(votes)
                except (TypeError, ValueError):
                    votes = 0
                totals[name] = totals.get(name, 0) + votes

        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        return ranked, failures

    # -------------------------------------------------------------
    # EMBED BUILDING
    # -------------------------------------------------------------
    @staticmethod
    def _format_table(rankings, limit):
        if not rankings:
            return "```\nNo data available yet.\n```"

        rows = rankings[:limit]
        header = f"{'#':<4}{'Votes':<8}Player"
        lines = [header, "-" * len(header)]
        for i, (name, votes) in enumerate(rows, start=1):
            display_name = name if len(name) <= 24 else name[:21] + "..."
            lines.append(f"{i:<4}{votes:<8}{display_name}")

        return "```\n" + "\n".join(lines) + "\n```"

    @staticmethod
    def _format_last_month(rankings, limit):
        if not rankings:
            return "No data available yet."

        medals = ["🥇", "🥈", "🥉", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅"]
        lines = []
        for i, (name, votes) in enumerate(rankings[:limit]):
            medal = medals[i] if i < len(medals) else "🏅"
            lines.append(f"{medal} **{name}** — {votes} votes")
        return "\n".join(lines)

    def _build_embed(self, current_rankings, last_month_rankings, top_count,
                      last_month_count, last_refresh: datetime, next_refresh: datetime,
                      failures: int = 0):
        embed = discord.Embed(
            title="🏆 Top Voters Leaderboard",
            color=discord.Color.gold(),
        )

        embed.add_field(
            name=f"Top {top_count} Voters (This Month)",
            value=self._format_table(current_rankings, top_count),
            inline=False,
        )

        embed.add_field(
            name=f"Top {last_month_count} Voters (Last Month)",
            value=self._format_last_month(last_month_rankings, last_month_count),
            inline=False,
        )

        if failures:
            embed.add_field(
                name="⚠️ Warning",
                value=f"{failures} API key(s) failed to respond during the last refresh.",
                inline=False,
            )

        footer = (
            f"Last refresh: {last_refresh.strftime('%Y-%m-%d %H:%M UTC')} | "
            f"Next update: {next_refresh.strftime('%Y-%m-%d %H:%M UTC')}"
        )
        embed.set_footer(text=footer)
        embed.timestamp = last_refresh

        return embed

    # -------------------------------------------------------------
    # CORE REFRESH LOGIC
    # -------------------------------------------------------------
    async def _refresh(self):
        api_keys = await self.config.api_keys()
        channel_id = await self.config.channel_id()

        if not api_keys or not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return

        current_rankings, current_failures = await self._aggregate(api_keys, last_month=False)
        last_month_rankings, last_month_failures = await self._aggregate(api_keys, last_month=True)

        now = datetime.now(timezone.utc)
        self._write_json(self._current_file, current_rankings, now)
        self._write_json(self._last_month_file, last_month_rankings, now)

        interval = await self.config.update_interval()
        next_refresh = now + timedelta(minutes=interval)

        top_count = await self.config.top_count()
        last_month_count = await self.config.last_month_count()
        total_failures = current_failures + last_month_failures

        embed = self._build_embed(
            current_rankings, last_month_rankings, top_count, last_month_count,
            now, next_refresh, failures=total_failures,
        )

        message_id = await self.config.message_id()
        message = None
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None

        if message is None:
            message = await channel.send(embed=embed)
            await self.config.message_id.set(message.id)
        else:
            await message.edit(embed=embed)

        await self.config.last_refresh.set(now.isoformat())
        await self.config.next_refresh.set(next_refresh.isoformat())

    @tasks.loop(minutes=DEFAULT_INTERVAL_MINUTES)
    async def update_loop(self):
        await self._refresh()

    @update_loop.before_loop
    async def before_update_loop(self):
        await self.bot.wait_until_ready()

    @update_loop.error
    async def update_loop_error(self, error):
        print(f"[TopGamesVotes] update loop error: {error!r}")
        if not self.update_loop.is_running():
            self.update_loop.start()

    # -------------------------------------------------------------
    # ADMIN COMMANDS
    # -------------------------------------------------------------
    @commands.group(name="tgvotes")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def tgvotes(self, ctx: commands.Context):
        """Manage the top-games.net voter leaderboard."""

    @tgvotes.group(name="apikey")
    async def tgvotes_apikey(self, ctx: commands.Context):
        """Manage the configured top-games.net API keys."""

    @staticmethod
    def _mask_key(key: str) -> str:
        """Mask an API key for safe display, keeping only a few edge chars."""
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"

    def _dm_check(self, ctx: commands.Context):
        def check(m: discord.Message):
            return m.author.id == ctx.author.id and isinstance(m.channel, discord.DMChannel)
        return check

    async def _nudge_to_dm(self, ctx: commands.Context, note: str):
        """Delete the invoking message (if in a guild) and point the user to their DMs."""
        if ctx.guild is not None:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
            await ctx.send(f"{ctx.author.mention} {note}", delete_after=15)

    @tgvotes_apikey.command(name="add")
    async def tgvotes_apikey_add(self, ctx: commands.Context):
        """Add a top-games.net server API key.

        API keys are secrets, so this never asks for them in a public
        channel. Run the command and reply in your DMs instead.
        """
        try:
            await ctx.author.send(
                "Please reply here with the top-games.net API key you want to add. "
                "This request expires in 2 minutes."
            )
        except discord.Forbidden:
            await self._nudge_to_dm(ctx, "I can't DM you. Please enable DMs from server members and try again.")
            return

        await self._nudge_to_dm(ctx, "Check your DMs to add an API key.")

        try:
            msg = await self.bot.wait_for("message", check=self._dm_check(ctx), timeout=120)
        except asyncio.TimeoutError:
            await ctx.author.send("⌛ Timed out waiting for the API key. Please run the command again.")
            return

        api_key = msg.content.strip()
        if not api_key:
            await ctx.author.send("That didn't look like a valid API key. Please run the command again.")
            return

        async with self.config.api_keys() as keys:
            if api_key in keys:
                await ctx.author.send("That API key is already configured.")
                return
            keys.append(api_key)

        count = len(await self.config.api_keys())
        await ctx.author.send(f"✅ API key `{self._mask_key(api_key)}` added. {count} key(s) now configured.")

    @tgvotes_apikey.command(name="remove")
    async def tgvotes_apikey_remove(self, ctx: commands.Context):
        """Remove a configured API key via a private DM prompt."""
        keys = await self.config.api_keys()
        if not keys:
            await ctx.send("No API keys configured yet.")
            return

        listing = "\n".join(f"{i}. `{self._mask_key(k)}`" for i, k in enumerate(keys, start=1))
        prompt = f"Which API key do you want to remove? Reply with its number:\n{listing}"

        try:
            await ctx.author.send(prompt)
        except discord.Forbidden:
            await self._nudge_to_dm(ctx, "I can't DM you. Please enable DMs from server members and try again.")
            return

        await self._nudge_to_dm(ctx, "Check your DMs to remove an API key.")

        try:
            msg = await self.bot.wait_for("message", check=self._dm_check(ctx), timeout=120)
        except asyncio.TimeoutError:
            await ctx.author.send("⌛ Timed out. Please run the command again.")
            return

        current_keys = await self.config.api_keys()
        try:
            idx = int(msg.content.strip()) - 1
            if idx < 0 or idx >= len(current_keys):
                raise ValueError
        except ValueError:
            await ctx.author.send("Invalid selection. Please run the command again.")
            return

        removed = current_keys[idx]
        async with self.config.api_keys() as keys2:
            keys2.pop(idx)

        await ctx.author.send(f"✅ API key `{self._mask_key(removed)}` removed.")

    @tgvotes_apikey.command(name="list")
    async def tgvotes_apikey_list(self, ctx: commands.Context):
        """List configured API keys, sent privately via DM."""
        keys = await self.config.api_keys()
        if not keys:
            message = "No API keys configured yet."
        else:
            formatted = "\n".join(f"- `{self._mask_key(k)}`" for k in keys)
            message = f"Configured API keys ({len(keys)}):\n{formatted}"

        try:
            await ctx.author.send(message)
        except discord.Forbidden:
            await self._nudge_to_dm(ctx, "I can't DM you. Please enable DMs from server members and try again.")
            return

        await self._nudge_to_dm(ctx, "Check your DMs for the API key list.")

    @tgvotes.command(name="channel")
    async def tgvotes_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel the leaderboard embed is posted/updated in."""
        await self.config.channel_id.set(channel.id)
        await self.config.message_id.set(None)
        await ctx.send(f"Output channel set to {channel.mention}. A new embed will be posted on the next refresh.")

    @tgvotes.command(name="interval")
    async def tgvotes_interval(self, ctx: commands.Context, minutes: int):
        """Set how often (in minutes) the leaderboard refreshes."""
        if minutes < 1:
            await ctx.send("The interval must be at least 1 minute.")
            return
        await self.config.update_interval.set(minutes)
        self.update_loop.change_interval(minutes=minutes)
        await ctx.send(f"Update interval set to {minutes} minute(s).")

    @tgvotes.command(name="topcount")
    async def tgvotes_topcount(self, ctx: commands.Context, count: int):
        """Set how many current-month voters are shown (default 10)."""
        if count < 1 or count > 25:
            await ctx.send("Please choose a value between 1 and 25.")
            return
        await self.config.top_count.set(count)
        await ctx.send(f"Current-month leaderboard size set to top {count}.")

    @tgvotes.command(name="lastmonthcount")
    async def tgvotes_lastmonthcount(self, ctx: commands.Context, count: int):
        """Set how many last-month voters are shown (default 3)."""
        if count < 1 or count > 10:
            await ctx.send("Please choose a value between 1 and 10.")
            return
        await self.config.last_month_count.set(count)
        await ctx.send(f"Last-month leaderboard size set to top {count}.")

    @tgvotes.command(name="start")
    async def tgvotes_start(self, ctx: commands.Context):
        """Start (or resume) the automatic leaderboard updates."""
        api_keys = await self.config.api_keys()
        channel_id = await self.config.channel_id()

        if not api_keys:
            await ctx.send("Add at least one API key first with `[p]tgvotes apikey add <key>`.")
            return
        if not channel_id:
            await ctx.send("Set an output channel first with `[p]tgvotes channel <#channel>`.")
            return

        await self.config.enabled.set(True)
        if not self.update_loop.is_running():
            self.update_loop.start()
        else:
            await self._refresh()
        await ctx.send("✅ Leaderboard updates started.")

    @tgvotes.command(name="stop")
    async def tgvotes_stop(self, ctx: commands.Context):
        """Stop the automatic leaderboard updates."""
        await self.config.enabled.set(False)
        self.update_loop.cancel()
        await ctx.send("🛑 Leaderboard updates stopped.")

    @tgvotes.command(name="forceupdate")
    async def tgvotes_forceupdate(self, ctx: commands.Context):
        """Trigger an immediate refresh of the leaderboard."""
        api_keys = await self.config.api_keys()
        channel_id = await self.config.channel_id()

        if not api_keys:
            await ctx.send("Add at least one API key first with `[p]tgvotes apikey add <key>`.")
            return
        if not channel_id:
            await ctx.send("Set an output channel first with `[p]tgvotes channel <#channel>`.")
            return

        async with ctx.typing():
            await self._refresh()
        await ctx.send("✅ Leaderboard refreshed.")

    @tgvotes.command(name="status")
    async def tgvotes_status(self, ctx: commands.Context):
        """Show the current configuration."""
        api_keys = await self.config.api_keys()
        channel_id = await self.config.channel_id()
        interval = await self.config.update_interval()
        top_count = await self.config.top_count()
        last_month_count = await self.config.last_month_count()
        enabled = await self.config.enabled()
        last_refresh = await self.config.last_refresh()
        next_refresh = await self.config.next_refresh()

        channel = self.bot.get_channel(channel_id) if channel_id else None

        embed = discord.Embed(title="TopGamesVotes Configuration", color=discord.Color.blue())
        embed.add_field(name="Enabled", value=str(enabled), inline=True)
        embed.add_field(name="API keys", value=str(len(api_keys)), inline=True)
        embed.add_field(name="Interval", value=f"{interval} min", inline=True)
        embed.add_field(name="Output channel", value=channel.mention if channel else "Not set", inline=True)
        embed.add_field(name="Top count (current)", value=str(top_count), inline=True)
        embed.add_field(name="Top count (last month)", value=str(last_month_count), inline=True)
        embed.add_field(name="Last refresh", value=last_refresh or "Never", inline=False)
        embed.add_field(name="Next refresh", value=next_refresh or "N/A", inline=False)

        await ctx.send(embed=embed)
