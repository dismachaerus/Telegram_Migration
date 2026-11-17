"""Build a single album caption with archive owner and forwarding attribution.

Telegram Desktop may flatten a grouped media post into several HTML records.
This module combines their distinct body captions while keeping the original
sender/date and forward attribution attached to the album itself.
"""
from copy import copy

from telethon.tl import types as tl

from .textutil import (
    compose_message,
    compose_message_header,
    format_archive_timestamp,
    strip_with_entities,
    u16len,
    validate_entities,
)


def caption_for_group_body(group):
    """Collect distinct source caption bodies and preserve their entities."""
    entities = []
    seen = set()
    text = ""
    for record in group:
        raw = record.get("body_text") or ""
        source_entities = validate_entities(
            raw, record.get("body_entities") or [], record.get("msg_id")
        )
        caption = raw.strip()
        if not caption or caption in seen:
            continue
        seen.add(caption)
        leading = len(raw) - len(raw.lstrip())
        trim_offset = u16len(raw[:leading])
        caption_length = u16len(caption)
        if text:
            text += "\n\n"
        base_offset = u16len(text)
        text += caption
        visible_start = trim_offset
        visible_end = trim_offset + caption_length
        for entity in source_entities:
            entity_start = int(entity.offset)
            entity_end = entity_start + int(entity.length)
            overlap_start = max(entity_start, visible_start)
            overlap_end = min(entity_end, visible_end)
            if overlap_end <= overlap_start:
                continue
            shifted = copy(entity)
            shifted.offset = base_offset + overlap_start - visible_start
            shifted.length = overlap_end - overlap_start
            entities.append(shifted)
    return text, entities


def _forward_attribution(record):
    if not record.get("is_forwarded"):
        return "", []
    forward_name = record.get("forward_name") or ""
    attribution = f"Forwarded from {forward_name}" if forward_name else "Forwarded message"
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
    return attribution + "\n\n", entities


def caption_for_group(group):
    """Build the album caption: owner/date, forwarding source, then unique captions."""
    if not group:
        return "", []
    first = group[0]
    header = compose_message_header(first.get("timestamp"), first.get("sender", ""))
    forward = _forward_attribution(first)
    body, body_entities = caption_for_group_body(group)
    text, entities = compose_message(
        header, ("", []), forward, body, body_entities,
    )
    return strip_with_entities(text, entities)
