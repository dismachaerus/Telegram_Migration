"""Helpers for inferred media albums reconstructed from Telegram Desktop HTML."""

from telethon.errors import MessageNotModifiedError
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

    # Preserve the caption and formatting associated with each source
    # message. Telegram's media-group API accepts one caption per media item;
    # do not merge distinct source messages into a synthetic album caption.
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
    if len(data.get("media") or []) != 1:
        return False
    if data.get("reply_src_id") or data.get("reactions") or data.get("media_note"):
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

    left_time, right_time = left.get("timestamp"), right.get("timestamp")
    if left_time is None or right_time is None:
        return False
    outer_gap = (right_time - left_time).total_seconds()
    if outer_gap < 0:
        return False

    # Telegram Desktop's "joined" class is strong evidence that this record
    # continues the preceding visual message. Captions belong to individual
    # media items, so different non-empty captions must not split an otherwise
    # consecutive album. Keep the original forwarded timestamps as a separate
    # boundary signal: a large jump there can indicate a new forwarded album
    # even when the export timestamps are only a second apart.
    joined_continuation = bool(right.get("joined") and outer_gap <= 30)
    if left_caption and right_caption and not same_caption and not joined_continuation:
        return False

    left_forward_time = left.get("forward_timestamp")
    right_forward_time = right.get("forward_timestamp")
    if (left_forward_time is None) != (right_forward_time is None):
        if not (same_caption or joined_continuation):
            return False

    if left_forward_time is not None and right_forward_time is not None:
        forward_gap = (right_forward_time - left_forward_time).total_seconds()
        if forward_gap < 0 or (forward_gap > 30 and not same_caption):
            return False
    elif not (same_caption or joined_continuation):
        # Without original source timestamps, only accept a short explicit
        # Desktop continuation. The outer timestamp is the export clock.
        if not right.get("joined") or outer_gap > 30:
            return False

    return exportmedia.album_compatible(left["media"] + right["media"])

def infer_media_groups(files, inline_emoji_ids=None):
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

                if inline_emoji_ids:
                    from .customemoji import apply_custom_emoji_entities
                    apply_custom_emoji_entities(data, inline_emoji_ids)

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


async def verify_mapped_album(client, channel, data, id_map, partial, source_id):
    """Audit a mapped album before skipping it; never resend media automatically."""
    from . import config, progress, sending
    from .textutil import split_with_entities

    source_ids = data["_album_source_ids"]
    state = dict(partial.get(str(source_id), {}) or {})
    saved_ids = state.get("album_message_ids")
    mapped_ids = [int(id_map[member_id]) for member_id in source_ids]
    if saved_ids is not None and [int(value) for value in saved_ids] != mapped_ids:
        raise RuntimeError(
            f"Partial album IDs disagree with the migration ID map for source id={source_id}; "
            "refusing to guess which destination messages belong to this album"
        )
    captions = []
    entities = []
    for caption, row in zip(
        data["_album_captions"], data["_album_caption_entities"]
    ):
        parts = split_with_entities(
            caption, row,
            config.settings.caption_limit, config.settings.text_limit,
        )
        captions.append(parts[0][0])
        entities.append(sending.clean_invalid_entities(parts[0][0], parts[0][1]))

    grouped_id = await _verify_saved_album(
        client, channel, mapped_ids, captions, entities, source_id
    )
    state["src"] = str(source_id)
    state["album_id"] = mapped_ids[0]
    state["album_message_ids"] = mapped_ids
    state["album_grouped_id"] = int(grouped_id)
    progress.save_state(source_id, state)
    print(
        f"    audited existing Telegram album for source id={source_id}: "
        f"{len(mapped_ids)} items, grouped_id={grouped_id}"
    )


async def _album_message_list(value):
    """Normalize Telethon's single/list return shapes to a list of messages."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


async def _verify_album_delivery(client, channel, messages, captions,
                                 caption_entities, source_id):
    """Verify Telegram created one album and retained every supplied caption.

    A successful send_file call and the expected number of returned IDs are not
    enough: Telegram may split the media group, or a caption may be lost.
    Fail closed rather than mapping the source records as a faithful album.
    """
    if len(messages) != len(captions):
        raise RuntimeError(
            f"Telegram returned {len(messages)} messages for source album "
            f"id={source_id}; expected {len(captions)}"
        )

    grouped_ids = [getattr(message, "grouped_id", None) for message in messages]
    if any(group_id is None for group_id in grouped_ids) or len(set(grouped_ids)) != 1:
        raise RuntimeError(
            f"Telegram did not confirm one grouped album for source id={source_id}; "
            f"returned grouped_id values={grouped_ids!r}. The source album has not "
            "been marked complete."
        )

    for index, (message, expected, entities) in enumerate(
        zip(messages, captions, caption_entities), start=1
    ):
        actual = getattr(message, "message", None)
        if actual is None:
            actual = getattr(message, "text", None)
        if actual is None:
            raise RuntimeError(
                f"Cannot verify caption for album item {index} "
                f"(source id={source_id}); Telegram returned no message text field"
            )
        if actual == expected:
            continue

        # Caption text is content, not decoration. If Telegram omitted or
        # altered it on upload, try to restore it on the already-sent message.
        edit_message = getattr(client, "edit_message", None)
        if not callable(edit_message):
            raise RuntimeError(
                f"Telegram did not retain caption for album item {index} "
                f"(source id={source_id}); no edit_message method is available"
            )
        try:
            edited = await edit_message(
                channel, message.id, expected,
                formatting_entities=entities or None, parse_mode=None,
            )
        except MessageNotModifiedError:
            # This is a positive server response: Telegram rejected the edit
            # specifically because the requested caption and formatting were
            # already present. The locally re-fetched TL object can encode
            # optional flags differently (notably blockquote.collapsed), so
            # comparing its raw entities would turn a confirmed no-op into a
            # false failure and cause the whole album to be retried.
            continue

        edited_text = getattr(edited, "message", None) if edited is not None else None
        if edited_text is None:
            get_messages = getattr(client, "get_messages", None)
            if callable(get_messages):
                refreshed = await get_messages(channel, ids=message.id)
                refreshed_list = await _album_message_list(refreshed)
                if refreshed_list:
                    edited_text = getattr(refreshed_list[0], "message", None)
        if edited_text != expected:
            raise RuntimeError(
                f"Caption verification failed for album item {index} "
                f"(source id={source_id}); expected text was not retained"
            )

    return grouped_ids[0]


async def _verify_saved_album(client, channel, saved_ids, captions,
                              caption_entities, source_id):
    """Fetch and verify an album recorded by an earlier interrupted run."""
    get_messages = getattr(client, "get_messages", None)
    if not callable(get_messages):
        raise RuntimeError(
            f"Cannot verify previously sent album for source id={source_id}; "
            "the Telegram client cannot fetch saved message IDs"
        )
    saved_messages = await get_messages(channel, ids=saved_ids)
    messages = await _album_message_list(saved_messages)
    if len(messages) != len(saved_ids) or any(message is None for message in messages):
        raise RuntimeError(
            f"Could not retrieve every saved message for source album id={source_id}; "
            "refusing to mark the album complete or resend it automatically"
        )
    return await _verify_album_delivery(
        client, channel, messages, captions, caption_entities, source_id
    )
