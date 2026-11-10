"""Regression tests for inferred-album verification edge cases."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.errors import MessageNotModifiedError

from telegram_migration import album


class AlbumVerificationTests(unittest.TestCase):
    def test_message_not_modified_confirms_server_already_has_requested_caption(self):
        stale = SimpleNamespace(id=77, grouped_id=9001, message="stale local caption")
        client = SimpleNamespace(
            edit_message=AsyncMock(side_effect=MessageNotModifiedError(None)),
            get_messages=AsyncMock(),
        )

        grouped_id = asyncio.run(album._verify_album_delivery(
            client, -100123, [stale], ["archive caption"], [[]], "309",
        ))

        self.assertEqual(grouped_id, 9001)
        client.edit_message.assert_awaited_once()
        client.get_messages.assert_not_awaited()

    def test_successful_edit_still_rejects_a_real_caption_mismatch(self):
        stale = SimpleNamespace(id=78, grouped_id=9002, message="stale caption")
        edited = SimpleNamespace(id=78, grouped_id=9002, message="still wrong")
        client = SimpleNamespace(
            edit_message=AsyncMock(return_value=edited),
            get_messages=AsyncMock(return_value=edited),
        )

        with self.assertRaisesRegex(RuntimeError, "expected text was not retained"):
            asyncio.run(album._verify_album_delivery(
                client, -100123, [stale], ["archive caption"], [[]], "310",
            ))

    def test_message_not_modified_is_accepted_for_caption_with_entities(self):
        from telethon.tl import types as tl

        expected_entities = [tl.MessageEntityBold(offset=0, length=7)]
        stale = SimpleNamespace(id=79, grouped_id=9003, message="old caption")
        client = SimpleNamespace(
            edit_message=AsyncMock(side_effect=MessageNotModifiedError(None)),
            get_messages=AsyncMock(),
        )

        grouped_id = asyncio.run(album._verify_album_delivery(
            client, -100123, [stale], ["caption"], [expected_entities], "311",
        ))

        self.assertEqual(grouped_id, 9003)
        client.edit_message.assert_awaited_once()
        client.get_messages.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
