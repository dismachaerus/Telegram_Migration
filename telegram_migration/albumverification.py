"""Verify Telegram media album delivery and repair missing captions."""

from telethon.errors import MessageNotModifiedError

from . import formatfallback


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

        # Apply Telegram's required trailing-whitespace rule, then strictly
        # validate all ranges before the final EditMessageRequest. No invalid
        # entity is clipped or discarded.
        entities = formatfallback.prepare_formatting(
            expected, raw_entities or [], source_id,
            context=f"album verification item {index}",
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
            if entities and formatfallback.is_entity_error(exc):
                formatfallback.record_send_failure(
                    expected, entities, source_id,
                    f"album caption edit item {index}", exc,
                )
                raise formatfallback.FormattingDeliveryError(
                    f"Telegram rejected album caption formatting for "
                    f"source id={source_id}, item={index}: {exc}"
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
