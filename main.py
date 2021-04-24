import sys
import os
import json
import subprocess
import asyncio
import discord
from discord.ext import slash

SRCDIR = os.path.dirname(os.path.abspath(__file__))
VERSION = subprocess.check_output(
    f"cd {SRCDIR} && git rev-parse --short HEAD", shell=True
).decode('ascii').strip()
CONFIG_FILE = 'buttonbot.json'

with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

client = slash.SlashBot(
    description='A bot that plays sound effects',
    command_prefix='/',
    help_command=None,
    activity=discord.Activity(type=discord.ActivityType.watching, name='/'),
    debug_guild=CONFIG.get('guild_id', None)
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
    await ctx.respond('Hello World!', flags=slash.MessageFlags.EPHEMERAL)

@client.slash_cmd()
async def invite(ctx: slash.Context):
    """Get a link to add the bot in your own server."""
    url = CONFIG.get('url', 'No invite configured! Contact bot owner.')
    await ctx.respond(url, flags=slash.MessageFlags.EPHEMERAL)

@client.slash_cmd()
async def version(ctx: slash.Context):
    """Get the Git version this bot is running on."""
    global VERSION
    VERSION = subprocess.check_output(
        f"cd {SRCDIR} && git rev-parse --short HEAD", shell=True
    ).decode('ascii').strip()
    await ctx.respond(f'`{VERSION}`', flags=slash.MessageFlags.EPHEMERAL)

async def wakeup():
    await client.wait_until_ready()
    print('ButtonBot Ready')
    while 1:
        try:
            await asyncio.sleep(1)
        except:
            await client.close()
            return

client.loop.create_task(wakeup())
print('ButtonBot Running')
client.run(CONFIG['token'])
