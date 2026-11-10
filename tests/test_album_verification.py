"""Regression tests for inferred-album verification edge cases."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.errors import MessageNotModifiedError

from telegram_migration import album


class AlbumVerificationTests(unittest.TestCase):
    def test_message_not_modified_is_accepted_only_after_exact_refetch(self):
        expected_entities = []
        stale = SimpleNamespace(id=77, grouped_id=9001, message="stale caption")
        confirmed = SimpleNamespace(
            id=77, grouped_id=9001, message="archive caption", entities=[]
        )
        client = SimpleNamespace(
            edit_message=AsyncMock(side_effect=MessageNotModifiedError(None)),
            get_messages=AsyncMock(return_value=confirmed),
        )

        grouped_id = asyncio.run(album._verify_album_delivery(
            client, -100123, [stale], ["archive caption"],
            [expected_entities], "309",
        ))

        self.assertEqual(grouped_id, 9001)
        client.edit_message.assert_awaited_once()
        client.get_messages.assert_awaited_once_with(-100123, ids=77)

    def test_message_not_modified_does_not_hide_a_real_caption_mismatch(self):
        stale = SimpleNamespace(id=78, grouped_id=9002, message="stale caption")
        confirmed = SimpleNamespace(
            id=78, grouped_id=9002, message="still wrong", entities=[]
        )
        client = SimpleNamespace(
            edit_message=AsyncMock(side_effect=MessageNotModifiedError(None)),
            get_messages=AsyncMock(return_value=confirmed),
        )

        with self.assertRaisesRegex(RuntimeError, "text or formatting entities"):
            asyncio.run(album._verify_album_delivery(
                client, -100123, [stale], ["archive caption"], [[]], "310",
            ))

    def test_message_not_modified_checks_formatting_entities_too(self):
        from telethon.tl import types as tl

        expected_entities = [tl.MessageEntityBold(offset=0, length=7)]
        stale = SimpleNamespace(id=79, grouped_id=9003, message="caption")
        confirmed = SimpleNamespace(
            id=79, grouped_id=9003, message="caption", entities=[]
        )
        client = SimpleNamespace(
            edit_message=AsyncMock(side_effect=MessageNotModifiedError(None)),
            get_messages=AsyncMock(return_value=confirmed),
        )

        with self.assertRaisesRegex(RuntimeError, "text or formatting entities"):
            asyncio.run(album._verify_album_delivery(
                client, -100123, [stale], ["caption"], [expected_entities], "311",
            ))


if __name__ == "__main__":
    unittest.main()
