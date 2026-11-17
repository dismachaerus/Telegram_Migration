"""Verify Telegram media album delivery and repair missing captions."""

from telethon.errors import MessageNotModifiedError

from .textutil import trim_entity_trailing_whitespace, u16len


async def _album_message_list(value):
    """Normalize Telethon's single/list return shapes to a list of messages."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


async def _verify_album_delivery(client, channel, messages, captions,
                                 caption_entities, source_id):
    """Verify Telegram created one album and retained every supplied caption."""
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

    for index, (message, expected, raw_entities) in enumerate(
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

        # send_file can return an UpdateShortSentMessage-derived object whose
        # cached caption is stale. Before issuing an EditMessageRequest, inspect
        # the persisted Telegram message. This avoids an unnecessary edit (and
        # entity revalidation by Telegram) when the upload already retained the
        # exact caption.
        get_messages = getattr(client, "get_messages", None)
        if callable(get_messages):
            refreshed_before_edit = await get_messages(channel, ids=message.id)
            refreshed_before_edit_list = await _album_message_list(refreshed_before_edit)
            if refreshed_before_edit_list:
                persisted_text = getattr(refreshed_before_edit_list[0], "message", None)
                if persisted_text is None:
                    persisted_text = getattr(refreshed_before_edit_list[0], "text", None)
                if persisted_text == expected:
                    continue
                # Use the fetched server message as the edit target/context.
                message = refreshed_before_edit_list[0]

        # Validate and canonicalize at the final EditMessageRequest boundary.
        # This catches offsets outside the exact UTF-16 text sent to Telegram,
        # and guarantees outer spans precede nested entities.
        entities = trim_entity_trailing_whitespace(expected, raw_entities or [])
        text_utf16_len = u16len(expected)
        entities.sort(key=lambda entity: (
            int(getattr(entity, "offset", 0)),
            -int(getattr(entity, "length", 0)),
        ))
        encoded = expected.encode("utf-16-le")
        entity_ranges = [
            {
                "type": type(entity).__name__,
                "offset": getattr(entity, "offset", None),
                "length": getattr(entity, "length", None),
            }
            for entity in entities
        ]

        def splits_surrogate_pair(position):
            if position <= 0 or position >= text_utf16_len:
                return False
            previous = encoded[(position - 1) * 2] | (encoded[(position - 1) * 2 + 1] << 8)
            following = encoded[position * 2] | (encoded[position * 2 + 1] << 8)
            return 0xD800 <= previous <= 0xDBFF and 0xDC00 <= following <= 0xDFFF

        for entity in entities:
            offset = getattr(entity, "offset", None)
            length = getattr(entity, "length", None)
            if (
                not isinstance(offset, int)
                or not isinstance(length, int)
                or offset < 0
                or length <= 0
                or offset + length > text_utf16_len
                or splits_surrogate_pair(offset)
                or splits_surrogate_pair(offset + length)
            ):
                raise ValueError(
                    "Invalid album caption entity before EditMessageRequest: "
                    f"source id={source_id}, item={index}, "
                    f"text_utf16_len={text_utf16_len}, entities={entity_ranges!r}"
                )

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
            # Telegram confirms the requested caption is already present.
            continue
        except Exception as exc:
            if "entityboundsinvalid" in type(exc).__name__.lower():
                raise RuntimeError(
                    "Telegram rejected the canonical album caption entities "
                    f"for source id={source_id}, item={index}; "
                    f"text_utf16_len={text_utf16_len}, entities={entity_ranges!r}"
                ) from exc
            raise

        # Do not trust the Message object returned by edit_message: Telethon can
        # return an update object whose cached text is stale. Verify the persisted
        # server state with a fresh GetMessages request whenever available.
        edited_text = getattr(edited, "message", None) if edited is not None else None
        get_messages = getattr(client, "get_messages", None)
        if callable(get_messages):
            refreshed = await get_messages(channel, ids=message.id)
            refreshed_list = await _album_message_list(refreshed)
            if refreshed_list:
                edited_text = getattr(refreshed_list[0], "message", None)
            else:
                edited_text = None
        if edited_text != expected:
            actual_preview = repr(edited_text)
            expected_preview = repr(expected)
            raise RuntimeError(
                f"Caption verification failed for album item {index} "
                f"(source id={source_id}); fetched text did not match. "
                f"expected_len={len(expected)}, actual_len="
                f"{len(edited_text) if edited_text is not None else None}, "
                f"expected={expected_preview[:240]}, actual={actual_preview[:240]}"
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
