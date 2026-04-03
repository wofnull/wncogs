from .palworldstatus import PalworldStatusV3

async def setup(bot):
    await bot.add_cog(PalworldStatusV3(bot))