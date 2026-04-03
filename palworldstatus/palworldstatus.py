import discord
from redbot.core import commands, Config
from discord.ext import tasks
import aiohttp
from datetime import datetime

class PalworldStatusV3(commands.Cog):
    """Palworld Server Status V3 - Overkill UI"""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1122334455)

        default_global = {
            "api_url": None,
            "admin_password": None,
            "message_id": None,
            "channel_id": None,
            "last_online": None,
            "cached_name": "Unknown Server",
            "server_start": None
        }

        self.config.register_global(**default_global)
        self.update_loop.start()

    def cog_unload(self):
        self.update_loop.cancel()

    # -------------------------
    # SETUP
    # -------------------------
    @commands.command()
    async def pwsetup(self, ctx, api_url: str):
        await self.config.api_url.set(api_url)

        await ctx.author.send("🔐 Bitte sende mir das Admin Passwort:")

        def check(m):
            return m.author == ctx.author and isinstance(m.channel, discord.DMChannel)

        msg = await self.bot.wait_for("message", check=check, timeout=120)
        await self.config.admin_password.set(msg.content)

        await ctx.send("✅ Setup abgeschlossen.")

    # -------------------------
    # START
    # -------------------------
    @commands.command()
    async def pwstart(self, ctx, channel: discord.TextChannel):
        embed = discord.Embed(
            title="🚀 Starte Monitoring...",
            color=discord.Color.orange()
        )

        msg = await channel.send(embed=embed)

        await self.config.channel_id.set(channel.id)
        await self.config.message_id.set(msg.id)

        await ctx.send("✅ Monitoring aktiv.")

    # -------------------------
    # STOP
    # -------------------------
    @commands.command()
    async def pwstop(self, ctx):
        channel_id = await self.config.channel_id()
        message_id = await self.config.message_id()

        if channel_id and message_id:
            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.delete()
                except:
                    pass

        await self.config.channel_id.set(None)
        await self.config.message_id.set(None)

        await ctx.send("🛑 Monitoring gestoppt.")

    # -------------------------
    # HELPERS
    # -------------------------
    def make_bar(self, current, max_players, length=16):
        if max_players == 0:
            return "░" * length

        ratio = current / max_players
        filled = int(ratio * length)
        return "█" * filled + "░" * (length - filled)

    def get_color(self, current, max_players):
        if max_players == 0:
            return discord.Color.greyple()

        ratio = current / max_players

        if ratio < 0.5:
            return discord.Color.green()
        elif ratio < 0.8:
            return discord.Color.gold()
        else:
            return discord.Color.red()

    # -------------------------
    # LOOP
    # -------------------------
    @tasks.loop(seconds=60)
    async def update_loop(self):
        api_url = await self.config.api_url()
        password = await self.config.admin_password()
        channel_id = await self.config.channel_id()
        message_id = await self.config.message_id()

        if not all([api_url, password, channel_id, message_id]):
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        try:
            message = await channel.fetch_message(message_id)
        except:
            return

        headers = {"Authorization": f"Basic {password}"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{api_url}/v1/api/info", headers=headers, timeout=10) as r:
                    info = await r.json()

                async with session.get(f"{api_url}/v1/api/players", headers=headers, timeout=10) as r:
                    players = await r.json()

            now = datetime.utcnow()

            name = info.get("servername", "Unknown")
            version = info.get("version", "Unknown")
            current = info.get("numplayers", 0)
            max_players = info.get("maxplayers", 0)

            # Uptime
            if not await self.config.server_start():
                await self.config.server_start.set(now.isoformat())

            start = datetime.fromisoformat(await self.config.server_start())
            uptime = str(now - start).split(".")[0]

            # Cache
            await self.config.cached_name.set(name)
            await self.config.last_online.set(now.isoformat())

            # Progressbar
            bar = self.make_bar(current, max_players)
            percent = int((current / max_players) * 100) if max_players else 0

            # Spieler Tabelle
            if players:
                lines = [f"{p.get('name')[:18]:<18}" for p in players]
                player_table = "```" + "\n".join(lines) + "```"
            else:
                player_table = "`Keine Spieler online`"

            embed = discord.Embed(
                title=f"🟢 {name}",
                description=f"**Version:** `{version}`",
                color=self.get_color(current, max_players)
            )

            embed.add_field(
                name="📊 Auslastung",
                value=f"`{bar}`\n**{current}/{max_players} ({percent}%)**",
                inline=False
            )

            embed.add_field(
                name="⚡ Status",
                value=f"🟢 Online\n⏱ Uptime: `{uptime}`",
                inline=True
            )

            embed.add_field(
                name="🕒 Letztes Update",
                value=f"<t:{int(now.timestamp())}:R>",
                inline=True
            )

            embed.add_field(
                name="👥 Spieler",
                value=player_table,
                inline=False
            )

            embed.set_footer(text="Palworld Monitor V3 • Ultra UI")
            embed.timestamp = now

        except Exception:
            cached_name = await self.config.cached_name()
            last_online = await self.config.last_online()

            if last_online:
                last = datetime.fromisoformat(last_online)
                offline = datetime.utcnow() - last
                offline_str = str(offline).split(".")[0]
                last_seen = int(last.timestamp())
            else:
                offline_str = "Unbekannt"
                last_seen = None

            embed = discord.Embed(
                title=f"🔴 {cached_name}",
                description="**SERVER OFFLINE**",
                color=discord.Color.dark_red()
            )

            value = f"⏱ Offline seit: `{offline_str}`"
            if last_seen:
                value += f"\n🕒 Last Seen: <t:{last_seen}:R>"

            embed.add_field(
                name="📉 Status",
                value=value,
                inline=False
            )

            embed.set_footer(text="Letzter bekannter Zustand")

        await message.edit(embed=embed)

    @update_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()