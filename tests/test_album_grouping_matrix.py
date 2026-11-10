"""Media-agnostic source album grouping regression matrix.

These tests use synthetic records only. They verify source grouping separately
from Telegram's narrower native sendMediaGroup compatibility rules.
"""
import unittest
from datetime import datetime, timedelta

from telegram_migration import album, exportmedia


class AlbumGroupingMatrixTests(unittest.TestCase):
    def _record(self, index, kind, *, first=False, base=None):
        base = base or datetime(2026, 1, 1, 12, 0, 0)
        return {
            "msg_id": str(1000 + index),
            "media": [(f"synthetic/{index}.{kind}", kind)],
            "media_note": "",
            "sender": "Archive owner",
            "_album_sender": "Original forwarded author",
            "is_forwarded": True,
            "forward_name": "Original forwarded author",
            "forward_timestamp": base,
            "timestamp": base + timedelta(seconds=index),
            "joined": not first,
            "reply_src_id": "900",
            "body_text": "synthetic caption" if first else "",
        }

    def test_source_grouping_is_independent_of_media_kind_and_group_size(self):
        media_runs = (
            ("photo",),
            ("video",),
            ("document",),
            ("audio",),
            ("gif",),
            ("voice",),
            ("round_video",),
            ("sticker_static",),
            ("photo", "video", "gif", "voice", "round_video", "sticker_static",
             "document", "audio"),
        )
        for count in (2, 3, 8, 10):
            for kinds in media_runs:
                with self.subTest(count=count, kinds=kinds):
                    records = [
                        self._record(i, kinds[i % len(kinds)], first=(i == 0))
                        for i in range(count)
                    ]
                    self.assertTrue(all(
                        album._album_candidate(record, record["sender"])
                        for record in records
                    ))
                    for left, right in zip(records, records[1:]):
                        self.assertTrue(album._album_records_compatible(left, right))
                    self.assertEqual(records[0]["body_text"], "synthetic caption")
                    self.assertTrue(all(not row["body_text"] for row in records[1:]))

    def test_repeated_reply_context_is_shared_group_context(self):
        first = self._record(0, "photo", first=True)
        continuation = self._record(1, "voice")
        self.assertTrue(album._album_records_compatible(first, continuation))

        continuation["reply_src_id"] = "901"
        self.assertFalse(album._album_records_compatible(first, continuation))

    def test_native_album_compatibility_is_a_separate_destination_decision(self):
        self.assertTrue(exportmedia.album_compatible([
            ("a.jpg", "photo"), ("b.mp4", "video"),
        ]))
        self.assertTrue(exportmedia.album_compatible([
            ("a.pdf", "document"), ("b.pdf", "document"),
        ]))
        self.assertTrue(exportmedia.album_compatible([
            ("a.mp3", "audio"), ("b.ogg", "audio"),
        ]))
        for media in (
            [("a.gif", "gif"), ("b.jpg", "photo")],
            [("a.ogg", "voice"), ("b.jpg", "photo")],
            [("a.webm", "round_video"), ("b.mp4", "video")],
            [("a.webp", "sticker_static"), ("b.jpg", "photo")],
            [("a.mp3", "audio"), ("b.pdf", "document")],
        ):
            self.assertFalse(exportmedia.album_compatible(media))


if __name__ == "__main__":
    unittest.main()
