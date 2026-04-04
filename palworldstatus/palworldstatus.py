import discord
from redbot.core import commands, Config
from discord.ext import tasks
import aiohttp
import base64
from datetime import datetime

class PalworldStatusV3(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=987654321)

        self.config.register_global(
            api_url=None,
            admin_password=None,
            message_id=None,
            channel_id=None,
            last_online=None,
            cached_name="Unknown Server"
        )

    def cog_load(self):
        self.update_loop.start()

    def cog_unload(self):
        self.update_loop.cancel()

    # -------------------------
    # SETUP
    # -------------------------
    @commands.command()
    async def pwsetup(self, ctx, api_url: str):
        await self.config.api_url.set(api_url)

        await ctx.author.send("🔐 Bitte sende das Admin Passwort:")

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
            title="🚀 Initialisiere Serverstatus...",
            color=discord.Color.orange()
        )

        message = await channel.send(embed=embed)

        await self.config.channel_id.set(channel.id)
        await self.config.message_id.set(message.id)

        await ctx.send("✅ Embed läuft.")

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

        await self.config.message_id.set(None)
        await self.config.channel_id.set(None)

        await ctx.send("🛑 Embed gestoppt.")

    # -------------------------
    # HELPER
    # -------------------------
    def make_bar(self, current, max_players, length=12):
        if max_players == 0:
            return "░" * length

        ratio = current / max_players
        filled = int(ratio * length)
        return "█" * filled + "░" * (length - filled)

    def format_uptime(self, seconds):
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02}:{m:02}:{s:02}"

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

        # 🔐 Auth Fix
        auth = base64.b64encode(f"admin:{password}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}

        try:
            async with aiohttp.ClientSession() as session:

                # INFO
                async with session.get(f"{api_url}/v1/api/info", headers=headers, timeout=10) as r:
                    info = await r.json()

                # METRICS
                async with session.get(f"{api_url}/v1/api/metrics", headers=headers, timeout=10) as r:
                    metrics = await r.json()

                # PLAYERS (failsafe + correct structure)
                try:
                    async with session.get(f"{api_url}/v1/api/players", headers=headers, timeout=10) as r:
                        pdata = await r.json()
                        players = pdata.get("players", [])
                        if not isinstance(players, list):
                            players = []
                except:
                    players = []

            now = datetime.utcnow()

            server_name = info.get("servername", "Unknown Server")
            version = info.get("version", "Unknown")

            current = metrics.get("currentplayernum", len(players))
            max_players = metrics.get("maxplayernum", 0)
            uptime = self.format_uptime(metrics.get("uptime", 0))

            await self.config.cached_name.set(server_name)
            await self.config.last_online.set(now.isoformat())

            percent = int((current / max_players) * 100) if max_players else 0

            # 🔥 Spielerliste (max 20)
            if players:
                player_lines = [
                    f"👤 `{p.get('name','Unknown')} (Lv.{p.get('level','?')})`"
                    for p in players[:20]
                ]
                player_text = "\n".join(player_lines)
            else:
                player_text = "`Keine Spieler online`"

            embed = discord.Embed(
                title=f"🟢 {server_name}",
                description=f"**Version:** `{version}`",
                color=discord.Color.green()
            )

            embed.add_field(
                name="📊 Auslastung",
                value=f"`{self.make_bar(current, max_players)}`\n**{current}/{max_players} ({percent}%)**",
                inline=False
            )

            embed.add_field(
                name="⚡ Status",
                value=f"🟢 Online\n⏱ `{uptime}`",
                inline=True
            )

            embed.add_field(
                name="🕒 Letztes Update",
                value=f"<t:{int(now.timestamp())}:R>",
                inline=True
            )

            embed.add_field(
                name="👥 Spieler",
                value=player_text,
                inline=False
            )

            embed.set_footer(text="Palworld Server Monitor • Update alle 60s")
            embed.timestamp = now

        except Exception as e:
            print("ERROR:", e)

            cached_name = await self.config.cached_name()

            embed = discord.Embed(
                title=f"🔴 {cached_name}",
                description="```SERVER OFFLINE```",
                color=discord.Color.red()
            )

        await message.edit(embed=embed)

    @update_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
