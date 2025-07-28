import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
from collections import deque
import asyncio
import shutil
import logging
import signal
import sys


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")


SONG_QUEUES = {}
volume_settings = {}
loop_mode = {}
is_24_7 = {}
current_songs = {}


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="`", intents=intents)


def signal_handler(sig, frame):
    logging.info("Shutting down bot...")
    asyncio.run(bot.close())
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)


async def check_for_inactivity(channel, bot, is_24_7_mode):
    try:
        if is_24_7_mode:
            return
        await asyncio.sleep(300) 
        if (
            channel.guild.voice_client
            and not channel.guild.voice_client.is_playing()
            and not channel.guild.voice_client.is_paused()
            and not SONG_QUEUES.get(str(channel.guild.id))
        ):
            await channel.guild.voice_client.disconnect()
            await channel.send("Disconnected due to inactivity.")
    except Exception as e:
        logging.error(f"Error in check_for_inactivity: {e}")


async def search_ytdlp_async(query, ydl_opts):
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))
    except Exception as e:
        logging.error(f"yt_dlp error: {e}")
        return None

def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)


async def play_next_song(voice_client, guild_id, channel):
    try:
        if loop_mode.get(guild_id, False) and voice_client.source and current_songs.get(guild_id):
            
            audio_url, title = current_songs[guild_id]["url"], current_songs[guild_id]["title"]
        elif SONG_QUEUES.get(guild_id) and SONG_QUEUES[guild_id]:
            audio_url, title = SONG_QUEUES[guild_id].popleft()
            current_songs[guild_id] = {"url": audio_url, "title": title}
        else:
            current_songs.pop(guild_id, None)
            await check_for_inactivity(channel, bot, is_24_7.get(guild_id, False))
            return

        ffmpeg_options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 20",
            "options": "-vn -b:a 256k -ac 2 -ar 48000 -filter:a aresample=48000"
        }
        ffmpeg_executable = shutil.which("ffmpeg") or "bin\\ffmpeg\\ffmpeg.exe"
        base_audio = discord.FFmpegPCMAudio(audio_url, **ffmpeg_options, executable=ffmpeg_executable)
        volume = volume_settings.get(guild_id, 1.0)
        source = discord.PCMVolumeTransformer(base_audio, volume=volume)

        def after_play(error):
            if error:
                logging.error(f"Error playing {title}: {error}")
            asyncio.run_coroutine_threadsafe(play_next_song(voice_client, guild_id, channel), bot.loop)

        voice_client.play(source, after=after_play)
        await channel.send(f"🎶 Now playing: **{title}**")
    except asyncio.exceptions.CancelledError:
        logging.info("Playback task cancelled.")
        return
    except Exception as e:
        logging.error(f"Error in play_next_song: {e}")
        await channel.send("An error occurred while playing the next song.")

@bot.event
async def on_ready():
    await bot.tree.sync()
    logging.info(f"{bot.user} is online!")

async def connect_to_voice(voice_channel, voice_client):
    max_retries = 4
    base_delay = 5
    for attempt in range(max_retries):
        try:
            if voice_client is None:
                voice_client = await voice_channel.connect(timeout=60.0, reconnect=True)
            elif voice_channel != voice_client.channel:
                await voice_client.move_to(voice_channel)
            return voice_client
        except discord.errors.ConnectionClosed as e:
            logging.error(f"ConnectionClosed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                await asyncio.sleep(delay)
            else:
                raise
        except Exception as e:
            logging.error(f"Join error (attempt {attempt + 1}/{max_retries}): {e}")
            raise

@bot.tree.command(name="play", description="Play a song from YouTube link or search query.")
@app_commands.describe(song_query="YouTube link or search term")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()

    if not (interaction.user.voice and interaction.user.voice.channel):
        return await interaction.followup.send("You must be in a voice channel.")

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    try:
        voice_client = await connect_to_voice(voice_channel, voice_client)
        volume_settings[str(interaction.guild_id)] = 1.0
    except Exception as e:
        logging.error(f"Failed to connect: {e}")
        return await interaction.followup.send("Unable to connect to your voice channel.")

    if "youtube.com/watch" in song_query or "youtu.be/" in song_query:
        query = song_query
    else:
        query = "ytsearch:" + song_query

    ydl_options = {
        "format": "bestaudio[acodec=opus]/bestaudio[acodec=aac]/bestaudio/best",
        "quiet": True,
        "default_search": "auto",
        "noplaylist": True,
        "source_address": "0.0.0.0",
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
        "geo_bypass": True,
        "proxy": "None",
    }

    try:
        results = await search_ytdlp_async(query, ydl_options)
        if not results:
            return await interaction.followup.send("Failed to fetch song data.")
    except Exception as e:
        logging.error(f"yt_dlp error: {e}")
        return await interaction.followup.send("Failed to fetch song data.")

    if "entries" in results:
        tracks = results.get("entries") or []
        if not tracks:
            return await interaction.followup.send("No results found for your query.")
        first = tracks[0]
    else:
        first = results

    audio_url = first["url"]
    title = first.get("title", "Untitled")

    guild_id = str(interaction.guild_id)
    if SONG_QUEUES.get(guild_id) is None:
        SONG_QUEUES[guild_id] = deque()
    SONG_QUEUES[guild_id].append((audio_url, title))

    if voice_client.is_playing() or voice_client.is_paused():
        await interaction.followup.send(f"Added to queue: **{title}**")
    else:
        await play_next_song(voice_client, guild_id, interaction.channel)

@bot.tree.command(name="pause", description="Pause the currently playing song.")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Playback paused.")
        if not SONG_QUEUES.get(str(interaction.guild_id)):
            await check_for_inactivity(interaction.channel, bot, is_24_7.get(str(interaction.guild_id), False))
    else:
        await interaction.response.send_message("Nothing is currently playing.")

@bot.tree.command(name="resume", description="Resume the currently paused song.")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Playback resumed.")
    else:
        await interaction.response.send_message("I’m not paused right now.")
        if vc and not vc.is_playing() and not SONG_QUEUES.get(str(interaction.guild_id)):
            await check_for_inactivity(interaction.channel, bot, is_24_7.get(str(interaction.guild_id), False))

@bot.tree.command(name="skip", description="Skips the current playing song.")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped the current song.")
    else:
        await interaction.response.send_message("Not playing anything to skip.")
        if vc and not vc.is_playing() and not SONG_QUEUES.get(str(interaction.guild_id)):
            await check_for_inactivity(interaction.channel, bot, is_24_7.get(str(interaction.guild_id), False))

@bot.tree.command(name="disconnect", description="Stop playback and disconnect.")
async def disconnect(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        guild_id = str(interaction.guild_id)
        SONG_QUEUES[guild_id] = deque()
        volume_settings.pop(guild_id, None)
        is_24_7.pop(guild_id, None)
        current_songs.pop(guild_id, None)
        await vc.disconnect()
        await interaction.response.send_message("👋 Disconnected and cleared the queue.")
    else:
        await interaction.response.send_message("I'm not connected to any voice channel.")

@bot.tree.command(name="join", description="Make the bot join your voice channel.")
async def join(interaction: discord.Interaction):
    if not (interaction.user.voice and interaction.user.voice.channel):
        await interaction.response.send_message("❌ You must be in a voice channel.")
        return

    try:
        vc = await connect_to_voice(interaction.user.voice.channel, interaction.guild.voice_client)
        volume_settings[str(interaction.guild_id)] = 1.0
        bitrate = interaction.user.voice.channel.bitrate // 1000
        if bitrate < 128:
            await interaction.response.send_message(
                f"✅ Joined your voice channel. ⚠️ Low bitrate ({bitrate} kbps) detected. Boost server for better audio (128+ kbps)."
            )
        else:
            await interaction.response.send_message("✅ Joined your voice channel.")
        if not vc.is_playing() and not SONG_QUEUES.get(str(interaction.guild_id)):
            await check_for_inactivity(interaction.channel, bot, is_24_7.get(str(interaction.guild_id), False))
    except Exception as e:
        logging.error(f"Join command error: {e}")
        await interaction.response.send_message("❌ Failed to join voice channel.")

@bot.tree.command(name="queue", description="View current song queue.")
async def view_queue(interaction: discord.Interaction):
    q = SONG_QUEUES.get(str(interaction.guild_id), [])
    if not q:
        await interaction.response.send_message("📭 The queue is currently empty.")
        vc = interaction.guild.voice_client
        if vc and not vc.is_playing():
            await check_for_inactivity(interaction.channel, bot, is_24_7.get(str(interaction.guild_id), False))
    else:
        lines = [f"{i+1}. {t}" for i, (_, t) in enumerate(q)]
        await interaction.response.send_message("🎶 Queue:\n" + "\n".join(lines))

@bot.tree.command(name="cleanqueue", description="Clear the entire queue.")
async def cleanqueue(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    SONG_QUEUES.get(gid, deque()).clear()
    await interaction.response.send_message("🧹 Queue has been cleared!")
    vc = interaction.guild.voice_client
    if vc and not vc.is_playing():
        await check_for_inactivity(interaction.channel, bot, is_24_7.get(gid, False))

@bot.tree.command(name="volume", description="Set volume between 0 and 200.")
@app_commands.describe(amount="Volume percentage 0-200")
async def volume(interaction: discord.Interaction, amount: int):
    if not (0 <= amount <= 200):
        return await interaction.response.send_message("❌ Volume must be between 0 and 200.")
    vid = str(interaction.guild_id)
    volume_settings[vid] = min(amount / 100, 2.0)
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = volume_settings[vid]
    await interaction.response.send_message(f"🔊 Volume set to {amount}%.")
    if vc and not vc.is_playing() and not SONG_QUEUES.get(vid):
        await check_for_inactivity(interaction.channel, bot, is_24_7.get(vid, False))

@bot.tree.command(name="loop", description="Toggle loop (repeat current song).")
async def loop(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    loop_mode[gid] = not loop_mode.get(gid, False)
    await interaction.response.send_message("🔁 Loop enabled." if loop_mode[gid] else "➡️ Loop disabled.")
    vc = interaction.guild.voice_client
    if vc and not vc.is_playing() and not SONG_QUEUES.get(gid):
        await check_for_inactivity(interaction.channel, bot, is_24_7.get(gid, False))

@bot.tree.command(name="nowplaying", description="Show current playing song.")
async def nowplaying(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    guild_id = str(interaction.guild_id)
    if not vc or not vc.is_playing():
        await interaction.response.send_message("Nothing is currently playing.")
        if vc and not SONG_QUEUES.get(guild_id):
            await check_for_inactivity(interaction.channel, bot, is_24_7.get(guild_id, False))
    else:
        title = current_songs.get(guild_id, {}).get("title", "Unknown")
        await interaction.response.send_message(f"🎵 Currently playing: **{title}**")

@bot.tree.command(name="247", description="Toggle 24/7 mode to keep bot in VC.")
async def toggle_247(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    is_24_7[gid] = not is_24_7.get(gid, False)
    await interaction.response.send_message(
        "🔄 24/7 mode enabled." if is_24_7[gid] else "🔄 24/7 mode disabled."
    )
    if not is_24_7.get(gid) and not SONG_QUEUES.get(gid) and interaction.guild.voice_client:
        await check_for_inactivity(interaction.channel, bot, is_24_7.get(gid, False))

async def volume_prefix(ctx, amount: int):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    if not (0 <= amount <= 200):
        return await ctx.send("❌ Volume must be between 0 and 200.")
    vid = str(ctx.guild.id)
    volume_settings[vid] = min(amount / 100, 2.0)
    vc = ctx.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = volume_settings[vid]
    await ctx.send(f"🔊 Volume set to {amount}%.")
    if vc and not vc.is_playing() and not SONG_QUEUES.get(vid):
        await check_for_inactivity(ctx.channel, bot, is_24_7.get(vid, False))

async def play_prefix(ctx, query):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("You must be in a voice channel.")

    voice_channel = ctx.author.voice.channel
    voice_client = ctx.voice_client

    try:
        voice_client = await connect_to_voice(voice_channel, voice_client)
        volume_settings[str(ctx.guild.id)] = 1.0
    except Exception as e:
        logging.error(f"Failed to connect: {e}")
        return await ctx.send("Unable to connect to your voice channel.")

    if "youtube.com/watch" in query or "youtu.be/" in query:
        search = query
    else:
        search = "ytsearch:" + query

    ydl_options = {
        "format": "bestaudio[acodec=opus]/bestaudio[acodec=aac]/bestaudio/best",
        "quiet": True,
        "default_search": "auto",
        "noplaylist": True,
        "source_address": "0.0.0.0",
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
    }

    try:
        results = await search_ytdlp_async(search, ydl_options)
        if not results:
            return await ctx.send("Failed to fetch song data.")
    except Exception as e:
        logging.error(f"yt_dlp error: {e}")
        return await ctx.send("Failed to fetch song data.")

    if "entries" in results:
        tracks = results.get("entries") or []
        if not tracks:
            return await ctx.send("No results found.")
        first = tracks[0]
    else:
        first = results

    audio_url = first["url"]
    title = first.get("title", "Untitled")
    gid = str(ctx.guild.id)
    if SONG_QUEUES.get(gid) is None:
        SONG_QUEUES[gid] = deque()
    SONG_QUEUES[gid].append((audio_url, title))

    if voice_client.is_playing() or voice_client.is_paused():
        await ctx.send(f"Added to queue: **{title}**")
    else:
        await play_next_song(voice_client, gid, ctx.channel)

async def pause_prefix(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸️ Playback paused.")
        if not SONG_QUEUES.get(str(ctx.guild.id)):
            await check_for_inactivity(ctx.channel, bot, is_24_7.get(str(ctx.guild.id), False))
    else:
        await ctx.send("Nothing is currently playing.")

async def resume_prefix(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ Playback resumed.")
    else:
        await ctx.send("I’m not paused right now.")
        if vc and not vc.is_playing() and not SONG_QUEUES.get(str(ctx.guild.id)):
            await check_for_inactivity(ctx.channel, bot, is_24_7.get(str(ctx.guild.id), False))

async def skip_prefix(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await ctx.send("⏭️ Skipped the current song.")
    else:
        await ctx.send("Not playing anything to skip.")
        if vc and not vc.is_playing() and not SONG_QUEUES.get(str(ctx.guild.id)):
            await check_for_inactivity(ctx.channel, bot, is_24_7.get(str(ctx.guild.id), False))

async def disconnect_prefix(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    vc = ctx.voice_client
    if vc:
        guild_id = str(ctx.guild.id)
        SONG_QUEUES[guild_id] = deque()
        volume_settings.pop(guild_id, None)
        is_24_7.pop(guild_id, None)
        current_songs.pop(guild_id, None)
        await vc.disconnect()
        await ctx.send("👋 Disconnected and cleared the queue.")
    else:
        await ctx.send("I'm not connected to any voice channel.")

async def join_prefix(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("You must be in a voice channel.")
    try:
        vc = await connect_to_voice(ctx.author.voice.channel, ctx.voice_client)
        volume_settings[str(ctx.guild.id)] = 1.0
        bitrate = ctx.author.voice.channel.bitrate // 1000
        if bitrate < 128:
            await ctx.send(
                f"✅ Joined your voice channel. ⚠️ Low bitrate ({bitrate} kbps) detected. Boost server for better audio (128+ kbps)."
            )
        else:
            await ctx.send("✅ Joined your voice channel.")
        if not vc.is_playing() and not SONG_QUEUES.get(str(ctx.guild.id)):
            await check_for_inactivity(ctx.channel, bot, is_24_7.get(str(ctx.guild.id), False))
    except Exception as e:
        logging.error(f"Join command error: {e}")
        await ctx.send("❌ Failed to join voice channel.")

async def queue_prefix(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    q = SONG_QUEUES.get(str(ctx.guild.id), [])
    if not q:
        await ctx.send("📭 The queue is currently empty.")
        vc = ctx.voice_client
        if vc and not vc.is_playing():
            await check_for_inactivity(ctx.channel, bot, is_24_7.get(str(ctx.guild.id), False))
    else:
        lines = [f"{i+1}. {t}" for i, (_, t) in enumerate(q)]
        await ctx.send("🎶 Queue:\n" + "\n".join(lines))

async def cleanqueue_prefix(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    gid = str(ctx.guild.id)
    SONG_QUEUES.get(gid, deque()).clear()
    await ctx.send("🧹 Queue has been cleared!")
    vc = ctx.voice_client
    if vc and not vc.is_playing():
        await check_for_inactivity(ctx.channel, bot, is_24_7.get(gid, False))

async def nowplaying_prefix(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    vc = ctx.voice_client
    guild_id = str(ctx.guild.id)
    if not vc or not vc.is_playing():
        await ctx.send("Nothing is currently playing.")
        if vc and not SONG_QUEUES.get(guild_id):
            await check_for_inactivity(ctx.channel, bot, is_24_7.get(guild_id, False))
    else:
        title = current_songs.get(guild_id, {}).get("title", "Unknown")
        await ctx.send(f"🎵 Currently playing: **{title}**")

async def loop_prefix(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    gid = str(ctx.guild.id)
    loop_mode[gid] = not loop_mode.get(gid, False)
    await ctx.send("🔁 Loop enabled." if loop_mode[gid] else "➡️ Loop disabled.")
    vc = ctx.voice_client
    if vc and not vc.is_playing() and not SONG_QUEUES.get(gid):
        await check_for_inactivity(ctx.channel, bot, is_24_7.get(gid, False))

async def toggle_247_prefix(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ I don’t have permission to delete your message.")
    gid = str(ctx.guild.id)
    is_24_7[gid] = not is_24_7.get(gid, False)
    await ctx.send("🔄 24/7 mode enabled." if is_24_7[gid] else "🔄 24/7 mode disabled.")
    if not is_24_7.get(gid) and not SONG_QUEUES.get(gid) and ctx.voice_client:
        await check_for_inactivity(ctx.channel, bot, is_24_7.get(gid, False))

@bot.event
async def on_message(message):
    if message.author.bot or not message.content.startswith("`"):
        return

    ctx = await bot.get_context(message)
    parts = message.content[1:].strip().split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == "play":
        await play_prefix(ctx, arg)
    elif command == "pause":
        await pause_prefix(ctx)
    elif command == "resume":
        await resume_prefix(ctx)
    elif command == "skip":
        await skip_prefix(ctx)
    elif command == "disconnect":
        await disconnect_prefix(ctx)
    elif command == "join":
        await join_prefix(ctx)
    elif command == "queue":
        await queue_prefix(ctx)
    elif command == "cleanqueue":
        await cleanqueue_prefix(ctx)
    elif command == "volume":
        try:
            await volume_prefix(ctx, int(arg))
        except:
            await ctx.send("❌ Provide a number between 0 and 200.")
    elif command == "nowplaying":
        await nowplaying_prefix(ctx)
    elif command == "loop":
        await loop_prefix(ctx)
    elif command == "247":
        await toggle_247_prefix(ctx)

try:
    bot.run(TOKEN)
except Exception as e:
    logging.error(f"Bot failed to start: {e}")