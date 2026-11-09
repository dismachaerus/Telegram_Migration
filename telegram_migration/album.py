"""Helpers for inferred media albums reconstructed from Telegram Desktop HTML."""

from telethon.tl import types as tl

from . import exportmedia
from .htmlutil import css_classes

from .textutil import (
    compose_message,
    compose_message_header,
    format_archive_timestamp,
    strip_with_entities,
    u16len,
)


def caption_for_record(record):
    """Build the original archive header and forward attribution for one album item."""
    timestamp = record.get("timestamp")
    if record.get("service_event"):
        header = compose_message_header(
            timestamp, "Telegram service event", date_only=True
        )
    else:
        header = compose_message_header(timestamp, record.get("sender", ""))

    if record.get("is_forwarded"):
        forward_name = record.get("forward_name") or ""
        label = f"Forwarded from {forward_name}" if forward_name else "Forwarded message"
        attribution = label
        entities = []
        if forward_name:
            entities.append(tl.MessageEntityBold(
                offset=u16len("Forwarded from "),
                length=u16len(forward_name),
            ))

        forward_timestamp = record.get("forward_timestamp")
        if forward_timestamp is not None:
            forward_date = format_archive_timestamp(forward_timestamp, date_only=True)
            if record.get("forward_timestamp_has_time"):
                time_format = "%H:%M:%S" if record.get("forward_timestamp_has_seconds") else "%H:%M"
                forward_date += f" · {forward_timestamp:{time_format}}"
            entities.append(tl.MessageEntityItalic(
                offset=u16len(attribution + "\n"),
                length=u16len(forward_date),
            ))
            attribution += "\n" + forward_date

        try:
            quote = tl.MessageEntityBlockquote(
                offset=0, length=u16len(attribution), collapsed=False
            )
        except TypeError:
            quote = tl.MessageEntityBlockquote(offset=0, length=u16len(attribution))
        entities.append(quote)
        text, ents = compose_message(
            header, ("", []), (attribution + "\n\n", entities),
            record.get("body_text", ""), record.get("body_entities", []),
        )
    else:
        text, ents = compose_message(
            header, ("", []), ("", []),
            record.get("body_text", ""), record.get("body_entities", []),
        )
    return strip_with_entities(text, ents)


def prepare_inferred_group(data, group, id_map, manual_done, partial, persist_id):
    """Attach album payload and captions, recovering mappings after an interrupted send."""
    if not group or len(group) < 2:
        return False

    member_ids = [str(member["msg_id"]) for member in group]
    saved_state = partial.get(str(data["msg_id"]), {})
    saved_album_ids = saved_state.get("album_message_ids")
    tail_counts = saved_state.get("album_caption_tail_counts")
    tails_complete = True
    if tail_counts is not None:
        for index, expected in enumerate(tail_counts):
            done = saved_state.get(f"album_caption_tail_{index}", {}).get("done", 0)
            if done < expected:
                tails_complete = False
                break
    if (saved_album_ids and len(saved_album_ids) == len(member_ids)
            and tails_complete):
        # Recover mappings only after any overflow-caption replies have also
        # been checkpointed. Otherwise _send_album must resume those replies.
        for member_id, destination_id in zip(member_ids, saved_album_ids):
            if member_id not in id_map:
                id_map[member_id] = int(destination_id)
                persist_id(member_id, destination_id)

    already_mapped = [
        member_id for member_id in member_ids
        if member_id in id_map or member_id in manual_done
    ]
    if already_mapped and len(already_mapped) != len(member_ids):
        # A previous run may have sent some members individually. Never resend
        # those records as part of a newly inferred album.
        return False

    captions = []
    caption_entities = []
    all_media = []
    for member in group:
        caption_text, caption_ents = caption_for_record(member)
        captions.append(caption_text)
        caption_entities.append(caption_ents)
        all_media.extend(member["media"])

    data["_inferred_album"] = True
    data["_album_source_ids"] = member_ids
    data["_album_captions"] = captions
    data["_album_caption_entities"] = caption_entities
    data["media"] = all_media
    data["body_text"] = ""
    data["body_entities"] = []
    data["forward_source"] = None
    data["is_forwarded"] = False
    data["reply_src_id"] = None
    return True


def _album_candidate(data, effective_sender):
    """Whether one HTML record can start or continue a conservative album inference.

    The first member of an album is often not marked "joined" in Telegram
    Desktop HTML. Continuation markers and timestamps are evaluated between
    adjacent candidates in _album_records_compatible().
    """
    if len(data.get("media") or []) != 1 or data.get("inline_stickers"):
        return False
    if data.get("reply_src_id") or data.get("reactions") or data.get("media_note"):
        return False
    if data.get("forward_source") and data["forward_source"][0] != "__text__":
        # A native-forward source ID identifies a real source message; leave it
        # to the ordinary per-message forwarding path.
        return False
    path, kind = data["media"][0]
    if kind not in ("photo", "video", "document"):
        return False
    if not effective_sender and not data.get("forward_name"):
        return False
    if data.get("is_forwarded") and not data.get("forward_name"):
        return False
    return bool(data.get("timestamp"))


def _caption_signature(record):
    """Normalize a visible source caption for album matching."""
    import re
    return re.sub(r"\s+", " ", (record.get("body_text") or "")).strip()


def _album_records_compatible(left, right):
    """Check whether adjacent records share a plausible album context.

    Identical non-empty captions are strong evidence that separate HTML
    records are members of one Telegram media group. Telegram Desktop may
    attach a single album caption to only one member, so a one-sided caption
    is also accepted when the surrounding sender/forward context and source
    timestamps support a short consecutive sequence.
    """
    if left.get("_album_sender") != right.get("_album_sender"):
        return False
    if left.get("is_forwarded") != right.get("is_forwarded"):
        return False
    if left.get("forward_name", "") != right.get("forward_name", ""):
        return False
    if left.get("reply_src_id") or right.get("reply_src_id"):
        return False

    left_caption = _caption_signature(left)
    right_caption = _caption_signature(right)
    same_caption = bool(left_caption and right_caption and left_caption == right_caption)
    if left_caption and right_caption and not same_caption:
        return False

    left_time, right_time = left.get("timestamp"), right.get("timestamp")
    if left_time is None or right_time is None:
        return False
    outer_gap = (right_time - left_time).total_seconds()
    if outer_gap < 0:
        return False

    left_forward_time = left.get("forward_timestamp")
    right_forward_time = right.get("forward_timestamp")
    if (left_forward_time is None) != (right_forward_time is None):
        if not same_caption:
            return False

    if left_forward_time is not None and right_forward_time is not None:
        forward_gap = (right_forward_time - left_forward_time).total_seconds()
        if forward_gap < 0 or (forward_gap > 30 and not same_caption):
            return False
    elif not same_caption:
        # Without matching captions or original source timestamps, use the
        # Desktop continuation marker and a wider 30-second tolerance. The
        # two-second outer timestamp rule was too strict for real exports.
        if not right.get("joined") or outer_gap > 30:
            return False

    return exportmedia.album_compatible(left["media"] + right["media"])

def infer_media_groups(files):
    """Infer likely media groups from consecutive media records in Desktop HTML.

    Desktop HTML does not retain Telegram's grouped_id. Album starts may lack
    the "joined" class, so single-media records are held as possible starts.
    Ordinary continuations still require the joined marker and close export
    timestamps; forwarded continuations can instead use close original
    forward timestamps from the same source author. If one standalone
    non-joined candidate is followed by ten compatible joined candidates,
    the ten-item continuation is inferred as its own album, respecting
    Telegram's hard album limit without relying on source-specific IDs. Other
    ambiguous runs longer than ten are rejected rather than guessed.
    """
    from lxml import etree

    from . import htmlexport

    groups = {}
    for page in files:
        pending = []
        overflow = False
        last_sender = ""

        def flush():
            nonlocal pending, overflow
            if not overflow and 2 <= len(pending) <= 10:
                groups[pending[0]["msg_id"]] = pending[:]
            pending = []
            overflow = False

        with open(page, "rb") as export_file:
            context = etree.iterparse(
                export_file, events=("end",), tag="div", html=True, encoding="utf-8"
            )
            for _event, elem in context:
                if "message" not in css_classes(elem):
                    continue

                classes = css_classes(elem)
                if "service" in classes:
                    flush()
                    continue

                data = htmlexport.extract_message_data(elem)
                if data is None:
                    flush()
                    continue

                if data.get("sender"):
                    last_sender = data["sender"]
                effective_sender = data.get("sender") or (
                    last_sender if data.get("joined") else ""
                )

                if not _album_candidate(data, effective_sender):
                    flush()
                else:
                    data["sender"] = effective_sender
                    data["_album_sender"] = effective_sender or data.get("forward_name", "")
                    if not pending:
                        pending = [data]
                    elif _album_records_compatible(pending[-1], data):
                        pending.append(data)
                        if (
                            len(pending) == 11
                            and not pending[0].get("joined")
                            and all(member.get("joined") for member in pending[1:])
                        ):
                            # A non-joined media message followed by ten joined
                            # media records cannot be one Telegram album: albums
                            # have at most ten items. Keep the leading message
                            # independent and infer the maximum-size album from
                            # the joined continuation run. This is based on
                            # export structure, not message IDs or file names.
                            album_run = pending[1:]
                            groups[album_run[0]["msg_id"]] = album_run[:]
                            pending = []
                        elif len(pending) > 10:
                            overflow = True
                    else:
                        flush()
                        pending = [data]

                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]

        flush()

    return groups


def stop_after_failed_album(ctx, source_id):
    """Stop before later records when an inferred album could not be sent."""
    ctx["stop"] = True
    print(
        f"  Stopped at failed inferred album beginning with id={source_id}. "
        "Later messages were left untouched; restart to retry this album."
    )


def map_destination_ids(source_ids, destination_ids, id_map, persist_id):
    """Persist one destination message ID for every original album source record."""
    if len(source_ids) != len(destination_ids):
        raise RuntimeError(
            f"Inferred album returned {len(destination_ids)} destination IDs "
            f"for {len(source_ids)} source messages"
        )
    for source_id, destination_id in zip(source_ids, destination_ids):
        id_map[str(source_id)] = int(destination_id)
        persist_id(source_id, destination_id)
    return int(destination_ids[0])
