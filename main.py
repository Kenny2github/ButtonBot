import sys
import os
import json
from typing import Dict, Optional, Tuple
import traceback
import subprocess
import asyncio
import discord
from discord.ext import slash
from discord.ext import commands

SRCDIR = os.path.dirname(os.path.abspath(__file__))
VERSION = subprocess.check_output(
    f"cd {SRCDIR} && git rev-parse --short HEAD", shell=True
).decode('ascii').strip()
CONFIG_FILE = 'buttonbot.json'
LEAVE_DELAY = 1
os.chdir(SRCDIR)

with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

client = slash.SlashBot(
    description='A bot that plays sound effects',
    command_prefix='/',
    help_command=None,
    activity=discord.Activity(type=discord.ActivityType.watching, name='/'),
    debug_guild=CONFIG.get('guild_id', None),
    resolve_not_fetch=False,
    fetch_if_not_get=True
)

try:
    if sys.argv[1] != '-':
        sys.stdout = sys.stderr = open(sys.argv[1])
except IOError:
    print(f"Couldn't open output file {sys.argv[1]!r}, quitting")
    raise SystemExit(1) from None
except IndexError:
    pass # not specified, use stdout

@client.event
async def on_command_error(ctx, exc):
    if hasattr(ctx.command, 'on_error'):
        return
    cog = ctx.cog
    if cog:
        attr = 'on_error'
        if hasattr(cog, attr):
            return
    if isinstance(exc, (
        commands.BotMissingPermissions,
        commands.MissingPermissions,
        commands.MissingRequiredArgument,
        commands.BadArgument,
        commands.CommandOnCooldown,
    )):
        return await ctx.send(embed=discord.Embed(
            title='Error',
            description=str(exc),
            color=0xff0000
        ))
    if isinstance(exc, (
        commands.CheckFailure,
        commands.CommandNotFound,
        commands.TooManyArguments,
    )):
        return
    print('Ignoring exception in command {}:\n'.format(ctx.command)
        + ''.join(traceback.format_exception(
            type(exc), exc, exc.__traceback__
        )),
    )

@client.slash_cmd()
async def hello(ctx: slash.Context):
    """Hello World!"""
    await ctx.respond('Hello World!', ephemeral=True)

@client.slash_cmd()
async def invite(ctx: slash.Context):
    """Get a link to add the bot in your own server."""
    url = CONFIG.get('url', 'No invite configured! Contact bot owner.')
    await ctx.respond(url, ephemeral=True)

@client.slash_cmd()
async def version(ctx: slash.Context):
    """Get the Git version this bot is running on."""
    global VERSION
    VERSION = subprocess.check_output(
        f"cd {SRCDIR} && git rev-parse --short HEAD", shell=True
    ).decode('ascii').strip()
    await ctx.respond(f'`{VERSION}`', ephemeral=True)

### DYNAMIC COMMANDS TECH ###

guild_locks: Dict[int, asyncio.Lock] = {}

def guild_root(guild_id: Optional[int]) -> str:
    if guild_id:
        return os.path.join('sounds', '.guild', str(guild_id))
    return 'sounds'

def sound(name: str, guild_id: Optional[int]) -> Tuple[str, str]:
    """Get the (message text, sound filename) from sound name."""
    root = guild_root(guild_id)
    with open(os.path.join(root, name, 'sound.json')) as f:
        text = json.load(f)['text']
    return text, os.path.join(root, name, 'sound.mp3')

def sound_source(name: str, guild_id: Optional[int]) \
        -> Tuple[str, discord.FFmpegOpusAudio]:
    """Get the FFmpegOpusAudio source from sound name."""
    text, fn = sound(name, guild_id)
    fn = fn.rsplit('.', 1)[0] + '.opus'
    return text, discord.FFmpegOpusAudio(fn, codec='copy')

async def wait_and_unset(ctx: slash.Context, last: discord.VoiceChannel):
    """Wait a few seconds before leaving."""
    await asyncio.sleep(LEAVE_DELAY)
    vc = ctx.guild.voice_client
    if vc is not None and not vc.is_playing():
        # - not already left
        # - not playing audio anymore
        await vc.disconnect() # so leave

async def play_in_voice(ctx: slash.Context, name: str, channel: discord.VoiceChannel):
    """Play the sound in a voice channel."""
    lock = guild_locks.setdefault(ctx.guild.id, asyncio.Lock())
    async with lock:
        text, source = sound_source(name, ctx.command.guild_id)
        asyncio.create_task(ctx.respond(text, ephemeral=True)) # send Bruh
        # if things error past here, we've already sent the message
        try:
            vc: Optional[discord.VoiceClient] = ctx.guild.voice_client
            if vc is not None and vc.channel.id != channel.id:
                # finished playing in another channel, move to author's channel
                await vc.move_to(channel)
            elif vc is None:
                # haven't been playing, join author's channel
                vc = await channel.connect(timeout=5)
            # convert callback-based to awaiting
            fut: asyncio.Future = client.loop.create_future()
            def after(exc):
                if exc:
                    fut.set_exception(exc)
                else:
                    fut.set_result(None)
            vc.play(source, after=after)
            await fut
        finally:
            # finished playing (hopefully)
            asyncio.create_task(wait_and_unset(ctx, channel))

async def execute(ctx: slash.Context, name: str):
    """Play or upload the sound."""
    v = ctx.author.voice is not None and ctx.author.voice.channel is not None
    if v:
        try:
            await play_in_voice(ctx, name, ctx.author.voice.channel)
        except (discord.HTTPException, asyncio.TimeoutError):
            return # we did our best
        else:
            return # success, stop here
    else:
        text, fn = sound(name, ctx.command.guild_id)
        f = discord.File(fn, filename=name + '.mp3')
        await ctx.respond(text, file=f)

def load_guild(guild_id: Optional[int]):
    root = guild_root(guild_id)
    for name in os.listdir(root):
        if not name.isalpha():
            continue
        with open(os.path.join(root, name, 'sound.json')) as f:
            descname = json.load(f)['name']
        desc = f"Play a {descname} sound effect."
        print('adding', name, guild_id, desc)
        @client.slash_cmd(name=name, description=desc, guild_id=guild_id)
        async def __cmd(ctx: slash.Context, n=name):
            """Closure for /(name)"""
            await execute(ctx, n)

load_guild(None)
for sid in os.listdir(os.path.join('sounds', '.guild')):
    if not sid.isdigit():
        continue
    load_guild(int(sid))

### DYNAMIC COMMANDS TECH END ###

async def wakeup():
    await client.wait_until_ready()
    while 1:
        try:
            await asyncio.sleep(1)
        except:
            await client.close()
            return

client.loop.create_task(wakeup())
client.run(CONFIG['token'])
