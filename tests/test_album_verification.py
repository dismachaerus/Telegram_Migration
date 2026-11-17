"""Regression tests for inferred-album verification edge cases and RPC no-ops."""
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
            get_messages=AsyncMock(return_value=stale),
        )

        grouped_id = asyncio.run(album._verify_album_delivery(
            client, -100123, [stale], ["archive caption"], [[]], "309",
        ))

        self.assertEqual(grouped_id, 9001)
        client.edit_message.assert_awaited_once()
        client.get_messages.assert_awaited_once_with(-100123, ids=77)

    def test_stale_upload_result_does_not_trigger_unnecessary_caption_edit(self):
        stale_upload_result = SimpleNamespace(
            id=781, grouped_id=90021, message="stale caption"
        )
        persisted = SimpleNamespace(
            id=781, grouped_id=90021, message="archive caption"
        )
        client = SimpleNamespace(
            edit_message=AsyncMock(),
            get_messages=AsyncMock(return_value=persisted),
        )

        grouped_id = asyncio.run(album._verify_album_delivery(
            client, -100123, [stale_upload_result], ["archive caption"], [[]], "310a",
        ))

        self.assertEqual(grouped_id, 90021)
        client.edit_message.assert_not_awaited()
        client.get_messages.assert_awaited_once_with(-100123, ids=781)

    def test_album_edit_verifies_fresh_server_message_not_stale_edit_result(self):
        stale = SimpleNamespace(id=781, grouped_id=90021, message="stale caption")
        edit_result = SimpleNamespace(id=781, grouped_id=90021, message="stale caption")
        fetched_before_edit = SimpleNamespace(
            id=781, grouped_id=90021, message="still stale"
        )
        fetched_after_edit = SimpleNamespace(
            id=781, grouped_id=90021, message="archive caption"
        )
        client = SimpleNamespace(
            edit_message=AsyncMock(return_value=edit_result),
            get_messages=AsyncMock(side_effect=[fetched_before_edit, fetched_after_edit]),
        )

        grouped_id = asyncio.run(album._verify_album_delivery(
            client, -100123, [stale], ["archive caption"], [[]], "310a",
        ))

        self.assertEqual(grouped_id, 90021)
        client.edit_message.assert_awaited_once()
        self.assertEqual(client.get_messages.await_count, 2)
        client.get_messages.assert_any_await(-100123, ids=781)

    def test_successful_edit_still_rejects_a_real_caption_mismatch(self):
        stale = SimpleNamespace(id=78, grouped_id=9002, message="stale caption")
        edited = SimpleNamespace(id=78, grouped_id=9002, message="still wrong")
        client = SimpleNamespace(
            edit_message=AsyncMock(return_value=edited),
            get_messages=AsyncMock(return_value=edited),
        )

        with self.assertRaisesRegex(RuntimeError, "fetched text did not match"):
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
        self.assertEqual(client.get_messages.await_count, 1)

    def test_album_edit_sorts_outer_blockquote_before_nested_entities(self):
        from telethon.tl import types as tl

        caption = "Forwarded from Source\\n20 August 2024 · 16:32:07"
        bold = tl.MessageEntityBold(offset=16, length=6)
        italic = tl.MessageEntityItalic(offset=23, length=25)
        quote = tl.MessageEntityBlockquote(offset=0, length=len(caption))
        stale = SimpleNamespace(id=80, grouped_id=9004, message="old caption")
        edited = SimpleNamespace(id=80, grouped_id=9004, message=caption)
        client = SimpleNamespace(edit_message=AsyncMock(return_value=edited))

        grouped_id = asyncio.run(album._verify_album_delivery(
            client, -100123, [stale], [caption], [[bold, italic, quote]], "312",
        ))

        self.assertEqual(grouped_id, 9004)
        passed_entities = client.edit_message.await_args.kwargs["formatting_entities"]
        self.assertEqual(
            [type(entity).__name__ for entity in passed_entities],
            ["MessageEntityBlockquote", "MessageEntityBold", "MessageEntityItalic"],
        )

    def test_album_edit_trims_trailing_whitespace_from_entity_spans(self):
        from telethon.tl import types as tl

        caption = "bold body "
        bold = tl.MessageEntityBold(offset=0, length=5)
        quote = tl.MessageEntityBlockquote(offset=0, length=len(caption))
        stale = SimpleNamespace(id=82, grouped_id=9006, message="old caption")
        edited = SimpleNamespace(id=82, grouped_id=9006, message=caption)
        client = SimpleNamespace(edit_message=AsyncMock(return_value=edited))

        grouped_id = asyncio.run(album._verify_album_delivery(
            client, -100123, [stale], [caption], [[bold, quote]], "314",
        ))

        self.assertEqual(grouped_id, 9006)
        passed = client.edit_message.await_args.kwargs["formatting_entities"]
        self.assertEqual(
            [(type(entity).__name__, entity.offset, entity.length) for entity in passed],
            [("MessageEntityBlockquote", 0, 9), ("MessageEntityBold", 0, 4)],
        )
        self.assertEqual((bold.length, quote.length), (5, len(caption)))

    def test_album_edit_rejects_out_of_bounds_entity_before_rpc(self):
        from telethon.tl import types as tl

        stale = SimpleNamespace(id=81, grouped_id=9005, message="old caption")
        client = SimpleNamespace(edit_message=AsyncMock())
        invalid = tl.MessageEntityBold(offset=0, length=99)

        with self.assertRaisesRegex(ValueError, "text_utf16_len=7"):
            asyncio.run(album._verify_album_delivery(
                client, -100123, [stale], ["caption"], [[invalid]], "313",
            ))

        client.edit_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
