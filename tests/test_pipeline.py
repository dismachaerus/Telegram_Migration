"""End-to-end runs of the migration against a fake Telegram client (no network, no credentials)."""
import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from telegram_migration import bigfiles, config, htmlexport, mediatools, pipeline, progress
from lxml import html
from telegram_migration.config import Settings

import fakes
from fakes import FakeTelegram


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        FakeTelegram.instances.clear()
        for patcher in (patch.object(pipeline, "TelegramClient", FakeTelegram),
                        patch.object(mediatools, "enable_hachoir", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def settings(self, **changes):
        values = dict(api_id=1234, api_hash="hash", bot_token="token", channel_peer=-1001234567890,
                      base_dir=self.folder.name, dry_run=False, skip_confirmation=True,
                      send_delay_seconds=0, max_upload_mb=1)
        values.update(changes)
        return Settings(**values)

    def run_once(self, **changes):
        settings = self.settings(**changes)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with config.use_settings(settings):
                asyncio.run(pipeline.run_async(settings))
        return FakeTelegram.instances[-1], output.getvalue(), settings

    def path(self, name):
        return os.path.join(self.folder.name, name)

    # ------------------------------------------------------------------------------------
    def test_dry_run_sends_nothing(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("hello")), fakes.message(2, fakes.text("again"))])
        client, output, _ = self.run_once(dry_run=True)
        self.assertEqual([c for c in client.calls if c[0] in ("send_message", "send_file")], [])
        self.assertIn("DRY RUN", output)
        self.assertFalse(os.path.exists(self.path("migration_id_map.jsonl")))

    def test_text_messages_are_sent_with_time_and_sender_and_recorded(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("hello <b>there</b>"))])
        client, _, _ = self.run_once()
        self.assertEqual(client.texts(), ["Alice\n12 March 2023 · 10:01\n\nhello there"])
        sent = client.kinds("send_message")[0][2]
        self.assertIsNone(sent["parse_mode"])
        entities = sent["formatting_entities"]
        self.assertEqual(
            [(type(entity).__name__, entity.offset, entity.length) for entity in entities[:2]],
            [("MessageEntityBold", 0, 5), ("MessageEntityItalic", 6, 21)],
        )
        self.assertEqual(
            (type(entities[2]).__name__, entities[2].offset, entities[2].length),
            ("MessageEntityBold", 35, 5),
        )
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"1": 1001})

    def test_missing_media_only_message_is_sent_as_placeholder_not_skipped(self):
        fakes.write_export(self.folder.name, [
            fakes.message(1, '<div class="media_wrap clearfix">'
                             '<a class="photo_wrap" href="photos/gone.jpg">'
                             '<img class="photo" src="photos/gone_thumb.jpg"/>'
                             '</a></div>'),
        ])
        client, output, _ = self.run_once()
        self.assertEqual(
            client.texts(),
            ["Alice\n12 March 2023 · 10:01\n\n[Media not found: photos/gone.jpg]"],
        )
        self.assertIn("sent=1", output)
        self.assertIn("skipped=0", output)
        self.assertEqual(client.kinds("send_file"), [])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"1": 1001})

    def test_empty_export_message_is_preserved_without_inventing_forward_attribution(self):
        # This matches the blank message shell in the user's Telegram Desktop
        # export: the sender and export timestamp survive, but there is no
        # forwarded body, original author, original timestamp, text, or media.
        empty_message = (
            '<div class="message default clearfix" id="message81">'
            '<div class="pull_left userpic_wrap">'
            '<div class="userpic userpic8"><div class="initials">⛯</div></div>'
            '</div>'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="01.07.2026 21:33:19 UTC+02:00">21:33</div>'
            '<div class="from_name">⛯⛯</div>'
            '</div>'
            '</div>'
        )
        fakes.write_export(self.folder.name, [empty_message])
        client, output, _ = self.run_once()

        self.assertEqual(
            client.texts(),
            [
                "⛯⛯\\n1 July 2026 · 21:33\\n\\n"
                "[Empty message in source export; no text or media were included]"
            ],
        )
        self.assertNotIn("Forwarded from", client.texts()[0])
        self.assertIn("sent=1", output)
        self.assertIn("skipped=0", output)
        self.assertIn("failed=0", output)
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"81": 1001})

    def test_service_events_are_migrated_as_dated_archival_entries(self):
        fakes.write_export(self.folder.name, [
            '<div class="message service" id="message-1">'
            '<div class="body details">12 March 2023</div></div>',
            '<div class="message service" id="message2">'
            '<div class="body details">Alice invited Bob</div></div>',
            fakes.message(3, fakes.text("hello")),
        ])
        client, _, _ = self.run_once()
        self.assertEqual(client.texts(), [
            "Telegram service event\n12 March 2023\n\nAlice invited Bob",
            "Alice\n12 March 2023 · 10:03\n\nhello",
        ])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"2": 1001, "3": 1002})

    def test_service_events_can_be_disabled(self):
        fakes.write_export(self.folder.name, [
            '<div class="message service" id="message-1">'
            '<div class="body details">12 March 2023</div></div>',
            '<div class="message service" id="message2">'
            '<div class="body details">Alice invited Bob</div></div>',
        ])
        client, _, _ = self.run_once(preserve_service_messages=False)
        self.assertEqual(client.texts(), [])

    def test_concurrent_runs_keep_settings_and_exports_isolated(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            fakes.write_export(first_dir, [fakes.message(1, fakes.text("first run"))])
            fakes.write_export(second_dir, [fakes.message(2, fakes.text("second run"))])
            first = self.settings(base_dir=first_dir)
            second = self.settings(base_dir=second_dir)

            async def scenario():
                await asyncio.gather(
                    pipeline.run_async(first),
                    pipeline.run_async(second),
                )

            with contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(scenario())

        clients = FakeTelegram.instances[-2:]
        self.assertCountEqual([client.texts() for client in clients], [
            ["Alice\n12 March 2023 · 10:01\n\nfirst run"],
            ["Alice\n12 March 2023 · 10:02\n\nsecond run"],
        ])

    def test_configured_text_limit_splits_reconstructed_messages(self):
        fakes.write_export(self.folder.name, [
            fakes.message(1, fakes.text("x" * 100)),
        ])
        client, _, _ = self.run_once(text_limit=40)
        parts = client.texts()
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 40 for part in parts))

    def test_second_run_resumes_and_does_not_repeat_messages(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("one")), fakes.message(2, fakes.text("two"))])
        self.run_once()
        client, _, _ = self.run_once()
        self.assertEqual(client.texts(), [])

    def test_reconstructed_text_forward_keeps_attribution_and_payload_in_one_message(self):
        forwarded = (
            '<div class="message default clearfix" id="message136">'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="08.11.2026 07:09:41 UTC+02:00">07:09</div>'
            '<div class="from_name">jbvjhvjhvjh</div>'
            '<div class="pull_left forwarded userpic_wrap">'
            '<div class="userpic userpic7"><div class="initials">H</div></div>'
            '</div>'
            '<div class="forwarded body">'
            '<div class="from_name">HLI8IOS '
            '<span class="date details" '
            'title="08.11.2026 07:09:09 UTC+02:00">'
            '08.11.2026 07:09:09</span></div>'
            '<div class="text">hello</div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        client, _, _ = self.run_once()

        calls = client.kinds("send_message")
        self.assertEqual(len(calls), 1)
        sent = calls[0]
        self.assertEqual(
            sent[1],
            "jbvjhvjhvjh\n8 November 2026 · 07:09\n\n"
            "Forwarded from HLI8IOS\n8 November 2026 · 07:09:09\n\nhello",
        )
        self.assertIsNone(sent[2]["reply_to"])

        entities = sent[2]["formatting_entities"]
        quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
        quote_text = sent[1][quote.offset:quote.offset + quote.length]
        self.assertEqual(quote_text, "Forwarded from HLI8IOS\n8 November 2026 · 07:09:09")
        self.assertLess(quote.offset + quote.length, len(sent[1]))
        source_bold = next(e for e in entities
                           if type(e).__name__ == "MessageEntityBold"
                           and sent[1][e.offset:e.offset + e.length] == "HLI8IOS")
        self.assertEqual(sent[1][source_bold.offset:source_bold.offset + source_bold.length],
                         "HLI8IOS")
        source_date_italic = next(e for e in entities
                                  if type(e).__name__ == "MessageEntityItalic"
                                  and "07:09:09" in sent[1][e.offset:e.offset + e.length])
        self.assertIn("8 November 2026 · 07:09:09",
                      sent[1][source_date_italic.offset:
                              source_date_italic.offset + source_date_italic.length])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"136": 1001})

    def test_reconstructed_forward_keeps_body_formatting_below_attribution_bar(self):
        forwarded = (
            '<div class="message default clearfix" id="message138">'
            '<div class="body">'
            '<div class="pull_right date details" title="08.11.2026 07:09:41 UTC+02:00">07:09</div>'
            '<div class="from_name">jbvjhvjhvjh</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name">HLI8IOS</div>'
            '<div class="text"><strong>hello</strong> <em>world</em></div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        client, _, _ = self.run_once()
        calls = client.kinds("send_message")
        self.assertEqual(len(calls), 1)
        sent = calls[0]
        self.assertIn("Forwarded from HLI8IOS", sent[1])
        self.assertTrue(sent[1].endswith("hello world"))
        entities = sent[2]["formatting_entities"]
        names = [type(entity).__name__ for entity in entities]
        self.assertIn("MessageEntityBold", names)
        self.assertIn("MessageEntityItalic", names)
        quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
        quote_text = sent[1][quote.offset:quote.offset + quote.length]
        self.assertEqual(quote_text, "Forwarded from HLI8IOS")
        self.assertNotIn("hello", quote_text)
        bold = next(e for e in entities if type(e).__name__ == "MessageEntityBold"
                    and sent[1][e.offset:e.offset + e.length] == "hello")
        italic = next(e for e in entities if type(e).__name__ == "MessageEntityItalic"
                      and sent[1][e.offset:e.offset + e.length] == "world")
        self.assertEqual(sent[1][bold.offset:bold.offset + bold.length], "hello")
        self.assertEqual(sent[1][italic.offset:italic.offset + italic.length], "world")

    def test_reconstructed_forward_media_keeps_attribution_in_same_media_caption(self):
        media_path = "video_files/a6579a95-f3fd-497d-9b88-46592c1a878e_wm.mp4"
        fakes.write_file(self.folder.name, media_path)
        forwarded = (
            '<div class="message default clearfix" id="message151">'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
            '<div class="from_name">jbvjhvjhvjh</div>'
            '<div class="pull_left forwarded userpic_wrap">'
            '<div class="userpic userpic7"><div class="initials">H</div></div>'
            '</div>'
            '<div class="forwarded body">'
            '<div class="from_name">HLI8IOS '
            '<span class="date details" title="05.11.2026 19:35:11 UTC+02:00">'
            '05.11.2026 19:35:11</span></div>'
            '<div class="media_wrap clearfix">'
            f'<a class="animated_wrap clearfix pull_left" href="{media_path}">'
            '<div class="video_play_bg"><div class="gif_play">GIF</div></div>'
            '</a></div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        client, _, _ = self.run_once()

        self.assertEqual(client.kinds("send_message"), [])
        media_calls = client.kinds("send_file")
        self.assertEqual(len(media_calls), 1)
        sent_files, kwargs = media_calls[0][1], media_calls[0][2]
        self.assertEqual(sent_files, os.path.join(self.folder.name, media_path))
        self.assertIn("Forwarded from HLI8IOS", kwargs["caption"])
        self.assertIn("5 November 2026 · 19:35:11", kwargs["caption"])
        entities = kwargs["formatting_entities"]
        quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
        quote_text = kwargs["caption"][quote.offset:quote.offset + quote.length]
        self.assertEqual(quote_text, "Forwarded from HLI8IOS\n5 November 2026 · 19:35:11")
        italic = next(e for e in entities if type(e).__name__ == "MessageEntityItalic"
                      and "19:35:11" in kwargs["caption"][e.offset:e.offset + e.length])
        self.assertIn("5 November 2026 · 19:35:11",
                      kwargs["caption"][italic.offset:italic.offset + italic.length])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"151": 1001})

    def test_reconstructed_forward_text_and_media_share_one_caption(self):
        media_path = "video_files/forward-with-caption.mp4"
        fakes.write_file(self.folder.name, media_path)
        forwarded = (
            '<div class="message default clearfix" id="message153">'
            '<div class="body">'
            '<div class="pull_right date details" title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
            '<div class="from_name">jbvjhvjhvjh</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name">HLI8IOS '
            '<span class="date details" title="05.11.2026 19:35:11 UTC+02:00">'
            '05.11.2026 19:35:11</span></div>'
            f'<div class="text"><em>original caption</em></div>'
            f'<div class="media_wrap clearfix"><a class="video_file_wrap" href="{media_path}"></a></div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        client, _, _ = self.run_once()
        self.assertEqual(client.kinds("send_message"), [])
        calls = client.kinds("send_file")
        self.assertEqual(len(calls), 1)
        caption = calls[0][2]["caption"]
        self.assertTrue(caption.endswith("original caption"))
        self.assertIn("Forwarded from HLI8IOS", caption)
        entities = calls[0][2]["formatting_entities"]
        names = [type(entity).__name__ for entity in entities]
        self.assertIn("MessageEntityBlockquote", names)
        self.assertIn("MessageEntityItalic", names)
        quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
        quote_text = caption[quote.offset:quote.offset + quote.length]
        self.assertEqual(quote_text, "Forwarded from HLI8IOS\n5 November 2026 · 19:35:11")
        italic_caption = next(e for e in entities if type(e).__name__ == "MessageEntityItalic"
                              and caption[e.offset:e.offset + e.length] == "original caption")
        self.assertEqual(caption[italic_caption.offset:italic_caption.offset + italic_caption.length],
                         "original caption")

    def test_native_forward_with_media_bypasses_reconstructed_quote_prefix(self):
        media_path = "video_files/native-forward.mp4"
        fakes.write_file(self.folder.name, media_path)
        forwarded = (
            '<div class="message default clearfix" id="message152">'
            '<div class="body">'
            '<div class="pull_right date details" title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
            '<div class="from_name">Alice</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name"><a href="https://t.me/NewsChannel/4321">News Channel</a></div>'
            '<div class="media_wrap clearfix">'
            f'<a class="video_file_wrap" href="{media_path}"></a>'
            '</div></div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        native_calls = []

        async def native_forward(_client, _channel, peer_hint, source_msg_id):
            native_calls.append((peer_hint, source_msg_id))
            return 777

        with patch.object(pipeline.sending, "_try_native_forward", new=native_forward):
            client, output, _ = self.run_once()

        self.assertEqual(native_calls, [("NewsChannel", 4321)])
        # The only text call is the existing sender/timestamp annotation that
        # is replied to the genuine native forward; no reconstructed quote or
        # duplicate media upload is added.
        text_calls = client.kinds("send_message")
        self.assertEqual(len(text_calls), 1)
        self.assertNotIn("Forwarded from", text_calls[0][1])
        self.assertNotIn(
            "MessageEntityBlockquote",
            [type(entity).__name__ for entity in text_calls[0][2]["formatting_entities"]],
        )
        self.assertEqual(text_calls[0][2]["reply_to"], 777)
        self.assertEqual(client.kinds("send_file"), [])
        self.assertIn("native_forwards=1", output)

    def test_real_telegram_forward_body_link_is_used_for_native_source(self):
        forwarded = html.fromstring(
            '<div class="message default clearfix" id="message137">'
            '<div class="body">'
            '<div class="from_name">jbvjhvjhvjh</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name"><a href="https://t.me/NewsChannel/4321">News Channel</a>'
            '<span class="date details" title="08.11.2026 07:09:09 UTC+02:00">'
            '08.11.2026 07:09:09</span></div>'
            '<div class="text">forwarded text</div>'
            '</div></div></div>'
        )
        with config.use_settings(self.settings()):
            data = htmlexport.extract_message_data(forwarded)
        self.assertEqual(data["forward_source"], ("NewsChannel", 4321))
        self.assertEqual(data["forward_name"], "News Channel")
        self.assertTrue(data["is_forwarded"])
        self.assertEqual(data["forward_header"], "Forwarded from News Channel\n")
        self.assertEqual(data["forward_timestamp"].strftime("%d.%m.%Y %H:%M:%S"),
                         "08.11.2026 07:09:09")
        self.assertTrue(data["forward_timestamp_has_time"])
        self.assertTrue(data["forward_timestamp_has_seconds"])

    def test_native_forward_path_remains_native_and_does_not_use_reconstruction_quote(self):
        forwarded = (
            '<div class="message default clearfix" id="message139">'
            '<div class="body">'
            '<div class="pull_right date details" title="08.11.2026 07:09:41 UTC+02:00">07:09</div>'
            '<div class="from_name">Alice</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name"><a href="https://t.me/NewsChannel/4321">News Channel</a></div>'
            '<div class="text">forwarded text</div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        native_calls = []

        async def native_forward(_client, _channel, peer_hint, source_msg_id):
            native_calls.append((peer_hint, source_msg_id))
            return 777

        with patch.object(pipeline.sending, "_try_native_forward", new=native_forward):
            client, output, _ = self.run_once()

        self.assertEqual(native_calls, [("NewsChannel", 4321)])
        # Native forwarding sends the source message itself; the only text
        # message is the normal archive header annotated as a reply.
        self.assertEqual(client.texts(), ["Alice\n8 November 2026 · 07:09"])
        send_call = client.kinds("send_message")[0]
        self.assertEqual(send_call[2]["reply_to"], 777)
        self.assertNotIn("Forwarded from", send_call[1])
        self.assertNotIn(
            "MessageEntityBlockquote",
            [type(entity).__name__ for entity in send_call[2]["formatting_entities"]],
        )
        self.assertIn("native_forwards=1", output)

    def test_reply_to_a_later_message_is_deferred_then_linked(self):
        fakes.write_export(self.folder.name, [
            fakes.message(1, fakes.text("first")),
            fakes.message(2, fakes.reply_to(3) + fakes.text("reply to the next one")),
            fakes.message(3, fakes.text("the target")),
        ])
        client, _, _ = self.run_once()
        order = client.texts()
        self.assertTrue(order[1].endswith("the target"), order)
        reply_call = [c for c in client.kinds("send_message") if "reply to the next one" in c[1]][0]
        target_id = 1002
        self.assertEqual(reply_call[2]["reply_to"], target_id)

    def test_reply_to_a_message_outside_the_export_gets_a_note(self):
        fakes.write_export(self.folder.name, [fakes.message(5, fakes.reply_to(999) + fakes.text("orphan reply"))])
        client, _, _ = self.run_once()
        self.assertIn("Original reply target #999 is unavailable", client.texts()[0])

    def test_oversize_file_stops_cleanly_and_the_next_run_skips_it(self):
        fakes.write_file(self.folder.name, "video_files/big.mp4", size=2 * 1024 * 1024)
        fakes.write_export(self.folder.name, [
            fakes.message(1, fakes.text("before")),
            fakes.message(2, fakes.text("has a big video") + fakes.video("video_files/big.mp4")),
            fakes.message(3, fakes.text("after")),
            fakes.message(4, fakes.reply_to(2) + fakes.text("reply to the big one")),
        ])
        client, output, _ = self.run_once()
        self.assertEqual(len(client.texts()), 1)
        self.assertEqual(client.kinds("send_file"), [])
        self.assertTrue(client.disconnected)
        self.assertIn("STOPPED: message id=2", output)
        self.assertIn(os.path.abspath(self.path("video_files/big.mp4")), output)
        with open(self.path("failed_messages.txt"), encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith("2\tFILE_TOO_LARGE"))
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"1": 1001})
            self.assertEqual(bigfiles.load_manual_uploads()[0], {"2"})
        self.assertFalse(os.path.exists(self.path("migration_reply_audit.jsonl")))
        client, output, _ = self.run_once()
        self.assertEqual(client.kinds("send_file"), [])
        self.assertTrue(any("after" in t for t in client.texts()))
        self.assertTrue(any("reply to the big one" in t and "Original reply target #2 is unavailable" in t
                            for t in client.texts()))
        self.assertIn("SKIPPED by the script (ids: 2)", output)

    def test_manual_upload_with_destination_id_links_later_replies(self):
        fakes.write_file(self.folder.name, "video_files/big.mp4", size=2 * 1024 * 1024)
        fakes.write_export(self.folder.name, [
            fakes.message(2, fakes.text("big") + fakes.video("video_files/big.mp4")),
            fakes.message(4, fakes.reply_to(2) + fakes.text("reply to the big one")),
        ])
        with open(self.path("migration_manual_uploads.txt"), "w", encoding="utf-8") as handle:
            handle.write("2 5000   # uploaded by hand\n")
        client, _, _ = self.run_once()
        call = [c for c in client.kinds("send_message") if "reply to the big one" in c[1]][0]
        self.assertEqual(call[2]["reply_to"], 5000)

    def test_rejected_formatting_is_retried_as_plain_text(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("some <b>bold</b> words"))])
        original = FakeTelegram.__init__

        def init(this, *a, **k):
            original(this, *a, **k)
            this.reject_formatting = ValueError("Some of provided entities have invalid bounds")

        with patch.object(FakeTelegram, "__init__", init):
            client, output, _ = self.run_once()
        self.assertEqual(len(client.texts()), 1)
        self.assertIsNone(client.kinds("send_message")[0][2]["formatting_entities"])
        self.assertIn("sending it as plain text", output)
        with open(self.path("migration_formatting_dropped.jsonl"), encoding="utf-8") as handle:
            self.assertEqual(json.loads(handle.readline())["src"], "1")

    def test_a_media_message_that_was_already_uploaded_is_not_uploaded_again(self):
        fakes.write_file(self.folder.name, "photos/p.jpg")
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("caption") + fakes.photo("photos/p.jpg"))])
        with open(self.path("migration_partial_sends.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"src": "1", "state": {"media_id": 777}}) + "\n")
        client, _, _ = self.run_once()
        self.assertEqual(client.kinds("send_file"), [])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"1": 777})

    def test_batch_limit_stops_the_run(self):
        fakes.write_export(self.folder.name, [fakes.message(i, fakes.text(f"m{i}")) for i in range(1, 6)])
        client, output, _ = self.run_once(batch_limit=2)
        self.assertEqual(len(client.texts()), 2)
        self.assertIn("Batch limit reached", output)

    def test_missing_credentials_raise_a_config_error(self):
        with self.assertRaises(config.ConfigError):
            asyncio.run(pipeline.run_async(Settings(base_dir=self.folder.name)))

    def test_run_async_returns_true_on_success(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("hello"))])
        settings = self.settings()
        with contextlib.redirect_stdout(io.StringIO()):
            with config.use_settings(settings):
                result = asyncio.run(pipeline.run_async(settings))
        self.assertIs(result, True)

    def test_run_async_returns_false_when_export_folder_is_empty(self):
        settings = self.settings()
        with contextlib.redirect_stdout(io.StringIO()):
            with config.use_settings(settings):
                result = asyncio.run(pipeline.run_async(settings))
        self.assertIs(result, False)

    def test_run_async_returns_false_when_channel_cannot_be_resolved(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("hello"))])
        settings = self.settings()

        async def broken_get_entity(self, peer):
            raise ValueError("channel not found")

        with patch.object(FakeTelegram, "get_entity", broken_get_entity):
            with contextlib.redirect_stdout(io.StringIO()):
                with config.use_settings(settings):
                    result = asyncio.run(pipeline.run_async(settings))
        self.assertIs(result, False)


if __name__ == "__main__":
    unittest.main()
