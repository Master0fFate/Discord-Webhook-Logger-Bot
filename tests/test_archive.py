import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from archive import (
    ArchivePolicy, ArchivedFile, archived_attachments, cleanup_attachments,
    collect_attachments, sanitize_filename,
)


def attachment(**updates):
    values = {
        "url": "https://cdn.discordapp.com/attachments/123/456/test.txt?ex=abc&hm=secret",
        "filename": "test.txt",
        "size": 3,
        "content_type": "text/plain",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def response(body=b"abc", *, status=200, headers=None, failure=None):
    result = SimpleNamespace(status=status, headers=headers if headers is not None else {"Content-Type": "text/plain"})
    offset = 0

    async def read(size):
        nonlocal offset
        if failure is not None and offset:
            raise failure
        chunk = body[offset:offset + size]
        offset += len(chunk)
        return chunk

    result.content = SimpleNamespace(read=AsyncMock(side_effect=read))
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=result)
    manager.__aexit__ = AsyncMock(return_value=False)
    return result, manager


def session_for(*responses):
    return SimpleNamespace(get=MagicMock(side_effect=[manager for _, manager in responses]))


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.policy = ArchivePolicy(temp_dir=self.directory.name, max_attempts=1)

    def assert_no_files(self):
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    async def test_success_streams_to_private_tempfile_and_cleanup_is_idempotent(self):
        prepared = response()
        session = session_for(prepared)
        files, notices = await collect_attachments(session, [attachment()], self.policy)
        self.assertEqual(notices, [])
        self.assertEqual(len(files), 1)
        file = files[0]
        self.assertEqual(file.path.read_bytes(), b"abc")
        self.assertEqual(file.filename, "test.txt")
        self.assertEqual(file.content_type, "text/plain")
        self.assertEqual(file.size, 3)
        self.assertEqual(file.path.stat().st_mode & 0o777, 0o600)
        args, kwargs = session.get.call_args
        self.assertEqual(args, (attachment().url,))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertFalse(kwargs["auto_decompress"])
        self.assertEqual(kwargs["headers"], {"Accept-Encoding": "identity"})
        self.assertEqual(kwargs["timeout"].total, 30)
        cleanup_attachments(files)
        cleanup_attachments(files)
        self.assert_no_files()

    async def test_trusted_proxy_host_and_dictionary_input(self):
        session = session_for(response())
        item = vars(attachment(url="https://media.discordapp.net:443/attachments/a/b/file.txt"))
        async with archived_attachments(session, [item], self.policy) as (files, notices):
            self.assertEqual(len(files), 1)
            self.assertEqual(notices, [])
            path = files[0].path
            self.assertTrue(path.exists())
        self.assertFalse(path.exists())

    async def test_untrusted_urls_never_contact_network(self):
        bad_urls = [
            "http://cdn.discordapp.com/a", "https://cdn.discordapp.com.evil.test/a",
            "https://evil.test/cdn.discordapp.com/a", "https://cdn.discordapp.com@evil.test/a",
            "https://user:pass@cdn.discordapp.com/a", "https://cdn.discordapp.com:444/a",
            "file:///etc/passwd", "https://127.0.0.1/a", "https://[::1]/a",
            "https://cdn.discordapp.com/a#fragment", " https://cdn.discordapp.com/a",
            "https://cdn.discordapp.com/a\r\nX-Evil: yes", "https://cdn.discordapp.com\\@evil.test/a",
            "https://cdn.discordapp.com:invalid/a", "https://cdn.discordapp.com./a", None,
        ]
        for url in bad_urls:
            with self.subTest(url=url):
                session = session_for()
                files, notices = await collect_attachments(session, [attachment(url=url)], self.policy)
                session.get.assert_not_called()
                self.assertEqual(files, [])
                self.assertIn("untrusted", notices[0])
        self.assert_no_files()

    async def test_redirect_is_not_followed_or_archived(self):
        prepared = response(status=302, headers={"Location": "http://127.0.0.1/private"})
        session = session_for(prepared)
        files, notices = await collect_attachments(session, [attachment()], self.policy)
        self.assertEqual(files, [])
        self.assertIn("HTTP 302", notices[0])
        self.assertEqual(session.get.call_count, 1)
        prepared[0].content.read.assert_not_called()
        self.assert_no_files()

    async def test_attachment_deleted_before_fetch_reports_unavailable(self):
        for status in (403, 404, 410):
            with self.subTest(status=status):
                prepared = response(status=status)
                session = session_for(prepared)
                files, notices = await collect_attachments(session, [attachment()], replace(self.policy, max_attempts=3))
                self.assertEqual(files, [])
                self.assertEqual(notices, [f"test.txt: unavailable (HTTP {status})."])
                self.assertEqual(session.get.call_count, 1)
                prepared[0].content.read.assert_not_called()
                self.assert_no_files()

    async def test_metadata_size_rejected_before_download(self):
        session = session_for()
        files, notices = await collect_attachments(session, [attachment(size=6)], replace(self.policy, max_file_bytes=5))
        self.assertEqual(files, [])
        self.assertIn("declared size", notices[0])
        session.get.assert_not_called()
        self.assert_no_files()

    async def test_content_length_rejected_before_creating_file(self):
        prepared = response(headers={"Content-Length": "6", "Content-Type": "text/plain"})
        files, notices = await collect_attachments(session_for(prepared), [attachment()], replace(self.policy, max_file_bytes=5))
        self.assertEqual(files, [])
        self.assertIn("response size", notices[0])
        prepared[0].content.read.assert_not_called()
        self.assert_no_files()

    async def test_lying_size_cannot_exceed_actual_file_limit(self):
        prepared = response(body=b"x" * 100, headers={"Content-Length": "1", "Content-Type": "text/plain"})
        files, notices = await collect_attachments(session_for(prepared), [attachment(size=1)], replace(self.policy, max_file_bytes=5))
        self.assertEqual(files, [])
        self.assertIn("actual size", notices[0])
        self.assertEqual(prepared[0].content.read.await_args.args, (6,))
        self.assert_no_files()

    async def test_missing_size_is_still_bounded(self):
        prepared = response(body=b"x" * 100)
        files, notices = await collect_attachments(session_for(prepared), [attachment(size=None)], replace(self.policy, max_file_bytes=5))
        self.assertEqual(files, [])
        self.assertIn("actual size", notices[0])
        self.assert_no_files()

    async def test_exact_byte_limit_is_accepted(self):
        session = session_for(response(body=b"12345"))
        files, notices = await collect_attachments(session, [attachment(size=5)], replace(self.policy, max_file_bytes=5))
        self.assertEqual(notices, [])
        self.assertEqual(files[0].path.read_bytes(), b"12345")
        cleanup_attachments(files)

    async def test_total_limit_bounds_sum_and_removes_partial(self):
        first = response(body=b"1234")
        second = response(body=b"5678")
        session = session_for(first, second)
        policy = replace(self.policy, max_file_bytes=5, max_total_bytes=6)
        files, notices = await collect_attachments(session, [attachment(size=1), attachment(size=1), attachment(size=1)], policy)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].size, 4)
        self.assertEqual(session.get.call_count, 2)
        self.assertIn("actual size", notices[0])
        self.assertIn("budget exhausted", notices[1])
        self.assertEqual(len(list(Path(self.directory.name).iterdir())), 1)
        cleanup_attachments(files)
        self.assert_no_files()

    async def test_failed_file_bytes_still_consume_total_budget(self):
        first = response(body=b"1234567")
        second = response(body=b"abc")
        session = session_for(first, second)
        policy = replace(self.policy, max_file_bytes=5, max_total_bytes=8)
        files, notices = await collect_attachments(session, [attachment(size=1), attachment(size=1)], policy)
        self.assertEqual(files, [])
        self.assertEqual(len(notices), 2)
        self.assertEqual(second[0].content.read.await_args.args, (3,))
        self.assert_no_files()

    async def test_file_count_limit_caps_attempts_even_failed_downloads(self):
        session = session_for(response(status=404), response())
        files, notices = await collect_attachments(session, [attachment()] * 10, replace(self.policy, max_files=2))
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(len(files), 1)
        self.assertIn("file-count", notices[-1])
        cleanup_attachments(files)

    async def test_zero_limits_disable_archiving(self):
        for overrides in ({"max_files": 0}, {"max_total_bytes": 0}, {"max_file_bytes": 0}):
            with self.subTest(overrides=overrides):
                session = session_for()
                files, notices = await collect_attachments(session, [attachment()], replace(self.policy, **overrides))
                session.get.assert_not_called()
                self.assertEqual(files, [])
                self.assertEqual(len(notices), 1)

    async def test_content_types_check_metadata_and_response(self):
        policy = replace(self.policy, allowed_content_types=frozenset({"image/*"}))
        session = session_for()
        files, notices = await collect_attachments(session, [attachment(content_type="application/pdf")], policy)
        session.get.assert_not_called()
        self.assertEqual(files, [])
        self.assertIn("disallowed", notices[0])
        prepared = response(headers={"Content-Type": "text/html"})
        files, notices = await collect_attachments(session_for(prepared), [attachment(content_type="image/png")], policy)
        self.assertEqual(files, [])
        self.assertIn("response content type", notices[0])
        prepared[0].content.read.assert_not_called()
        self.assert_no_files()

    async def test_content_type_normalized_and_wildcards_supported(self):
        policy = replace(self.policy, allowed_content_types=frozenset({"IMAGE/*"}))
        prepared = response(headers={"Content-Type": "IMAGE/PNG; extra=value"})
        files, notices = await collect_attachments(session_for(prepared), [attachment(content_type="image/png")], policy)
        self.assertEqual(notices, [])
        self.assertEqual(files[0].content_type, "image/png")
        cleanup_attachments(files)

    async def test_missing_type_falls_back_to_metadata_or_octet_stream(self):
        for supplied, expected in (("text/plain", "text/plain"), (None, "application/octet-stream")):
            with self.subTest(supplied=supplied):
                files, notices = await collect_attachments(session_for(response(headers={})), [attachment(content_type=supplied)], self.policy)
                self.assertEqual(notices, [])
                self.assertEqual(files[0].content_type, expected)
                cleanup_attachments(files)

    async def test_compressed_response_is_rejected_without_decompression(self):
        prepared = response(headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"})
        files, notices = await collect_attachments(session_for(prepared), [attachment()], self.policy)
        self.assertEqual(files, [])
        self.assertIn("encoded response", notices[0])
        prepared[0].content.read.assert_not_called()
        self.assert_no_files()

    async def test_network_failure_cleans_partial_file_and_does_not_leak_url(self):
        prepared = response(body=b"abc", failure=aiohttp.ClientError("signed secret " + attachment().url))
        files, notices = await collect_attachments(session_for(prepared), [attachment()], self.policy)
        self.assertEqual(files, [])
        self.assertIn("download failed", notices[0])
        self.assertNotIn("secret", " ".join(notices))
        self.assertNotIn("https", " ".join(notices))
        self.assert_no_files()

    async def test_connection_failure_before_response_is_safe(self):
        prepared = response()
        prepared[1].__aenter__.side_effect = aiohttp.ClientError("secret URL")
        files, notices = await collect_attachments(session_for(prepared), [attachment()], self.policy)
        self.assertEqual(files, [])
        self.assertIn("download failed", notices[0])
        self.assertNotIn("secret", notices[0])
        self.assert_no_files()

    async def test_timeout_is_reported_and_cleaned(self):
        prepared = response(body=b"abc", failure=asyncio.TimeoutError())
        files, notices = await collect_attachments(session_for(prepared), [attachment()], self.policy)
        self.assertEqual(files, [])
        self.assertIn("timed out", notices[0])
        self.assert_no_files()

    async def test_cancellation_cleans_completed_and_current_file(self):
        session = session_for(response(), response(body=b"abc", failure=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await collect_attachments(session, [attachment(), attachment()], self.policy)
        self.assert_no_files()

    async def test_context_cleanup_when_upload_raises_or_is_cancelled(self):
        for error in (RuntimeError("upload failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    async with archived_attachments(session_for(response()), [attachment()], self.policy) as (files, notices):
                        self.assertTrue(files[0].path.exists())
                        raise error
                self.assert_no_files()

    async def test_empty_attachment_is_valid(self):
        files, notices = await collect_attachments(session_for(response(body=b"")), [attachment(size=0)], self.policy)
        self.assertEqual(notices, [])
        self.assertEqual(files[0].size, 0)
        cleanup_attachments(files)

    async def test_duplicate_names_are_unique(self):
        files, notices = await collect_attachments(session_for(response(), response()), [attachment(), attachment()], self.policy)
        self.assertEqual(notices, [])
        self.assertEqual([file.filename for file in files], ["test.txt", "test_2.txt"])
        cleanup_attachments(files)

    async def test_downloads_use_small_chunks_not_full_body_reads(self):
        body = b"x" * (150 * 1024)
        prepared = response(body=body)
        files, notices = await collect_attachments(session_for(prepared), [attachment(size=len(body))], self.policy)
        self.assertEqual(notices, [])
        self.assertEqual(files[0].path.read_bytes(), body)
        self.assertGreater(prepared[0].content.read.await_count, 2)
        self.assertTrue(all(0 < call.args[0] <= 64 * 1024 for call in prepared[0].content.read.await_args_list))
        cleanup_attachments(files)

    async def test_unexpected_failure_also_cleans_completed_files(self):
        prepared = response()
        prepared[0].content.read.side_effect = RuntimeError("programming error")
        session = session_for(response(), prepared)
        with self.assertRaises(RuntimeError):
            await collect_attachments(session, [attachment(), attachment()], self.policy)
        self.assert_no_files()

    async def test_transient_status_retries_with_backoff_then_succeeds(self):
        session = session_for(response(status=500), response(status=503), response())
        with patch("archive.asyncio.sleep", new_callable=AsyncMock) as sleep:
            files, notices = await collect_attachments(session, [attachment()], replace(self.policy, max_attempts=3))
        self.assertEqual(notices, [])
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.5, 1.0])
        cleanup_attachments(files)

    async def test_429_honors_retry_after(self):
        session = session_for(response(status=429, headers={"Retry-After": "2.5"}), response())
        with patch("archive.asyncio.sleep", new_callable=AsyncMock) as sleep:
            files, notices = await collect_attachments(session, [attachment()], replace(self.policy, max_attempts=3))
        self.assertEqual(notices, [])
        sleep.assert_awaited_once_with(2.5)
        cleanup_attachments(files)

    async def test_retry_after_http_date(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session = session_for(response(status=429, headers={"Retry-After": format_datetime(now + timedelta(seconds=5))}), response())
        with patch("archive.asyncio.sleep", new_callable=AsyncMock) as sleep, patch("archive.datetime") as clock:
            clock.now.return_value = now
            files, notices = await collect_attachments(session, [attachment()], replace(self.policy, max_attempts=3))
        self.assertEqual(notices, [])
        sleep.assert_awaited_once_with(5.0)
        cleanup_attachments(files)

    async def test_retry_after_above_bound_stops_instead_of_retrying_too_early(self):
        session = session_for(response(status=429, headers={"Retry-After": "60"}))
        with patch("archive.asyncio.sleep", new_callable=AsyncMock) as sleep:
            files, notices = await collect_attachments(session, [attachment()], replace(self.policy, max_attempts=3))
        self.assertEqual(files, [])
        self.assertIn("server backoff exceeds retry window", notices[0])
        self.assertEqual(session.get.call_count, 1)
        sleep.assert_not_awaited()
        self.assert_no_files()

    async def test_transient_errors_stop_at_attempt_limit(self):
        session = session_for(response(status=503), response(status=503), response(status=503))
        with patch("archive.asyncio.sleep", new_callable=AsyncMock) as sleep:
            files, notices = await collect_attachments(session, [attachment()], replace(self.policy, max_attempts=3))
        self.assertEqual(files, [])
        self.assertIn("retry limit reached", notices[0])
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assert_no_files()

    async def test_retry_network_failure_counts_prior_bytes_against_total(self):
        first = response(body=b"1234", failure=aiohttp.ClientError("disconnected"))
        second = response(body=b"abcde")
        session = session_for(first, second)
        policy = replace(self.policy, max_attempts=3, max_total_bytes=6)
        with patch("archive.asyncio.sleep", new_callable=AsyncMock):
            files, notices = await collect_attachments(session, [attachment(size=1)], policy)
        self.assertEqual(files, [])
        self.assertIn("actual size", notices[0])
        self.assertEqual(second[0].content.read.await_args.args, (3,))
        self.assertEqual(session.get.call_count, 2)
        self.assert_no_files()

    async def test_retry_network_failure_can_succeed_with_remaining_budget(self):
        session = session_for(response(body=b"abc", failure=aiohttp.ClientError("disconnected")), response())
        with patch("archive.asyncio.sleep", new_callable=AsyncMock):
            files, notices = await collect_attachments(session, [attachment()], replace(self.policy, max_attempts=3))
        self.assertEqual(notices, [])
        self.assertEqual(files[0].path.read_bytes(), b"abc")
        self.assertEqual(len(list(Path(self.directory.name).iterdir())), 1)
        cleanup_attachments(files)
        self.assert_no_files()

    async def test_cancellation_during_backoff_cleans_completed_files(self):
        session = session_for(response(), response(status=429))
        with patch("archive.asyncio.sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await collect_attachments(session, [attachment(), attachment()], replace(self.policy, max_attempts=3))
        self.assert_no_files()

    async def test_cancellation_during_response_exit_cleans_current_file(self):
        prepared = response()
        prepared[1].__aexit__.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await collect_attachments(session_for(prepared), [attachment()], self.policy)
        self.assert_no_files()

    async def test_oversized_body_never_retries(self):
        session = session_for(response(body=b"x" * 20))
        with patch("archive.asyncio.sleep", new_callable=AsyncMock) as sleep:
            files, notices = await collect_attachments(session, [attachment(size=1)], replace(self.policy, max_attempts=3, max_file_bytes=5))
        self.assertEqual(files, [])
        self.assertIn("actual size", notices[0])
        self.assertEqual(session.get.call_count, 1)
        sleep.assert_not_awaited()
        self.assert_no_files()


class PolicyAndFilenameTests(unittest.TestCase):
    def test_cleanup_attempts_all_files_if_one_unlink_fails(self):
        first_path, second_path = MagicMock(), MagicMock()
        first_path.unlink.side_effect = PermissionError("cannot remove")
        files = [ArchivedFile(first_path, "a", "text/plain", 0), ArchivedFile(second_path, "b", "text/plain", 0)]
        with self.assertRaises(PermissionError):
            cleanup_attachments(files)
        first_path.unlink.assert_called_once_with(missing_ok=True)
        second_path.unlink.assert_called_once_with(missing_ok=True)

    def test_defaults(self):
        policy = ArchivePolicy()
        self.assertEqual(policy.max_file_bytes, 8 * 1024 * 1024)
        self.assertEqual(policy.max_total_bytes, 16 * 1024 * 1024)
        self.assertEqual(policy.max_files, 4)
        self.assertEqual(policy.max_attempts, 3)

    def test_invalid_policies_rejected(self):
        for fields in (
            {"max_file_bytes": -1}, {"max_total_bytes": 0.5}, {"max_files": True},
            {"timeout_seconds": 0}, {"timeout_seconds": float("inf")},
            {"allowed_content_types": "image/*"}, {"allowed_content_types": {"bad"}},
            {"allowed_content_types": {"image/png\r\nx-inject: yes"}},
            {"max_attempts": 0}, {"max_attempts": 1.5}, {"retry_base_seconds": -1},
            {"max_retry_delay_seconds": float("nan")},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ArchivePolicy(**fields)

    def test_filename_sanitization(self):
        cases = {
            "../../secret.txt": "secret.txt", "C:\\temp\\note.txt": "note.txt",
            "test\r\nContent-Type:evil.txt": "test_Content-Type_evil.txt",
            "..": "attachment", "": "attachment", "CON.txt": "_CON.txt",
            "normal-photo.png": "normal-photo.png", "/leading/.hidden": "hidden",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(sanitize_filename(original), expected)
        self.assertLessEqual(len(sanitize_filename("a" * 1000)), 120)


if __name__ == "__main__":
    unittest.main()
