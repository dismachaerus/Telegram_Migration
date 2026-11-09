"""Helpers for inferred media albums reconstructed from Telegram Desktop HTML."""

from telethon.tl import types as tl

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
