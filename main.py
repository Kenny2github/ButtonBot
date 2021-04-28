import sys
import os
import shutil
import json
import re
from typing import Dict, Optional, Tuple, Union
import traceback
import asyncio
import aiohttp
import discord
from discord.ext import slash
from discord.ext import commands

SRCDIR = os.path.dirname(os.path.abspath(__file__))
VERSION = None
CONFIG_FILE = 'buttonbot.json'
LEAVE_DELAY = 1
NAME_REGEX = re.compile(r'^[a-z0-9]{1,32}$')
os.chdir(SRCDIR)

with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

try:
    loop = asyncio.ProactorEventLoop()
except AttributeError:
    loop = asyncio.get_event_loop()

client = slash.SlashBot(
    loop=loop,
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
        sys.stdout = sys.stderr = open(sys.argv[1], 'a')
except IOError:
    print(f"Couldn't open output file {sys.argv[1]!r}, quitting")
    raise SystemExit(1) from None
except IndexError:
    pass # not specified, use stdout

async def send_error(method, msg):
    await method(embed=discord.Embed(
        title='Error',
        description=msg,
        color=0xff0000
    ))

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
        return await send_error(ctx.send, str(exc))
    if isinstance(exc, (
        commands.CheckFailure,
        commands.CommandNotFound,
        commands.TooManyArguments,
    )):
        return
    print('Ignoring exception in command {}:\n'.format(ctx.command)
        + ''.join(traceback.format_exception(
            type(exc), exc, exc.__traceback__
        )), flush=True
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
    proc = await asyncio.create_subprocess_shell(
        f"cd {SRCDIR} && git rev-parse --short HEAD",
        stdout=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate()
    VERSION = stdout.decode('ascii').strip()
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
    if guild_id:
        for _cmd in tuple(client.slash):
            if _cmd.guild_id == guild_id:
                # remove all commands from this guild first
                client.slash.discard(_cmd)
    root = guild_root(guild_id)
    for name in os.listdir(root):
        if not NAME_REGEX.match(name):
            continue
        try:
            os.rmdir(os.path.join(root, name))
        except OSError:
            pass # not empty, can probably be used
        else:
            continue # was empty, skip
        with open(os.path.join(root, name, 'sound.json')) as f:
            descname = json.load(f)['name']
        desc = f"Play a {descname} sound effect."
        print('adding', name, guild_id, desc, flush=True)
        @client.slash_cmd(name=name, description=desc, guild_id=guild_id)
        async def __cmd(ctx: slash.Context, n=name):
            """Closure for /(name)"""
            await execute(ctx, n)

### DYNAMIC COMMANDS TECH END ###

async def msg_input(ctx: slash.Context, prompt: str, content: bool = True) \
        -> Union[str, discord.Message]:
    await ctx.respond(prompt)
    msg = await client.wait_for('message', timeout=60.0, check=lambda m: (
        m.channel.id == ctx.channel.id
        and m.author.id == ctx.author.id
    ))
    if content:
        return msg.content
    return msg

def cleanup_failure(fn: str, root: str):
    # remove the tmp file if it exists
    try:
        os.remove(fn)
    except FileNotFoundError:
        pass
    # remove the command directory if it's empty
    try:
        os.rmdir(root)
    except OSError:
        pass

name_opt = slash.Option(
    'The /name of the command (alphanumeric only).',
    required=True)

@client.slash_cmd()
async def cmd(ctx: slash.Context, name: name_opt):
    """Create a new guild command."""
    try:
        text = await msg_input(ctx, 'Send the **exact text** '
                               'to use as the command text. (Send "cancel" '
                               'three times to cancel this addition.)')
        desc = await msg_input(ctx, 'Send the name of the sound effect '
                               '(goes in the command description).')
        audf = await msg_input(ctx, 'Send a link to, or upload, '
                               'the ffmpeg-compatible sound file.', False)
    except asyncio.TimeoutError:
        await send_error(ctx.webhook.send, 'Timed out waiting for message.')
        return
    root = os.path.join(guild_root(ctx.guild.id), name)
    os.makedirs(root, exist_ok=True)
    if audf.attachments:
        audf = audf.attachments[0]
        try:
            fn = 'tmp.' + audf.filename.rsplit('.', 1)[1]
            fn = os.path.join(root, fn)
            await audf.save(fn)
        except IndexError:
            await send_error(ctx.webhook.send, 'Failed to download attachment'
                             ': Does not seem to be ffmpeg-compatible')
        except discord.HTTPException as exc:
            await send_error(ctx.webhook.send, 'Failed to download attachment'
                             f': {exc!s}')
            return
    else:
        try:
            fn = 'tmp.' + audf.content.strip().rsplit('.', 1)[1]
            fn = os.path.join(root, fn)
            async with aiohttp.ClientSession() as sesh:
                async with sesh.get(audf.content.strip()) as resp:
                    resp.raise_for_status()
                    CHUNK = 1024*1024
                    chunk = await resp.content.read(CHUNK)
                    with open(fn, 'wb') as f:
                        while chunk:
                            f.write(chunk)
                            chunk = await resp.content.read(CHUNK)
        except (IndexError, aiohttp.InvalidURL):
            await send_error(ctx.webhook.send, 'Failed to download link: '
                             'Invalid URL')
            return
        except aiohttp.ClientError as exc:
            await send_error(ctx.webhook.send,
                             f'Failed to download link: {exc!s}')
            cleanup_failure(fn, root)
            return
    MP3 = os.path.join(root, 'sound.mp3')
    OPUS = os.path.join(root, 'sound.opus')
    try:
        os.remove(MP3)
    except FileNotFoundError:
        pass
    try:
        os.remove(OPUS)
    except FileNotFoundError:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-i', fn, MP3, OPUS,
            stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            stderr = stderr.decode()
            stderr = stderr.rsplit('  lib', 1)[1].split('\n', 1)[1]
            await send_error(ctx.webhook.send,
                             'Failed to convert audio:\n'
                             '```\n%s\n```' % stderr)
            return
    finally:
        cleanup_failure(fn, root)
    with open(os.path.join(root, 'sound.json'), 'w') as f:
        json.dump({'text': text, 'name': desc}, f)
    load_guild(ctx.guild.id)
    await client.register_commands(ctx.guild.id)
    await ctx.webhook.send(f'Successfully added/modified `/{name}`')

@client.slash_cmd(name='-cmd')
async def del_cmd(ctx: slash.Context, name: name_opt):
    """Remove a command, if it exists."""
    root = os.path.join(guild_root(ctx.guild.id), name)
    shutil.rmtree(root, True)
    load_guild(ctx.guild.id)
    await client.register_commands(ctx.guild.id)
    await ctx.respond(f'Removed `/{name}` if it exists')

@cmd.check
@del_cmd.check
async def cmd_check(ctx: slash.Context):
    if not ctx.author.guild_permissions.manage_guild:
        await send_error(ctx.respond, 'You must have Manage Server '
                         'permissions to use this command.')
        return False
    name = ctx.options['name'].casefold()
    if not NAME_REGEX.match(name):
        await send_error(ctx.respond, 'Command name must consist '
                         'only of 1-32 letters and numbers')
        return False
    return True

async def wakeup():
    while 1:
        try:
            await asyncio.sleep(1)
        except:
            await client.close()
            return

if '-v' in sys.argv:
    import logging
    logging.basicConfig()
    logging.getLogger('discord.ext.slash').setLevel(logging.DEBUG)

try:
    load_guild(None)
    for sid in os.listdir(os.path.join('sounds', '.guild')):
        if not sid.isdigit():
            continue
        load_guild(int(sid))
    client.loop.create_task(wakeup())
    client.run(CONFIG['token'])
finally:
    sys.stdout.close()
