"""Verify Telegram media album delivery and repair missing captions."""

from telethon.errors import MessageNotModifiedError

from .textutil import validate_entities


async def _album_message_list(value):
    """Normalize Telethon's single/list return shapes to a list of messages."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _same_entity_spans(actual_entities, expected_entities):
    """Compare entity types and UTF-16 spans, ignoring server-only flags."""
    def signature(entity):
        name = type(entity).__name__
        if name == "InputMessageEntityMentionName":
            name = "MessageEntityMentionName"
        return (
            name,
            int(getattr(entity, "offset", -1)),
            int(getattr(entity, "length", -1)),
        )

    actual = sorted(signature(entity) for entity in (actual_entities or []))
    expected = sorted(signature(entity) for entity in (expected_entities or []))
    return actual == expected


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
        # Apply the same UTF-16 bounds validation used by the send path. The
        # resume/audit path must never pass malformed source spans to EditMessage.
        # Validate and order entities without silently dropping formatting.
        entities = validate_entities(expected, entities or [], source_id)
        actual = getattr(message, "message", None)
        if actual is None:
            actual = getattr(message, "text", None)
        if actual is None:
            raise RuntimeError(
                f"Cannot verify caption for album item {index} "
                f"(source id={source_id}); Telegram returned no message text field"
            )
        actual_entities = getattr(message, "entities", None) or []
        if actual == expected and _same_entity_spans(actual_entities, entities):
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

        edited_message = edited
        edited_text = getattr(edited_message, "message", None) if edited_message is not None else None
        edited_entities = getattr(edited_message, "entities", None) if edited_message is not None else None
        if edited_text is None or (entities and edited_entities is None):
            get_messages = getattr(client, "get_messages", None)
            if callable(get_messages):
                refreshed = await get_messages(channel, ids=message.id)
                refreshed_list = await _album_message_list(refreshed)
                if refreshed_list:
                    edited_message = refreshed_list[0]
                    edited_text = getattr(edited_message, "message", None)
                    edited_entities = getattr(edited_message, "entities", None)
        if edited_text != expected:
            raise RuntimeError(
                f"Caption verification failed for album item {index} "
                f"(source id={source_id}); expected text was not retained"
            )
        if entities and not _same_entity_spans(edited_entities, entities):
            raise RuntimeError(
                f"Caption formatting verification failed for album item {index} "
                f"(source id={source_id}); expected entities were not retained"
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
