import asyncio
import discord

async def check_for_inactivity(channel, bot, is_24_7=False):
    """
    Check if the bot is alone in the voice channel and disconnect after 5 minutes of inactivity.
    """
    if is_24_7:
        return 
    while True:
        vc = bot.get_guild(channel.guild.id).voice_client
        if vc and vc.is_connected():
            members = vc.channel.members
            only_bot = len(members) == 1 and members[0].id == bot.user.id

            if only_bot and not vc.is_playing():
                await channel.send("😴 No one is in the VC. Waiting 5 minutes...")
                await asyncio.sleep(5 * 60)

                vc = bot.get_guild(channel.guild.id).voice_client
                if vc and vc.is_connected():
                    members = vc.channel.members
                    only_bot = len(members) == 1 and members[0].id == bot.user.id

                    if only_bot and not vc.is_playing():
                        await channel.send("👋 Leaving due to inactivity.")
                        await vc.disconnect()
                        break
        else:
            break

        await asyncio.sleep(30)