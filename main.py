import sys
import os
from pathlib import Path
import shutil
import json
import re
from typing import Dict, Optional, Tuple, Union
import logging
import traceback
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

SRCDIR = Path(__file__).resolve().parent
VERSION = None
CONFIG_FILE = 'buttonbot.json'
LEAVE_DELAY = 1
NAME_REGEX = re.compile(r'^[a-z0-9]{1,32}$')
os.chdir(SRCDIR)

# logging config
if len(sys.argv) <= 1 or sys.argv[1].startswith('-'):
    log_handler = logging.StreamHandler(sys.stdout)
else:
    log_handler = logging.FileHandler(sys.argv[1], 'a')
logging.basicConfig(format='{asctime} {levelname}\t {name:19} {message}',
                    style='{', handlers=[log_handler], level=logging.INFO)
logging.getLogger('discord').setLevel(logging.INFO)
if '-v' in sys.argv:
    logging.getLogger('discord.app_commands').setLevel(logging.DEBUG)
logger = logging.getLogger('ButtonBot')
logger.setLevel(logging.DEBUG)

with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

async def send_error(method, msg):
    await method(embed=discord.Embed(
        title='Error',
        description=msg,
        color=0xff0000
    ))

class ButtonTree(app_commands.CommandTree):
    async def on_error(self, ctx: discord.Interaction,
                       exc: app_commands.AppCommandError) -> None:
        if ctx.command:
            if ctx.command.on_error:
                return # has its own handler
        else:
            return # no valid command
        if isinstance(exc, (
            commands.BotMissingPermissions,
            commands.MissingPermissions,
            commands.MissingRequiredArgument,
            commands.BadArgument,
            commands.CommandOnCooldown,
        )) and isinstance(ctx.channel, discord.abc.Messageable):
            return await send_error(ctx.channel.send, str(exc))
        if isinstance(exc, (
            commands.CheckFailure,
            commands.CommandNotFound,
            commands.TooManyArguments,
        )):
            return
        logger.error('Ignoring exception in command %r:\n%s',
                     ctx.command and ctx.command.name,
                     ''.join(traceback.format_exception(
                         type(exc), exc, exc.__traceback__)))

class ButtonBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            description='A bot that plays sound effects',
            command_prefix='/',
            intents=discord.Intents.default(),
            help_command=None,
            activity=discord.Activity(type=discord.ActivityType.watching, name='/'),
            tree_cls=ButtonTree,
        )

    async def setup_hook(self) -> None:
        asyncio.create_task(wakeup())
        debug_guild_id = CONFIG.get('guild_id', None)
        if debug_guild_id:
            debug_guild = discord.Object(debug_guild_id)
            self.tree.copy_global_to(guild=debug_guild)
            await self.tree.sync(guild=debug_guild)

client = ButtonBot()

@client.tree.command()
async def hello(ctx: discord.Interaction):
    """Hello World!"""
    await ctx.response.send_message('Hello World!', ephemeral=True)

@client.tree.command()
async def invite(ctx: discord.Interaction):
    """Get a link to add the bot in your own server."""
    url = CONFIG.get('url', 'No invite configured! Contact bot owner.')
    await ctx.response.send_message(url, ephemeral=True)

@client.tree.command()
async def version(ctx: discord.Interaction):
    """Get the Git version this bot is running on."""
    global VERSION
    proc = await asyncio.create_subprocess_shell(
        f"cd {SRCDIR} && git rev-parse --short HEAD",
        stdout=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate()
    VERSION = stdout.decode('ascii').strip()
    await ctx.response.send_message(f'`{VERSION}`', ephemeral=True)

### DYNAMIC COMMANDS TECH ###

guild_locks: Dict[int, asyncio.Lock] = {}

def guild_root(guild_id: Optional[int]) -> Path:
    if guild_id:
        return Path('sounds') / '.guild' / str(guild_id)
    return Path('sounds')

def sound(name: str, guild_id: Optional[int]) -> Tuple[str, Path]:
    """Get the (message text, sound filename) from sound name."""
    root = guild_root(guild_id)
    with open(root / name / 'sound.json') as f:
        text = json.load(f)['text']
    return text, root / name / 'sound.mp3'

def sound_source(name: str, guild_id: Optional[int]) \
        -> Tuple[str, discord.FFmpegOpusAudio]:
    """Get the FFmpegOpusAudio source from sound name."""
    text, fn = sound(name, guild_id)
    fn = str(fn).rsplit('.', 1)[0] + '.opus'
    return text, discord.FFmpegOpusAudio(fn, codec='copy')

async def wait_and_unset(vc: discord.VoiceClient):
    """Wait a few seconds before leaving."""
    await asyncio.sleep(LEAVE_DELAY)
    if vc is not None and not vc.is_playing():
        # - not already left
        # - not playing audio anymore
        await vc.disconnect() # so leave

async def play_in_voice(
    ctx: discord.Interaction, name: str,
    channel: Union[discord.VoiceChannel, discord.StageChannel],
    guild: Optional[discord.abc.Snowflake]
) -> None:
    """Play the sound in a voice channel."""
    if ctx.guild is None or not isinstance(ctx.command, app_commands.Command):
        logger.warning('play_in_voice called from outside guild or command? '
                       'guild: %r, command: %r', ctx.guild, ctx.command)
        return
    lock = guild_locks.setdefault(ctx.guild.id, asyncio.Lock())
    locked = lock.locked()
    if locked:
        await ctx.response.defer(ephemeral=True)
    async with lock:
        text, source = sound_source(name, guild.id if guild else None)
        if locked: # i.e. was deferred
            asyncio.create_task(ctx.edit_original_message(content=text))
        else:
            asyncio.create_task(ctx.response.send_message(text, ephemeral=True))
        # if things error past here, we've already sent the message
        vc: Optional[discord.VoiceClient] \
            = ctx.guild.voice_client # type: ignore # not customized
        try:
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
            if vc is not None:
                asyncio.create_task(wait_and_unset(vc))

async def execute(ctx: discord.Interaction, chat: bool, name: str,
                  guild: Optional[discord.abc.Snowflake]) -> None:
    """Play or upload the sound."""
    assert isinstance(ctx.user, discord.Member) \
        and isinstance(ctx.command, app_commands.Command)
    if ctx.user.voice is not None and ctx.user.voice.channel \
            is not None and not chat:
        try:
            await play_in_voice(ctx, name, ctx.user.voice.channel, guild)
        except (discord.HTTPException, asyncio.TimeoutError):
            return # we did our best
        else:
            return # success, stop here
    else:
        text, fn = sound(name, guild.id if guild else None)
        f = discord.File(fn, filename=name + '.mp3')
        await ctx.response.send_message(text, file=f)

def make_cmd(name: str, desc: str,
             guild: Optional[discord.abc.Snowflake]) -> None:
    @client.tree.command(name=name, description=desc, guild=guild)
    @app_commands.describe(
        chat="If True, sends the sound in chat even if you're in voice.")
    @app_commands.guild_only
    async def __cmd(ctx: discord.Interaction, chat: bool = False):
        """Closure for /(name)"""
        await execute(ctx, chat, name, guild)

def load_guild(guild_id: Optional[int]):
    if guild_id:
        guild = discord.Object(guild_id)
        client.tree.clear_commands(guild=guild)
    else:
        guild = None
    root = guild_root(guild_id)
    for name in os.listdir(root):
        if not NAME_REGEX.match(name):
            continue
        try:
            os.rmdir(root / name)
        except OSError:
            pass # not empty, can probably be used
        else:
            continue # was empty, skip
        with open(root / name / 'sound.json') as f:
            descname = json.load(f)['name']
        desc = f"Play a {descname} sound effect."
        logger.info('Adding /%s in guild %s: %r', name, guild_id, desc)
        make_cmd(name, desc, guild)

### DYNAMIC COMMANDS TECH END ###

def cleanup_failure(fn: Path, root: Path):
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

@client.tree.command()
@app_commands.describe(
    name='The /name of the command (alphanumeric only).',
    text='The exact text to use as the command text.',
    description='The name of the sound effect ("Play a <desc> sound effect").',
    file='Upload the ffmpeg-compatible sound file to play.',
    link='The link to the ffmpeg-compatible sound file to play.',
)
@app_commands.guild_only
async def cmd(ctx: discord.Interaction, name: str,
              text: str, description: str,
              file: Optional[discord.Attachment] = None,
              link: Optional[str] = None) -> None:
    """Create a new guild command."""
    assert ctx.guild is not None
    if (file is None and link is None) \
            or (file is not None and link is not None):
        await send_error(ctx.response.send_message,
                         'Exactly one of `file` or `link` '
                         'should be specified.')
        return
    await ctx.response.defer()
    text = text
    root = guild_root(ctx.guild.id) / name
    os.makedirs(root, exist_ok=True)
    if file is not None:
        try:
            fn = 'tmp.' + file.filename.rsplit('.', 1)[1]
            fn = root / fn
        except IndexError:
            await send_error(ctx.edit_original_message,
                             'Failed to download attachment: '
                             'Does not seem to be ffmpeg-compatible')
            return
        try:
            await file.save(fn)
        except discord.HTTPException as exc:
            await send_error(ctx.edit_original_message,
                             'Failed to download attachment: '
                             f'{exc!s}')
            return
    elif link is not None:
        try:
            fn = 'tmp.' + link.strip().rsplit('.', 1)[1]
            fn = root / fn
        except IndexError:
            await send_error(ctx.edit_original_message,
                             'Failed to download link: Invalid URL')
            return
        try:
            async with aiohttp.ClientSession() as sesh:
                async with sesh.get(link.strip()) as resp:
                    resp.raise_for_status()
                    CHUNK = 1024*1024
                    with open(fn, 'wb') as f:
                        while chunk := await resp.content.read(CHUNK):
                            f.write(chunk)
        except aiohttp.InvalidURL:
            await send_error(ctx.edit_original_message,
                             'Failed to download link: Invalid URL')
            return
        except aiohttp.ClientError as exc:
            await send_error(ctx.edit_original_message,
                             f'Failed to download link: {exc!s}')
            cleanup_failure(fn, root)
            return
    else:
        raise RuntimeError('Logical impossibility')
    MP3 = root / 'sound.mp3'
    OPUS = root / 'sound.opus'
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
            await send_error(ctx.edit_original_message,
                             'Failed to convert audio:\n'
                             '```\n%s\n```' % stderr)
            return
    finally:
        cleanup_failure(fn, root)
    with open(root / 'sound.json', 'w') as f:
        json.dump({'text': text, 'name': description}, f)
    load_guild(ctx.guild.id)
    await client.tree.sync(guild=ctx.guild)
    await ctx.edit_original_message(content=f'Successfully added/modified `/{name}`')

@client.tree.command(name='-cmd')
@app_commands.describe(name='The /name of the command (alphanumeric only).')
@app_commands.guild_only
async def del_cmd(ctx: discord.Interaction, name: str):
    """Remove a command, if it exists."""
    assert ctx.guild is not None
    root = guild_root(ctx.guild.id) / name
    shutil.rmtree(root, True)
    load_guild(ctx.guild.id)
    await client.tree.sync(guild=ctx.guild)
    await ctx.response.send_message(f'Removed `/{name}` if it exists')

async def cmd_check(ctx: discord.Interaction):
    assert isinstance(ctx.user, discord.Member)
    if not ctx.user.guild_permissions.manage_guild:
        await send_error(ctx.response.send_message,
                         'You must have Manage Server '
                         'permissions to use this command.')
        return False
    name = ctx.namespace.name.casefold()
    if not NAME_REGEX.match(name):
        await send_error(ctx.response.send_message,
                         'Command name must consist '
                         'only of 1-32 letters and numbers')
        return False
    return True

cmd.add_check(cmd_check)
del_cmd.add_check(cmd_check)

async def wakeup():
    while 1:
        try:
            await asyncio.sleep(1)
        except:
            await client.close()
            return

load_guild(None)
for sid in os.listdir(Path('sounds') / '.guild'):
    if not sid.isdigit():
        continue
    load_guild(int(sid))
client.run(CONFIG['token'], log_handler=None)
