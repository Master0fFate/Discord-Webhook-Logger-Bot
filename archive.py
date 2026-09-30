"""Bounded, temporary attachment copies for a Discord webhook upload.

Only call this for an authorized message-create event, while the source CDN
links are still available. The caller owns successful files and must call
``cleanup_attachments`` after the upload (or use ``archived_attachments``).
No signed URLs or exception messages are included in the returned notices.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, AsyncIterator, Iterable, Mapping
from urllib.parse import urlsplit

import aiohttp


TRUSTED_CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
DEFAULT_CONTENT_TYPES = frozenset({
    "image/*", "audio/*", "video/*", "text/plain", "application/pdf",
    "application/octet-stream",
})
CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class ArchivePolicy:
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024
    max_files: int = 4
    allowed_content_types: frozenset[str] = DEFAULT_CONTENT_TYPES
    timeout_seconds: float = 30.0
    temp_dir: Path | str | None = None
    max_attempts: int = 3
    retry_base_seconds: float = 0.5
    max_retry_delay_seconds: float = 10.0

    def __post_init__(self) -> None:
        for name in ("max_file_bytes", "max_total_bytes", "max_files"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if (isinstance(self.max_attempts, bool)
                or not isinstance(self.max_attempts, int) or self.max_attempts < 1):
            raise ValueError("max_attempts must be a positive integer")
        for name in ("timeout_seconds", "retry_base_seconds", "max_retry_delay_seconds"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be positive and finite")
        if isinstance(self.allowed_content_types, str):
            raise ValueError("allowed_content_types must be a collection of MIME types")
        normalized = frozenset(str(value).lower().strip() for value in self.allowed_content_types)
        for value in normalized:
            if not re.fullmatch(r"(?:[a-z0-9!#$&^_.+-]+/([a-z0-9!#$&^_.+-]+|\*)|\*/\*)", value):
                raise ValueError("allowed_content_types contains an invalid MIME type")
        object.__setattr__(self, "allowed_content_types", normalized)


@dataclass(frozen=True)
class ArchivedFile:
    path: Path
    filename: str
    content_type: str
    size: int


def _value(attachment: Any, key: str, default: Any = None) -> Any:
    if isinstance(attachment, Mapping):
        return attachment.get(key, default)
    return getattr(attachment, key, default)


def _trusted_url(url: Any) -> bool:
    if not isinstance(url, str) or not url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return False
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname in TRUSTED_CDN_HOSTS
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
            and not parts.fragment
            and "\\" not in url
        )
    except ValueError:
        return False


def sanitize_filename(filename: Any) -> str:
    """Return a short, header-safe basename, never a caller-provided path."""
    name = re.split(r"[/\\]", str(filename or "attachment"))[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    name = name[:120].rstrip(".") or "attachment"
    if name.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        name = "_" + name
    return name


def _unique_filename(filename: str, used: set[str]) -> str:
    candidate = filename
    index = 2
    path = Path(filename)
    while candidate.casefold() in used:
        candidate = f"{path.stem}_{index}{path.suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _mime_type(value: Any) -> str:
    return str(value or "").split(";", 1)[0].lower().strip()


def _allowed(content_type: str, policy: ArchivePolicy) -> bool:
    # Do not forward malformed response headers as multipart header values.
    if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", content_type):
        return False
    return (
        content_type in policy.allowed_content_types
        or content_type.split("/", 1)[0] + "/*" in policy.allowed_content_types
        or "*/*" in policy.allowed_content_types
    )


def _declared_size(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = int(value)
        return result if result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def cleanup_attachments(files: Iterable[ArchivedFile]) -> None:
    """Remove temporary files after webhook upload; safe to call twice."""
    first_error: OSError | None = None
    for file in files:
        try:
            file.path.unlink(missing_ok=True)
        except OSError as error:
            # One filesystem failure must not abandon the other copies.
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


@dataclass
class _TransferBudget:
    consumed: int = 0


class _RetryableDownload(Exception):
    def __init__(self, notice: str, retry_after: str | None = None):
        super().__init__(notice)
        self.notice = notice
        self.retry_after = retry_after


def _retry_delay(retry_after: str | None, attempt: int, policy: ArchivePolicy) -> float | None:
    """None means the server's minimum wait exceeds our bounded retry window."""
    delay = min(policy.max_retry_delay_seconds, policy.retry_base_seconds * (2 ** min(attempt, 30)))
    if retry_after:
        try:
            requested = float(retry_after)
        except (ValueError, TypeError):
            try:
                timestamp = parsedate_to_datetime(retry_after)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                requested = (timestamp - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                requested = 0.0
        if math.isfinite(requested):
            delay = max(delay, requested)
        elif requested > 0:
            return None
    return delay if delay <= policy.max_retry_delay_seconds else None


async def _download_once(
    session: aiohttp.ClientSession,
    url: str,
    filename: str,
    supplied_type: str,
    policy: ArchivePolicy,
    budget: _TransferBudget,
    limit: int,
) -> tuple[ArchivedFile | None, str | None]:
    path: Path | None = None
    try:
        async with session.get(
            url, allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=policy.timeout_seconds),
            headers={"Accept-Encoding": "identity"}, auto_decompress=False,
        ) as response:
            if response.status == 429 or 500 <= response.status <= 599:
                raise _RetryableDownload(f"unavailable (HTTP {response.status})", response.headers.get("Retry-After"))
            if response.status != 200:
                return None, f"unavailable (HTTP {response.status})."
            encoding = response.headers.get("Content-Encoding", "identity").lower().strip()
            if encoding not in ("", "identity"):
                return None, "skipped encoded response."
            content_type = _mime_type(response.headers.get("Content-Type")) or supplied_type or "application/octet-stream"
            if not _allowed(content_type, policy):
                return None, "skipped disallowed response content type."
            response_size = _declared_size(response.headers.get("Content-Length"))
            if response_size is not None and response_size > limit:
                return None, "skipped response size above archive limit."

            fd, temp_name = tempfile.mkstemp(prefix="discord-archive-", suffix=".tmp", dir=policy.temp_dir)
            path = Path(temp_name)
            size = 0
            try:
                output = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with output:
                while True:
                    # At most one chunk is in memory, including the look-ahead
                    # byte needed to detect an oversized body.
                    chunk = await response.content.read(min(CHUNK_BYTES, limit - size + 1))
                    if not chunk:
                        break
                    budget.consumed += len(chunk)
                    if size + len(chunk) > limit:
                        return None, "skipped actual size above archive limit."
                    output.write(chunk)
                    size += len(chunk)
            file = ArchivedFile(path, filename, content_type, size)
        # The response context can itself fail or be cancelled on exit. Keep
        # ownership until it has closed successfully so that no copy is leaked.
        path = None
        return file, None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise _RetryableDownload("download failed or timed out") from None
    except OSError:
        return None, "download failed: temporary file unavailable."
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


async def collect_attachments(
    session: aiohttp.ClientSession,
    attachments: Iterable[Any],
    policy: ArchivePolicy = ArchivePolicy(),
) -> tuple[list[ArchivedFile], list[str]]:
    """Download the first ``max_files`` attachments with bounded memory/disk.

    The total budget includes bytes read from files that later fail validation.
    A single look-ahead byte may be read to detect a lying size or missing size,
    but is never written beyond the configured limits. A failed or cancelled
    collection removes every temporary file it created before propagating.
    Transient failures get a bounded number of retries with cancellable backoff.
    HTTP 403/404 and policy rejection never retry. MIME checks use metadata, not
    malware scanning. Individual failures become safe notices, not batch errors.
    """
    files: list[ArchivedFile] = []
    notices: list[str] = []
    used_names: set[str] = set()
    budget = _TransferBudget()
    try:
        for index, attachment in enumerate(attachments):
            if index >= policy.max_files:
                notices.append("Further attachments skipped: file-count limit reached.")
                break
            filename = _unique_filename(sanitize_filename(_value(attachment, "filename")), used_names)
            remaining = policy.max_total_bytes - budget.consumed
            limit = min(policy.max_file_bytes, remaining)
            if limit <= 0:
                notices.append("Further attachments skipped: archive byte budget exhausted.")
                break
            url = _value(attachment, "url")
            if not _trusted_url(url):
                notices.append(f"{filename}: skipped untrusted attachment URL.")
                continue
            declared = _declared_size(_value(attachment, "size"))
            if declared is not None and declared > limit:
                notices.append(f"{filename}: skipped declared size above archive limit.")
                continue
            supplied_type = _mime_type(_value(attachment, "content_type"))
            if supplied_type and not _allowed(supplied_type, policy):
                notices.append(f"{filename}: skipped disallowed content type.")
                continue

            for attempt in range(policy.max_attempts):
                limit = min(policy.max_file_bytes, policy.max_total_bytes - budget.consumed)
                if limit <= 0 or (declared is not None and declared > limit):
                    notices.append(f"{filename}: skipped remaining archive byte budget too small.")
                    break
                try:
                    file, notice = await _download_once(session, url, filename, supplied_type, policy, budget, limit)
                except _RetryableDownload as error:
                    if attempt + 1 >= policy.max_attempts:
                        notices.append(f"{filename}: {error.notice}; retry limit reached.")
                        break
                    delay = _retry_delay(error.retry_after, attempt, policy)
                    if delay is None:
                        notices.append(f"{filename}: {error.notice}; server backoff exceeds retry window.")
                        break
                    await asyncio.sleep(delay)
                    continue
                if file is not None:
                    files.append(file)
                if notice:
                    notices.append(f"{filename}: {notice}")
                break
    except BaseException:
        cleanup_attachments(files)
        raise
    return files, notices


@asynccontextmanager
async def archived_attachments(
    session: aiohttp.ClientSession,
    attachments: Iterable[Any],
    policy: ArchivePolicy = ArchivePolicy(),
) -> AsyncIterator[tuple[list[ArchivedFile], list[str]]]:
    """Keep copies alive during an upload and clean up on exit or cancellation."""
    files, notices = await collect_attachments(session, attachments, policy)
    try:
        yield files, notices
    finally:
        cleanup_attachments(files)
