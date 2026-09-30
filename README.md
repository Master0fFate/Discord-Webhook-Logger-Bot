# Discord Webhook Logger

A scoped, best-effort moderation logger for Discord guild messages. It records message creates, partial edits, deletes, reactions, forwarded snapshots and modern message forms to one configured webhook. It can copy small attachments while their original URLs are still available, so deleting the original does not remove the copy in the log.

## Install and migrate

Use Python 3.11 or newer:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env  # Windows: copy .env.example .env
python webhooklogger.py
```

Edit `.env` locally, or supply environment variables (environment variables take precedence). Never commit or share your token, webhook URL or `.env`. Imports no longer write configuration files or start the bot. Use a **bot token**, never a user token. There is no command that changes server permissions.

**Migration is intentionally fail-closed:** the old version watched all joined servers. You must now set `LOG_GUILD_IDS` and either `LOG_CHANNEL_IDS` or explicitly `LOG_ALL_CHANNELS=true`. All configured guilds must be servers whose owners authorized this logging. Existing message logging remains available within that scope. To retain broad collection intentionally, list the approved guild IDs and enable `LOG_ALL_CHANNELS`. Empty scope does not silently become “everything.”

Create an ordinary text-channel webhook as the destination. At startup the logger reads its metadata to verify the destination; messages/events in that channel are excluded automatically. Webhooks targeting forum/media channels requiring `thread_id`/`thread_name` are not supported as destinations. Forums and media-channel threads **are supported as sources**. Do not grant unnecessary Administrator permissions: use View Channel for approved source channels and the required thread access. The bot never joins private threads, fetches message history or broadens permissions for you.

In the Developer Portal enable/obtain approval for Message Content Intent if you want message content. Set `MESSAGE_CONTENT_INTENT=false` for metadata-only operation. The logger requests guild, message, reaction and poll-vote intents, not member or presence access. Discord may omit content, embeds, attachments, components, snapshots or polls when intent/permissions do not allow them. Empty fields are not proof the original message was empty.

## Configuration

- `LOG_GUILD_IDS`: required, comma-separated source guild IDs
- `LOG_CHANNEL_IDS`: source channel or thread IDs. A cached thread inherits an allowed parent forum/text/media channel; an uncached thread must be explicitly listed unless all-channel mode is enabled
- `LOG_ALL_CHANNELS=false`: explicit opt-in to all accessible channels in the listed guilds
- `LOG_EXCLUDE_CHANNEL_IDS`: excluded channel/thread/parent IDs override allowlists
- `LOG_BOTS=false`: omit known bot authors. All webhook-authored creates/updates are excluded
- `MESSAGE_CONTENT_INTENT=true`: must also be enabled/approved in Discord
- `ARCHIVE_ATTACHMENTS=true`: reupload permitted attachments; disable to keep metadata/temporary CDN URLs only
- `ARCHIVE_MAX_FILE_BYTES=8388608`: maximum 8 MiB per file, configurable downward
- `ARCHIVE_MAX_TOTAL_BYTES=16777216`: maximum 16 MiB per event, configurable downward
- `ARCHIVE_MAX_FILES=4`: maximum four attempts/files per event, configurable downward
- `ARCHIVE_CONTENT_TYPES`: MIME allowlist, defaults in `.env.example`; `image/*`, `audio/*`, `video/*`, plain text, PDF and generic binary are allowed. This is a metadata filter, **not malware detection**; tighten it for your community
- `QUEUE_SIZE=20`: bounded event/capture queues, permitted range 1–100
- `CACHE_SIZE=500`: last-observed message records and archive-log links, range 0–5000; zero disables retention in these in-memory caches

The 8 MiB per-file ceiling is deliberately below Discord's current documented default 20 MiB per-file upload limit. Larger source/Nitro allowances do not raise this logger's cap. 800 MB files are rejected without downloading. There is no automatic cap increase based on source guild or account boosts; destination restrictions can still reject a payload and are reported by HTTP status. An archive batch is at most 16 MiB plus multipart metadata.

## Coverage and semantics

- Text, author information, guild/channel IDs, thread/parent labels when cached, attachments and voice-file metadata, stickers, rich embeds, polls, components, flags, system/type IDs and interaction metadata
- Forwarded `message_snapshots`, including their permitted attachments. The displayed outer author is the forwarding user; snapshots do not provide the original author's identity. No original-source message is fetched and no reply's resolved content is copied from outside the authorized scope
- Raw partial updates preserve omitted fields in the bounded last-observed cache and distinguish missing history from an empty value. Non-text updates are logged too
- Raw delete and bulk-delete events work without Discord's message cache. Cached last-observed content is included where available. Deletions after cache eviction/restart contain IDs and an explicit unavailable notice
- Raw reaction add/remove/remove-all/remove-emoji and poll vote add/remove events include only the IDs/emoji information Discord actually supplied. No actor is invented for reaction clears, and no uncached message lookup is performed
- Future/unknown numeric message types retain their type and supported fields. Unknown new fields are not automatically exported. Message fields larger than 16,000 JSON characters are explicitly omitted; the webhook display is bounded and marked when truncated. This is not a lossless audit archive

Known excluded bot/webhook message IDs are remembered in a bounded in-memory cache. Bot/webhook identity is absent in some partial/deletion events. Metadata-only uncached events may therefore appear even when their original author would have been filtered. Reaction events describe the supplied reacting user ID; the bot does not fetch user profiles to classify them.

## Attachment capture, deletion and retention

Two fixed capture workers receive files at event admission, independently of the webhook delivery worker. On an idle host a fetch can start immediately; there is **no subsecond guarantee**. Bandwidth, DNS, Discord, event scheduling and concurrency limits still apply. Captures waiting over two seconds to start are skipped with a notice. Full delivery queues drop the whole event and increment a logged counter; full capture admission skips bytes but retains the event notice. No unbounded task spawning occurs.

Only HTTPS Discord CDN hosts are fetched, redirects and compressed bodies are refused, and actual streamed bytes are checked even when declared size lies. Downloads use 64 KiB chunks and owner-only temporary files. CDN 429/5xx/network failures retry at most three times with bounded backoff; server-requested waits over ten seconds are reported and skipped rather than shortened. Failed-attempt bytes still count against the total budget. Per-file/count/total/type limits are enforced before and during reads. Files are reuploaded with the create/edit log, then deleted from local disk, including on errors or cancellation. A later deletion log points to the earlier archive log while that bounded link cache is retained. Nothing is downloaded for delete events.

If a source is deleted before its bytes are captured, the URL expires, collection is denied, admission overflows, or upload fails, the logger **cannot promise recovery**. Already captured bytes survive deletion of the source. The archive copy itself remains in the destination until authorized moderators remove it; deleting the original does not purge it. No automated destination retention/deletion policy is implemented. Choose a restricted destination, communicate this retention policy to members, and establish manual retention and deletion procedures before running the bot. Forwarded content can include material originally posted elsewhere; scope applies to where the forward was observed.

With defaults, queued completed captures plus active processing can occupy roughly `(QUEUE_SIZE + 1) × 16 MiB` of temporary disk, plus two at-most-64 KiB streaming chunks; choose queue/byte limits for your host. Queued message fields and bounded caches also consume memory. A hard process crash or power loss may leave `discord-archive-*.tmp` files in the OS temporary directory; remove stale files only when no logger process owns them. Normal shutdown waits up to 15 seconds for delivery, cancels remaining work and removes owned temporary files. Pending events/cache/links do not survive restart.

## Delivery and operations

One webhook worker preserves admitted event order and serializes rate limits. It honors `retry_after` / `Retry-After` and successful response bucket reset hints, retries transient network/5xx failures within five attempts, and stops using a destination returning 401/403/404 until restart. Errors log status/category only, never tokens, URLs, response bodies or message text. Do not enable HTTP/gateway DEBUG logging in production. Mentions are disabled explicitly for every webhook submission, including user, role, everyone and reply mentions.

This is best-effort, not exactly-once delivery: ambiguous network failures can duplicate a webhook send; queue overflow, downtime, permission changes and exhausted retries can lose events. The logger does not retrieve missed historical events. Watch stderr for dropped-event counters, archive notices and destination failures. If destination validation fails at startup, correct configuration/access and restart. Tests never connect a live bot or webhook; one transport integration test uses an ephemeral localhost HTTP server.

## Tests

```sh
python -m unittest discover -s tests -v
python -m compileall -q webhooklogger.py archive.py
```

GitHub Actions runs the offline suite on Python 3.11, 3.12 and 3.13. Mocks cover current-format rendering, scope, raw/uncached edits/deletes/reactions, archive admission, retries, mention suppression, size/type/URL validation, deletion timing, streamed size mismatches and cleanup/cancellation.

## API references

- [Discord message structures, snapshots and content restrictions](https://docs.discord.com/developers/resources/message)
- [Discord file upload limits](https://docs.discord.com/developers/reference#uploading-files)
- [Discord webhooks and execution payloads](https://docs.discord.com/developers/resources/webhook)
- [Discord rate limits](https://docs.discord.com/developers/topics/rate-limits)
- [Discord gateway events](https://docs.discord.com/developers/events/gateway-events)
- [Discord thread access](https://docs.discord.com/developers/topics/threads)

Reviewed against the current Discord API documentation during this retrofit. API visibility and permissions always bound what can be logged.
