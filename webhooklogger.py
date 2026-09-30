"""Scoped Discord gateway logger. Importing this module never starts the bot."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import logging
import math
import time
import os
from pathlib import Path
import re
from urllib.parse import urlparse

import aiohttp
import discord

from archive import ArchivePolicy, collect_attachments, cleanup_attachments

log = logging.getLogger('ExternalLogger')
MESSAGE_KEYS = ('content', 'type', 'flags', 'attachments', 'embeds', 'sticker_items',
                'poll', 'components', 'message_snapshots', 'message_reference',
                'activity', 'interaction_metadata', 'edited_timestamp', 'pinned')
def cleanup(files):
    try:
        cleanup_attachments(files)
    except OSError:
        log.error('Temporary file cleanup failed; check disk permissions and remove stale archives')


EVENTS = {'MESSAGE_CREATE', 'MESSAGE_UPDATE', 'MESSAGE_DELETE', 'MESSAGE_DELETE_BULK',
          'MESSAGE_REACTION_ADD', 'MESSAGE_REACTION_REMOVE', 'MESSAGE_REACTION_REMOVE_ALL',
          'MESSAGE_REACTION_REMOVE_EMOJI', 'MESSAGE_POLL_VOTE_ADD', 'MESSAGE_POLL_VOTE_REMOVE'}


def ids(value):
    result = frozenset(int(x.strip()) for x in value.split(',') if x.strip())
    if any(x <= 0 for x in result):
        raise ValueError('IDs must be positive integers')
    return result


def flag(env, name, default=False):
    value = env.get(name, str(default)).lower()
    if value not in {'true', 'false', '1', '0'}:
        raise ValueError(f'{name} must be true or false')
    return value in {'true', '1'}


def number(env, name, default, minimum, maximum):
    value = int(env.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}')
    return value


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    webhook: str = field(repr=False)
    guilds: frozenset[int]
    channels: frozenset[int]
    excluded: frozenset[int]
    all_channels: bool = False
    include_bots: bool = False
    content: bool = True
    archive: bool = True
    queue_size: int = 20
    cache_size: int = 500
    archive_policy: ArchivePolicy = field(default_factory=ArchivePolicy)

    @classmethod
    def load(cls, env=None):
        env = os.environ if env is None else env
        token, webhook = env.get('DISCORD_TOKEN', ''), env.get('DISCORD_WEBHOOK_URL', '')
        u = urlparse(webhook)
        if not token or token.startswith('PASTE_'):
            raise ValueError('Set DISCORD_TOKEN to a bot token')
        if (u.scheme != 'https' or u.hostname != 'discord.com' or u.port not in (None, 443)
                or u.username or u.password or u.query or u.fragment
                or not re.fullmatch(r'/api(?:/v10)?/webhooks/[0-9]+/[A-Za-z0-9._-]+', u.path)):
            raise ValueError('DISCORD_WEBHOOK_URL must be an HTTPS discord.com webhook URL without query parameters')
        guilds = ids(env.get('LOG_GUILD_IDS', ''))
        channels = ids(env.get('LOG_CHANNEL_IDS', ''))
        all_channels = flag(env, 'LOG_ALL_CHANNELS')
        if not guilds or not (channels or all_channels):
            raise ValueError('Set LOG_GUILD_IDS and LOG_CHANNEL_IDS, or explicitly LOG_ALL_CHANNELS=true')
        return cls(token, webhook, guilds, channels, ids(env.get('LOG_EXCLUDE_CHANNEL_IDS', '')),
                   all_channels, flag(env, 'LOG_BOTS'), flag(env, 'MESSAGE_CONTENT_INTENT', True),
                   flag(env, 'ARCHIVE_ATTACHMENTS', True),
                   number(env, 'QUEUE_SIZE', 20, 1, 100),
                   number(env, 'CACHE_SIZE', 500, 0, 5000),
                   ArchivePolicy(
                       max_file_bytes=number(env, 'ARCHIVE_MAX_FILE_BYTES', 8*1024*1024, 1, 8*1024*1024),
                       max_total_bytes=number(env, 'ARCHIVE_MAX_TOTAL_BYTES', 16*1024*1024, 1, 16*1024*1024),
                       max_files=number(env, 'ARCHIVE_MAX_FILES', 4, 1, 4),
                       allowed_content_types=frozenset(x.strip() for x in env.get('ARCHIVE_CONTENT_TYPES', 'image/*,audio/*,video/*,text/plain,application/pdf,application/octet-stream').split(',') if x.strip())))


def load_env(path=Path(__file__).with_name('.env')):
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())


def bounded(value, limit=6000):
    """Bound before retaining anything in our own queue/cache, including unknown forms."""
    text = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
    return text if len(text) <= limit else text[:limit - 32] + ' ... [truncated; limit reached]'


def render(event, data, context='', before=None, archive_link=None):
    lines = [f'Guild {data.get("guild_id", "unknown")} | Channel {data.get("channel_id", "unknown")}',
             context, f'Message {data.get("id", data.get("message_id", "unknown"))}']
    if data.get('author'):
        lines.append('Author: ' + bounded(data['author'], 350))
    if event == 'MESSAGE_UPDATE':
        lines.append('Partial update: omitted fields are unchanged/unknown, not empty.')
        lines.append('Before: ' + (bounded(before, 700) if before else '[not retained]'))
    if event.startswith('MESSAGE_DELETE'):
        lines.append('Last observed: ' + (bounded(before, 1800) if before else '[not retained; deleted content cannot be fetched]'))
    if archive_link:
        lines.append('Previously archived log: ' + archive_link)
    for key in (*MESSAGE_KEYS, 'emoji', 'user_id', 'burst', 'burst_colors', 'answer_id', 'ids'):
        if key in data:
            lines.append(f'{key}: {bounded(data[key], 1800)}')
    if not any(data.get(k) for k in ('content', 'attachments', 'embeds', 'message_snapshots', 'poll')):
        lines.append('No content supplied in this event. This may be metadata-only or restricted by Message Content Intent.')
    text = '\n'.join(x for x in lines if x)
    if len(text) > 3900:
        text = text[:3850] + '\n[Event display truncated at configured limit]'
    return {'embeds': [{'title': event.replace('MESSAGE_', '').replace('_', ' ').title(),
                        'description': text}], 'allowed_mentions': {'parse': [], 'users': [], 'roles': [], 'replied_user': False}}


class Sender:
    """One worker owns this transport; no parallel requests bypass a cooldown."""
    def __init__(self, session, url, sleep=asyncio.sleep):
        self.session, self.url, self.sleep = session, url, sleep
        self.disabled = False

    async def send(self, payload, files=()):
        if self.disabled:
            return None
        for attempt in range(5):
            handles = []
            try:
                kwargs = {'params': {'wait': 'true'}, 'allow_redirects': False}
                if files:
                    form = aiohttp.FormData()
                    form.add_field('payload_json', json.dumps(payload), content_type='application/json')
                    for i, file in enumerate(files):
                        handle = open(file.path, 'rb')
                        handles.append(handle)
                        form.add_field(f'files[{i}]', handle, filename=file.filename, content_type=file.content_type)
                    kwargs['data'] = form
                else:
                    kwargs['json'] = payload
                async with self.session.post(self.url, **kwargs) as response:
                    status = response.status
                    # Never log response bodies or exception strings: either can contain credentials/content.
                    if status == 200:
                        try:
                            result = await response.json()
                        except (ValueError, aiohttp.ClientError, asyncio.TimeoutError):
                            log.warning('Webhook delivered but receipt metadata unavailable; will not retry')
                            result = {}
                        if not isinstance(result, dict):
                            result = {}
                        if response.headers.get('X-RateLimit-Remaining') == '0':
                            await self.sleep(self.delay(response.headers.get('X-RateLimit-Reset-After'), 1))
                        return result
                    if status in (401, 403, 404):
                        self.disabled = True
                        log.error('Webhook disabled after HTTP %s; verify destination and restart', status)
                        return None
                    if status == 429:
                        try:
                            body = await response.json()
                        except (ValueError, aiohttp.ClientError):
                            body = {}
                        delay = self.delay(body.get('retry_after', response.headers.get('Retry-After')), 2 ** attempt)
                    elif status >= 500:
                        delay = 2 ** attempt
                    else:
                        log.error('Webhook rejected event (HTTP %s)', status)
                        return None
                if attempt < 4:
                    await self.sleep(delay)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                if attempt < 4:
                    await self.sleep(2 ** attempt)
            finally:
                for handle in handles:
                    handle.close()
        log.error('Webhook event dropped after retry budget; uncertain network failures can duplicate delivery')
        return None

    @staticmethod
    def delay(value, default):
        try:
            delay = float(value)
            if not math.isfinite(delay) or delay < 0:
                raise ValueError
            return delay
        except (TypeError, ValueError):
            # An unusually long cooldown must never be shortened into repeated 429s.
            return 300 if value is not None else default


class Logger(discord.Client):
    def __init__(self, config):
        intents = discord.Intents.none()
        intents.guilds = intents.guild_messages = intents.guild_reactions = True
        intents.message_content = config.content
        intents.guild_polls = True
        super().__init__(intents=intents, max_messages=None, enable_debug_events=True)
        self.config = config
        self.queue = asyncio.Queue(maxsize=config.queue_size)
        self.cache = OrderedDict()
        self.ignored = OrderedDict()
        self.links = OrderedDict()
        self.worker = None
        self.capture_workers = []
        self.captures = {}
        self.destination_id = None
        self.destination_guild_id = None
        self.session = None
        self.dropped = 0

    async def on_error(self, event_method, *args, **kwargs):
        log.error('Gateway handler failed (%s); event details suppressed', event_method)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        self.sender = Sender(self.session, self.config.webhook)
        # Resolve the configured destination before accepting events; never log into its source.
        async with self.session.get(self.config.webhook, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError('Cannot verify webhook destination')
            destination = await response.json()
            self.destination_id = int(destination['channel_id'])
            self.destination_guild_id = int(destination['guild_id'])
        self.capture_queue = asyncio.Queue(maxsize=self.config.queue_size)
        self.capture_workers = [asyncio.create_task(self.capture()) for _ in range(2)]
        self.worker = asyncio.create_task(self.consume())
        log.info('Configured %s guilds; archive=%s; content intent=%s', len(self.config.guilds), self.config.archive, self.config.content)

    def scope(self, data):
        guild_id, channel_id = int(data.get('guild_id', 0)), int(data.get('channel_id', 0))
        channel = self.get_channel(channel_id)
        parent_id = getattr(channel, 'parent_id', None)
        candidates = {channel_id, parent_id}
        return (channel_id != self.destination_id and guild_id in self.config.guilds and not candidates.intersection(self.config.excluded)
                and (self.config.all_channels or bool(candidates.intersection(self.config.channels))))

    async def on_socket_raw_receive(self, raw):
        # discord.py emits the decompressed JSON text through this documented debug event.
        # Do not log it: READY and other events may contain sensitive session data.
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(payload, dict):
            await self.handle_event(payload)

    async def handle_event(self, payload):
        event, data = payload.get('t'), payload.get('d')
        if event not in EVENTS or not isinstance(data, dict) or not self.scope(data):
            return
        key = (str(data.get('channel_id')), str(data.get('id', data.get('message_id'))))
        if key in self.ignored:
            return
        # Ignore every webhook to avoid feedback across logger deployments.
        if data.get('webhook_id') or (data.get('author', {}).get('bot') and not self.config.include_bots):
            self.remember(self.ignored, key, True)
            return
        # Keep raw fields needed for forwarding without retaining unbounded gateway objects.
        clean = {k: data[k] for k in (*MESSAGE_KEYS, 'id', 'message_id', 'guild_id', 'channel_id',
                  'author', 'emoji', 'user_id', 'burst', 'burst_colors', 'answer_id', 'ids') if k in data}
        if not self.config.content:
            for key in ('content', 'attachments', 'embeds', 'components', 'message_snapshots', 'poll'):
                clean.pop(key, None)
        for key in list(clean):
            if len(json.dumps(clean[key], ensure_ascii=True)) > 16000:
                clean[key] = '[omitted: field exceeds 16000 characters]'
        try:
            if self.queue.full():
                raise asyncio.QueueFull
            future = None
            if self.config.archive and event in {'MESSAGE_CREATE', 'MESSAGE_UPDATE'}:
                attachments = clean.get('attachments', [])
                # Capture forwarded attachments too, without fetching original source messages.
                snapshots = clean.get('message_snapshots', [])
                if isinstance(snapshots, list):
                    attachments = list(attachments) if isinstance(attachments, list) else []
                    for snapshot in snapshots:
                        if isinstance(snapshot, dict) and isinstance(snapshot.get('message'), dict):
                            extra = snapshot['message'].get('attachments', [])
                            if isinstance(extra, list):
                                attachments.extend(extra)
                if isinstance(attachments, list) and attachments:
                    future = asyncio.get_running_loop().create_future()
                    if self.capture_queue.full():
                        future.set_result(([], ['Capture admission full; bytes not archived']))
                    else:
                        self.capture_queue.put_nowait((attachments, future, time.monotonic()))
                        self.captures[future] = None
            self.queue.put_nowait((event, clean, future))
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                log.warning('Queue full; dropped event (total=%s)', self.dropped)

    def remember(self, mapping, key, value):
        if self.config.cache_size == 0:
            return
        mapping[key] = value
        mapping.move_to_end(key)
        while len(mapping) > self.config.cache_size:
            mapping.popitem(last=False)

    async def capture(self):
        while True:
            attachments, future, received = await self.capture_queue.get()
            try:
                if time.monotonic() - received > 2:
                    future.set_result(([], ['Capture admission expired after 2 seconds; bytes not archived']))
                    continue
                result = await collect_attachments(self.session, attachments, self.config.archive_policy)
                self.captures[future] = result[0]
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                future.cancel()
                raise
            except Exception:
                if not future.done():
                    future.set_result(([], ['Capture failed; bytes not archived']))
            finally:
                self.capture_queue.task_done()

    async def consume(self):
        while True:
            event, data, future = await self.queue.get()
            try:
                await self.process(event, data, future)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error('Event processing failed; details suppressed to protect message content and credentials')
            finally:
                self.queue.task_done()

    async def process(self, event, data, future=None):
        if event == 'MESSAGE_DELETE_BULK':
            for message_id in data.get('ids', []):
                if isinstance(message_id, (str, int)):
                    await self.process('MESSAGE_DELETE', {**data, 'id': str(message_id), 'ids': []})
            return
        key = (str(data['channel_id']), str(data.get('id', data.get('message_id'))))
        if key in self.ignored:
            return
        before = self.cache.get(key)
        channel = self.get_channel(int(data['channel_id']))
        parent = getattr(channel, 'parent', None)
        context = f'Location: {getattr(parent, "name", "")} / {getattr(channel, "name", "uncached channel")}'
        if getattr(channel, 'parent_id', None):
            context += f' (thread parent {channel.parent_id})'
        payload = render(event, data, context, before, self.links.get(key))
        records = []
        try:
            if future:
                records, notices = await future
                if notices:
                    payload['embeds'][0]['description'] += '\nArchive: ' + bounded(notices, 150)
            result = await self.sender.send(payload, records)
            if result and records and result.get('id') and result.get('channel_id'):
                self.remember(self.links, key, f'https://discord.com/channels/{self.destination_guild_id}/{result["channel_id"]}/{result["id"]}')
        finally:
            cleanup(records)
            if future:
                self.captures.pop(future, None)
        if event in {'MESSAGE_CREATE', 'MESSAGE_UPDATE'}:
            merged = {**(before or {}), **data}
            self.remember(self.cache, key, merged)
        elif event == 'MESSAGE_DELETE':
            self.cache.pop(key, None)
            self.links.pop(key, None)

    async def close(self):
        if self.worker:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=15)
            except asyncio.TimeoutError:
                log.warning('Shutdown drain timed out; pending events discarded')
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        for task in self.capture_workers:
            task.cancel()
        await asyncio.gather(*self.capture_workers, return_exceptions=True)
        for records in self.captures.values():
            cleanup(records or [])
        self.captures.clear()
        if self.session:
            await self.session.close()
        await super().close()


async def main():
    load_env()
    config = Config.load()
    async with Logger(config) as client:
        await client.start(config.token)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    # discord.py HTTP debugging may include request bodies; do not enable DEBUG in production.
    try:
        asyncio.run(main())
    except Exception as error:
        log.error('Startup failed (%s); check configuration, bot token and approved intents', type(error).__name__)
        raise SystemExit(1) from None
