from __future__ import annotations
from functools import cache
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
from urllib.parse import urlparse
import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

SRCDIR = Path(__file__).resolve().parent
VERSION = None
CONFIG_FILE = 'buttonbot.json'
STATS_FILE = 'buttonbot_stats.db'
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

@cache
def cmd_guild_id(ctx: discord.Interaction) -> Optional[int]:
    if not isinstance(ctx.command, app_commands.Command):
        return None
    if 'guild_id' in ctx.command.extras:
        return ctx.command.extras['guild_id']
    if ctx.command.guild_only:
        return ctx.guild_id
    return None

class ButtonTree(app_commands.CommandTree):

    client: ButtonBot

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

    async def interaction_check(self, ctx: discord.Interaction) -> bool:
        if not isinstance(ctx.command, app_commands.Command):
            return False # we shouldn't have anything other than these
        logger.info('User %s\t(%18d) in channel %s\t(%18d) '
                    'running /%s (belongs to guild %s)',
                    ctx.user, ctx.user.id, ctx.channel,
                    ctx.channel.id if ctx.channel else '(none)',
                    ctx.command.qualified_name,
                    cmd_guild_id(ctx))
        self.client.log_queue.put_nowait(ctx)
        return True

class ButtonBot(commands.Bot):

    log_queue: asyncio.Queue[discord.Interaction]
    db: aiosqlite.Cursor

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
        self.log_queue = asyncio.Queue()

        debug_guild_id = CONFIG.get('guild_id', None)
        if debug_guild_id:
            debug_guild = discord.Object(debug_guild_id)
            self.tree.copy_global_to(guild=debug_guild)
        else:
            debug_guild = None
        await self.tree.sync(guild=debug_guild)

        asyncio.create_task(self.save_stats())

    async def save_stats(self) -> None:
        dbw = await aiosqlite.connect(STATS_FILE)
        dbw.row_factory = aiosqlite.Row
        self.db = await dbw.cursor()
        await self.db.executescript("""
CREATE TABLE IF NOT EXISTS global_stats (
    cmd_name TEXT NOT NULL,
    used_in_guild_id INTEGER NOT NULL,
    usage_count INTEGER DEFAULT 1,
    PRIMARY KEY(cmd_name, used_in_guild_id)
);
CREATE INDEX IF NOT EXISTS global_cmds ON global_stats(cmd_name);
CREATE INDEX IF NOT EXISTS global_guilds ON global_stats(used_in_guild_id);
CREATE TABLE IF NOT EXISTS guild_stats (
    cmd_name TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    usage_count INTEGER DEFAULT 1,
    PRIMARY KEY(cmd_name, guild_id)
);
CREATE INDEX IF NOT EXISTS guild_guilds ON guild_stats(guild_id);
""")
        try:
            logged_count = 0
            while 1:
                ctx = await self.log_queue.get()
                if not ctx.command:
                    continue # ignore non-command interactions
                if not isinstance(ctx.command, app_commands.Command):
                    continue # ...what?
                if ctx.command.qualified_name in {
                    'hello', 'invite', 'version', 'stats', 'cmd', '-cmd'
                }:
                    continue # don't record stats for meta-commands
                command_guild = cmd_guild_id(ctx)
                if command_guild: # guild-specific command
                    await self.db.execute(
                        'INSERT INTO guild_stats(cmd_name, guild_id) '
                        'VALUES (?, ?) ON CONFLICT(cmd_name, guild_id) '
                        'DO UPDATE SET usage_count = usage_count + 1;',
                        (ctx.command.qualified_name, command_guild))
                else:
                    await self.db.execute(
                        'INSERT INTO global_stats(cmd_name, used_in_guild_id) '
                        'VALUES (?, ?) ON CONFLICT(cmd_name, used_in_guild_id) '
                        'DO UPDATE SET usage_count = usage_count + 1;',
                        (ctx.command.qualified_name, ctx.guild_id))
                logged_count += 1
                if logged_count >= CONFIG['commit_threshold']:
                    logger.debug('Logged %s usages, committing stats',
                                 logged_count)
                    await dbw.commit()
                    logged_count = 0
        except KeyboardInterrupt:
            logger.info('Goodbye.')
        finally:
            await dbw.commit()
            await dbw.close()
            await self.close()

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

def mask(data: str, reveal_last_n: int = 0) -> str:
    mask_n = len(data) - reveal_last_n
    return r'\*' * (mask_n) + data[mask_n:]

@client.tree.command()
@app_commands.describe(
    command="If specified, only get stats for this command.")
async def stats(ctx: discord.Interaction, command: Optional[str] = None):
    """Get stats for command usage."""
    await ctx.response.defer()
    if ctx.guild is not None:
        guild_name = discord.utils.escape_markdown(ctx.guild.name)
    else:
        guild_name = None
    embeds: list[discord.Embed] = []
    if command:
        # global stats
        query = 'SELECT used_in_guild_id, usage_count '\
            'FROM global_stats WHERE cmd_name=?'
        guild_data: dict[int, int] = {
            row[0]: row[1] async for row in
            await client.db.execute(query, (command,))}
        if guild_data:
            def reveal_if_us(guild_id: int) -> str:
                if ctx.guild is not None and guild_id == ctx.guild.id:
                    return ctx.guild.name
                return mask(str(guild_id), 4)
            lines = [f'{reveal_if_us(guild_id)}: {count} uses'
                    for guild_id, count in guild_data.items()]
            lines.append(f'Total: {sum(guild_data.values())} uses')
            embeds.append(discord.Embed(
                title=f'Stats for global command `/{command}`',
                description='\n'.join(lines),
                color=discord.Color.yellow()))
        # guild stats
        if ctx.guild is not None:
            query = 'SELECT usage_count FROM guild_stats ' \
                'WHERE cmd_name=? AND guild_id=?'
            await client.db.execute(query, (command, ctx.guild.id))
            row = await client.db.fetchone()
            if row:
                embeds.append(discord.Embed(
                    title=f'Stats for `/{command}` in {guild_name}',
                    description=f'{row[0]} uses',
                    color=discord.Color.blue()))
        if not embeds:
            embeds.append(discord.Embed(
                title='404 Not Found',
                description=f'No stats found for any command named `/{command}`',
                color=discord.Color.red()))
    else:
        # global stats
        query = 'SELECT cmd_name, SUM(usage_count) ' \
            'FROM global_stats GROUP BY cmd_name ORDER BY SUM(usage_count)'
        cmd_data: dict[str, int] = {
            row[0]: row[1] async for row in await client.db.execute(query)}
        lines = [f'`/{command}`: {count} uses'
                 for command, count in cmd_data.items()]
        embeds.append(discord.Embed(
            title='Global command stats',
            description='\n'.join(lines),
            color=discord.Color.yellow()))
        # guild stats
        if ctx.guild is not None:
            query = 'SELECT cmd_name, SUM(usage_count) ' \
                'FROM guild_stats WHERE guild_id=? '\
                'GROUP BY cmd_name ORDER BY SUM(usage_count)'
            cmd_data: dict[str, int] = {
                row[0]: row[1] async for row in
                await client.db.execute(query, (ctx.guild.id,))}
            if cmd_data:
                lines = [f'`/{command}`: {count} uses'
                        for command, count in cmd_data.items()]
                embeds.append(discord.Embed(
                    title=f'{guild_name} command stats',
                    description='\n'.join(lines),
                    color=discord.Color.blue()))
    await ctx.edit_original_response(embeds=embeds)

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
    async with lock:
        text, source = sound_source(name, guild.id if guild else None)
        asyncio.create_task(ctx.edit_original_response(content=text))
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
        await ctx.response.defer(ephemeral=True)
        try:
            await play_in_voice(ctx, name, ctx.user.voice.channel, guild)
        except (discord.HTTPException, asyncio.TimeoutError):
            return # we did our best
        else:
            return # success, stop here
    else:
        await ctx.response.defer()
        text, fn = sound(name, guild.id if guild else None)
        f = discord.File(fn, filename=name + '.mp3')
        await ctx.edit_original_response(content=text, attachments=[f])

def make_cmd(name: str, desc: str,
             guild: Optional[discord.abc.Snowflake]) -> None:
    @client.tree.command(name=name, description=desc, guild=guild,
                         extras={'guild_id': guild.id if guild else None})
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

async def try_save_file(ctx: discord.Interaction, root: Path,
                        file: discord.Attachment) -> Optional[Path]:
    try:
        fn = 'tmp.' + file.filename.rsplit('.', 1)[1]
        fn = root / fn
        logger.debug('Saving file %s to %s', file.filename, fn)
    except IndexError:
        logger.debug('Filename %s invalid for saving', file.filename)
        await send_error(ctx.edit_original_response,
                         'Failed to download attachment: '
                         'Does not seem to be ffmpeg-compatible')
        return None
    try:
        await file.save(fn)
    except discord.HTTPException as exc:
        logger.exception('Failed to download attachment:')
        await send_error(ctx.edit_original_response,
                         f'Failed to download attachment: {exc!s}')
        return None
    return fn

async def try_save_url(ctx: discord.Interaction,
                       root: Path, link: str) -> Optional[Path]:
    try:
        urlpath = urlparse(link.strip()).path # includes leading slash
        fn = 'tmp.' + urlpath.rsplit('/', 1)[1].rsplit('.', 1)[1]
        fn = root / fn
        logger.debug('Saving filename link %s to %s', link, fn)
    except IndexError:
        logger.debug('Link invalid as filename, trying youtube-dl: %s', link)
        raise # indicates to retry as youtube-dl url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(link.strip()) as response:
                response.raise_for_status()
                CHUNK = 1024*1024
                with open(fn, 'wb') as f:
                    while chunk := await response.content.read(CHUNK):
                        f.write(chunk)
    except aiohttp.InvalidURL:
        await send_error(ctx.edit_original_response,
                         'Failed to download link: Invalid URL')
        return None
    except aiohttp.ClientError as exc:
        logger.exception('Failed to download %s:', link)
        await send_error(ctx.edit_original_response,
                         f'Failed to download link: {exc!s}')
        cleanup_failure(fn, root)
        return None
    return fn

async def try_save_ytd(ctx: discord.Interaction,
                       root: Path, link: str) -> Optional[Path]:
    fn = root / 'sound.m4a'
    logger.debug('Saving youtube-dl link %s to %s', link, fn)
    try:
        cmd = ['youtube-dl', link.strip(), '-f', 'm4a', '-o', fn]
        logger.debug('Executing: %s', cmd)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        stdout, _ = await proc.communicate()
        stdout = stdout.decode()
        logger.debug('youtube-dl subprocess exited; output:\n%s', stdout)
        if proc.returncode != 0:
            await send_error(ctx.edit_original_response,
                             'Failed to download link:\n'
                             f'```\n{stdout}\n```')
            cleanup_failure(fn, root)
            return None
    except Exception as exc:
        logger.exception('Failed to youtube-dl link:')
        await send_error(ctx.edit_original_response,
                         f'Failed to download link: {exc!s}')
        cleanup_failure(fn, root)
        return None
    return fn

async def create_cmd(ctx: discord.Interaction, name: str,
                     text: str, description: str,
                     file: Optional[discord.Attachment] = None,
                     link: Optional[str] = None) -> None:
    """Create a new guild command."""
    assert ctx.guild is not None
    await ctx.response.defer(thinking=True)
    text = text
    root = guild_root(ctx.guild.id) / name
    os.makedirs(root, exist_ok=True)
    if file is not None:
        fn = await try_save_file(ctx, root, file)
        if fn is None:
            return # error already reported
    elif link is not None:
        try:
            fn = await try_save_url(ctx, root, link)
        except IndexError: # link without file extension, maybe ytd-able?
            fn = await try_save_ytd(ctx, root, link)
        if fn is None:
            return # error already reported
    else:
        raise RuntimeError('Logical impossibility')
    logger.debug('Saved argument to %s', fn)
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
        cmd = ['ffmpeg', '-i', fn, MP3, OPUS]
        logger.debug('Executing: %s', cmd)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        stdout, _ = await proc.communicate()
        stdout = stdout.decode()
        logger.debug('ffmpeg subprocess exited; output:\n%s', stdout)
        if proc.returncode != 0:
            stdout = stdout.rsplit('  lib', 1)[1].split('\n', 1)[1]
            await send_error(ctx.edit_original_response,
                             'Failed to convert audio:\n'
                             f'```\n{stdout}\n```')
            return
    finally:
        cleanup_failure(fn, root)
    with open(root / 'sound.json', 'w') as f:
        json.dump({'text': text, 'name': description}, f)
    load_guild(ctx.guild.id)
    await client.tree.sync(guild=ctx.guild)
    await ctx.edit_original_response(content=f'Successfully added/modified `/{name}`')

class CommandTextModal(discord.ui.Modal):

    name = discord.ui.TextInput(
        label='Command Name',
        placeholder='The /name of the command (alphanumeric only).',
        required=True,
    )
    text = discord.ui.TextInput(
        label='Command Text',
        placeholder='The exact text to use as the command text.',
        required=True,
        style=discord.TextStyle.paragraph,
    )
    description = discord.ui.TextInput(
        label='Command Description',
        placeholder='The name of the sound effect ("Play a <desc> sound effect").',
        required=True,
    )
    link: Optional[discord.ui.TextInput] = None
    file: Optional[discord.Attachment] = None

    def __init__(self, file: Optional[discord.Attachment] = None) -> None:
        super().__init__(title='Command Details')
        if file is None:
            self.link = discord.ui.TextInput(
                label='Link to Sound',
                placeholder='Link to the ffmpeg- (incl. file extension) or '
                'youtube-dl-compatible sound-containing file to play.',
                required=True,
                style=discord.TextStyle.paragraph,
            )
            self.add_item(self.link)
        else:
            self.file = file

    async def interaction_check(self, ctx: discord.Interaction) -> bool:
        return await cmd_name_check(ctx, self.name.value)

    async def on_submit(self, ctx: discord.Interaction) -> None:
        await create_cmd(ctx, self.name.value, self.text.value,
                         self.description.value, self.file,
                         None if self.link is None else self.link.value)

@client.tree.command()
@app_commands.describe(
    file='Upload the ffmpeg-compatible sound-containing file to play '
    '(instead of linking to it).',
)
@app_commands.guild_only
async def cmd(ctx: discord.Interaction,
              file: Optional[discord.Attachment] = None) -> None:
    """Create a new guild command."""
    await ctx.response.send_modal(CommandTextModal(file))

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

async def cmd_check(ctx: discord.Interaction) -> bool:
    assert isinstance(ctx.user, discord.Member)
    if not ctx.user.guild_permissions.manage_guild:
        await send_error(ctx.response.send_message,
                         'You must have Manage Server '
                         'permissions to use this command.')
        return False
    return True

async def del_check(ctx: discord.Interaction) -> bool:
    return await cmd_name_check(ctx, ctx.namespace.name.casefold())

async def cmd_name_check(ctx: discord.Interaction, name: str) -> bool:
    if not NAME_REGEX.match(name):
        await send_error(ctx.response.send_message,
                         'Command name must consist '
                         'only of 1-32 letters and numbers')
        return False
    return True

cmd.add_check(cmd_check)
del_cmd.add_check(cmd_check)
del_cmd.add_check(del_check)

load_guild(None)
for sid in os.listdir(Path('sounds') / '.guild'):
    if not sid.isdigit():
        continue
    load_guild(int(sid))
client.run(CONFIG['token'], log_handler=None)
