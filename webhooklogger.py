import os
import sys
import logging
import asyncio
import discord
import aiohttp
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / '.env'


def _load_env():
    if not ENV_PATH.exists():
        ENV_PATH.write_text(
            'DISCORD_TOKEN=PASTE_YOUR_BOT_TOKEN_HERE\n'
            'DISCORD_WEBHOOK_URL=PASTE_YOUR_WEBHOOK_URL_HERE\n'
        )
        sys.exit(f'Created .env file at {ENV_PATH} — fill in your credentials and restart.')

    for line in ENV_PATH.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        if value and key not in os.environ:
            os.environ[key] = value


_load_env()

TOKEN = os.environ.get('DISCORD_TOKEN')
WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')

if not TOKEN or not WEBHOOK_URL or 'PASTE_' in TOKEN or 'PASTE_' in WEBHOOK_URL:
    sys.exit(f'Open {ENV_PATH} and fill in DISCORD_TOKEN and DISCORD_WEBHOOK_URL')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('ExternalLogger')

intents = discord.Intents.default()
intents.message_content = True
intents.guild_messages = True

client = discord.Client(intents=intents)
session = None


def _resolve_location(channel):
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        if isinstance(parent, discord.ForumChannel):
            return (
                f'Forum: {parent.name} > {channel.name}',
                f'Forum ID: {parent.id} | Thread ID: {channel.id}',
            )
        if parent:
            return (
                f'{parent.name} > Thread: {channel.name}',
                f'Channel ID: {parent.id} | Thread ID: {channel.id}',
            )
        return f'Thread: {channel.name}', f'Thread ID: {channel.id}'
    return f'Channel: {channel.name}', f'Channel ID: {channel.id}'


def _tr(text, limit=1024):
    return text[:limit - 3] + '...' if len(text) > limit else text


def _build_embed(message, event='Message'):
    author = message.author
    member = author if isinstance(author, discord.Member) else None
    location, ids = _resolve_location(message.channel)

    event_colors = {'Message': 0x5865F2, 'Edited': 0xFEE75C, 'Deleted': 0xED4245}
    color = event_colors.get(event, 0x5865F2)
    if event == 'Message' and member and member.color.value:
        color = member.color.value

    embed = {
        'title': event,
        'color': color,
        'timestamp': message.created_at.isoformat(),
        'footer': {'text': f'Message ID: {message.id} | {ids}'},
    }

    display = member.nick if member and member.nick else (author.global_name or author.name)
    author_block = {'name': f'{display} (@{author.name}, {author.id})'}
    if author.display_avatar:
        author_block['icon_url'] = str(author.display_avatar.url)
    embed['author'] = author_block

    desc_parts = [
        f'**Server:** {message.guild.name} ({message.guild.id})',
        f'**Location:** {location}',
    ]

    if message.reference:
        ref = message.reference.resolved
        if ref and isinstance(ref, discord.Message):
            ref_text = ref.content[:80] or '[Attachment/Embed]'
            desc_parts.append(f'**Reply to:** {ref.author.name} ({ref.author.id}): {ref_text}')
        elif message.reference.message_id:
            desc_parts.append(f'**Reply to:** Message {message.reference.message_id}')

    embed['description'] = '\n'.join(desc_parts)

    fields = []

    if message.content:
        fields.append({'name': 'Content', 'value': _tr(message.content), 'inline': False})

    if message.attachments:
        lines = []
        for a in message.attachments:
            entry = f'[{a.filename}]({a.url})'
            if a.filename.endswith('.ogg'):
                entry += ' [Voice Message]'
            if a.description:
                entry += f' \u2014 alt: {a.description}'
            lines.append(entry)
        fields.append({
            'name': f'Attachments ({len(message.attachments)})',
            'value': _tr('\n'.join(lines)),
            'inline': False,
        })

    if message.stickers:
        lines = [f'{s.name} ({s.id})' for s in message.stickers]
        fields.append({
            'name': f'Stickers ({len(message.stickers)})',
            'value': _tr('\n'.join(lines)),
            'inline': False,
        })

    if message.embeds:
        lines = []
        for e in message.embeds:
            parts = []
            if e.title:
                parts.append(e.title)
            if e.description:
                parts.append(e.description[:120])
            lines.append(' | '.join(parts) if parts else '[Rich Embed]')
        fields.append({
            'name': f'Embeds ({len(message.embeds)})',
            'value': _tr('\n'.join(lines)),
            'inline': False,
        })

    embed['fields'] = fields
    return embed


async def _send_webhook(embed):
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession()
    try:
        async with session.post(WEBHOOK_URL, json={'embeds': [embed]}) as resp:
            if resp.status not in (200, 204):
                log.warning('Webhook returned status %d', resp.status)
    except aiohttp.ClientError as e:
        log.error('Webhook failed: %s', e)


@client.event
async def on_ready():
    log.info('Logged in as %s (%s) \u2014 watching %d guild(s)', client.user.name, client.user.id, len(client.guilds))


@client.event
async def on_message(message):
    if message.guild is None or message.author.bot:
        return
    try:
        await _send_webhook(_build_embed(message))
    except Exception as e:
        log.error('Failed to log message %s: %s', message.id, e)


@client.event
async def on_message_edit(before, after):
    if after.guild is None or after.author.bot:
        return
    if before.content == after.content:
        return
    try:
        embed = _build_embed(after, event='Edited')
        embed['fields'] = [
            {'name': 'Before', 'value': _tr(before.content or '[Empty]'), 'inline': False},
            {'name': 'After', 'value': _tr(after.content or '[Empty]'), 'inline': False},
        ]
        await _send_webhook(embed)
    except Exception as e:
        log.error('Failed to log edit %s: %s', after.id, e)


@client.event
async def on_message_delete(message):
    if message.guild is None or message.author.bot:
        return
    try:
        await _send_webhook(_build_embed(message, event='Deleted'))
    except Exception as e:
        log.error('Failed to log delete %s: %s', message.id, e)


async def main():
    global session
    try:
        await client.start(TOKEN)
    finally:
        if session and not session.closed:
            await session.close()


if __name__ == '__main__':
    asyncio.run(main())
