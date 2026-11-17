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
            get_messages=AsyncMock(),
        )

        grouped_id = asyncio.run(album._verify_album_delivery(
            client, -100123, [stale], ["archive caption"], [[]], "309",
        ))

        self.assertEqual(grouped_id, 9001)
        client.edit_message.assert_awaited_once()
        client.get_messages.assert_not_awaited()

    def test_album_edit_verifies_fresh_server_message_not_stale_edit_result(self):
        stale = SimpleNamespace(id=781, grouped_id=90021, message="stale caption")
        edit_result = SimpleNamespace(id=781, grouped_id=90021, message="stale caption")
        fetched = SimpleNamespace(id=781, grouped_id=90021, message="archive caption")
        client = SimpleNamespace(
            edit_message=AsyncMock(return_value=edit_result),
            get_messages=AsyncMock(return_value=fetched),
        )

        grouped_id = asyncio.run(album._verify_album_delivery(
            client, -100123, [stale], ["archive caption"], [[]], "310a",
        ))

        self.assertEqual(grouped_id, 90021)
        client.edit_message.assert_awaited_once()
        client.get_messages.assert_awaited_once_with(-100123, ids=781)

    def test_saved_album_recovery_moves_caption_to_first_item_and_clears_last(self):
        from telethon.tl import types as tl

        caption = "Original HTML caption"
        messages = [
            SimpleNamespace(id=901, grouped_id=9901, message=""),
            SimpleNamespace(id=902, grouped_id=9901, message=""),
            SimpleNamespace(id=903, grouped_id=9901, message=""),
            SimpleNamespace(id=904, grouped_id=9901, message="stale caption on last item"),
        ]
        by_id = {message.id: message for message in messages}

        async def edit_message(_channel, message_id, text, **_kwargs):
            by_id[message_id].message = text
            return by_id[message_id]

        async def get_messages(_channel, ids):
            if isinstance(ids, (list, tuple)):
                return [by_id[value] for value in ids]
            return by_id[ids]

        bold = tl.MessageEntityBold(offset=0, length=8)
        client = SimpleNamespace(
            edit_message=AsyncMock(side_effect=edit_message),
            get_messages=AsyncMock(side_effect=get_messages),
        )

        grouped_id = asyncio.run(album._verify_album_delivery(
            client, -100123, messages,
            [caption, "", "", ""], [[bold], [], [], []], "27425",
        ))

        self.assertEqual(grouped_id, 9901)
        self.assertEqual([message.message for message in messages], [caption, "", "", ""])
        self.assertEqual(client.edit_message.await_count, 2)
        first_edit = client.edit_message.await_args_list[0]
        last_edit = client.edit_message.await_args_list[1]
        self.assertEqual(first_edit.args[1:], (901, caption))
        self.assertEqual(
            first_edit.kwargs["formatting_entities"],
            [bold],
        )
        self.assertEqual(last_edit.args[1:], (904, ""))
        self.assertIsNone(last_edit.kwargs["formatting_entities"])

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
        client.get_messages.assert_not_awaited()

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
