from .topgamesvotes import TopGamesVotes


async def setup(bot):
    await bot.add_cog(TopGamesVotes(bot))
