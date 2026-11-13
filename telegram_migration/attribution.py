"""Durable sender/date attribution for standalone Telegram stickers."""

import secrets

from telethon.tl import functions as fn
from telethon.tl import types as tl

from . import formatfallback, progress, uploads
from .textutil import clean_invalid_entities


def prepare_message_attribution(data, text, entities):
    """Mark a standalone TGS sticker for separate sender/date attribution."""
    media_items = data.get("media") or []
    if len(media_items) == 1:
        path, kind = media_items[0]
        if kind == "sticker_animated" and str(path).casefold().endswith(".tgs"):
            data["_sticker_attribution"] = True
            data["_sticker_attribution_text"] = text
            data["_sticker_attribution_entities"] = entities



def _sent_message_id_from_updates(result, random_id):
    """Get the server-assigned message ID corresponding to one MTProto random ID."""
    if getattr(result, "id", None) is not None:
        return int(result.id)
    for update in getattr(result, "updates", ()) or ():
        if (
            getattr(update, "random_id", None) == random_id
            and getattr(update, "id", None) is not None
        ):
            return int(update.id)
    for update in getattr(result, "updates", ()) or ():
        message = getattr(update, "message", None)
        if message is not None and getattr(message, "id", None) is not None:
            return int(message.id)
    return None


async def _send_attribution_reply(
    client, channel, target_message_id, text, entities, state, source_id,
    progress_key,
):
    """Reply in the linked discussion group, or post attribution after the item.

    A broadcast channel without a linked discussion group cannot have replies.
    In that case the attribution is published as the next standalone channel
    post. The selected route and MTProto random ID are checkpointed so retries
    do not switch destinations or intentionally create a second attribution.
    """
    progress_state = state.setdefault(progress_key, {})
    if progress_state.get("reply_message_id") is not None:
        return int(progress_state["reply_message_id"])

    if not progress_state.get("random_id"):
        progress_state["random_id"] = secrets.randbits(63) or 1
        progress.save_state(state["src"], state)

    random_id = int(progress_state["random_id"])
    entities = clean_invalid_entities(text, entities or [])

    if getattr(channel, "broadcast", False):
        channel_id = int(getattr(channel, "id"))
        access_hash = getattr(channel, "access_hash", None)
        if access_hash is None:
            raise RuntimeError(
                "Cannot attribute this channel post: Telegram did not provide "
                "the channel access hash."
            )
        input_channel = tl.InputChannel(
            channel_id=channel_id, access_hash=int(access_hash)
        )
        channel_peer = tl.InputPeerChannel(
            channel_id=channel_id, access_hash=int(access_hash)
        )

        delivery_mode = progress_state.get("delivery_mode")
        standalone = delivery_mode == "standalone_channel_post"
        discussion_peer = None
        if not standalone:
            full = await client(fn.channels.GetFullChannelRequest(
                channel=input_channel
            ))
            linked_chat_id = getattr(
                getattr(full, "full_chat", None), "linked_chat_id", None
            )
            if not linked_chat_id or getattr(channel, "noforwards", False):
                if delivery_mode == "discussion_reply":
                    raise RuntimeError(
                        "The saved attribution route was the linked discussion group, "
                        "but that route is no longer available. Refusing to switch peers "
                        "with the same random_id."
                    )
                standalone = True
                progress_state["delivery_mode"] = "standalone_channel_post"
                progress.save_state(state["src"], state)
            elif (
                delivery_mode == "discussion_reply"
                and int(linked_chat_id) != int(progress_state.get("discussion_chat_id", -1))
            ):
                raise RuntimeError(
                    "The linked discussion group changed after the attribution route "
                    "was checkpointed. Refusing to reuse the random_id in another peer."
                )
            else:
                discussion_chat = next(
                    (
                        chat for chat in getattr(full, "chats", ()) or ()
                        if int(getattr(chat, "id", -1)) == int(linked_chat_id)
                    ),
                    None,
                )
                if discussion_chat is None or getattr(
                    discussion_chat, "access_hash", None
                ) is None:
                    raise RuntimeError(
                        "Telegram reported a linked discussion group but did not "
                        "return its access hash. The bot must have access to that group."
                    )
                discussion_peer = tl.InputPeerChannel(
                    channel_id=int(linked_chat_id),
                    access_hash=int(discussion_chat.access_hash),
                )
                progress_state["delivery_mode"] = "discussion_reply"
                progress_state["discussion_chat_id"] = int(linked_chat_id)
                progress.save_state(state["src"], state)

        if standalone:
            request = fn.messages.SendMessageRequest(
                peer=channel_peer,
                message=text,
                random_id=random_id,
                entities=entities or None,
                no_webpage=True,
            )
        else:
            reply_to = tl.InputReplyToMessage(
                reply_to_msg_id=int(target_message_id),
                reply_to_peer_id=channel_peer,
            )
            request = fn.messages.SendMessageRequest(
                peer=discussion_peer,
                message=text,
                random_id=random_id,
                reply_to=reply_to,
                entities=entities or None,
                no_webpage=True,
            )
        result = await client(request)
        reply_message_id = _sent_message_id_from_updates(result, random_id)
        if reply_message_id is None:
            raise RuntimeError(
                "Telegram accepted the attribution request but returned no message ID; "
                "the source message has not been marked complete. Restart to retry "
                "with the same random_id."
            )
    else:
        sent = await formatfallback.with_formatting_fallback(
            lambda **kw: client.send_message(channel, text, **kw),
            {
                "reply_to": int(target_message_id),
                "formatting_entities": entities or None,
                "parse_mode": None,
            },
            text, entities, source_id,
        )
        reply_message_id = int(sent.id)

    progress_state["reply_message_id"] = int(reply_message_id)
    progress.save_state(state["src"], state)
    return int(reply_message_id)


async def _send_sticker_with_attribution(
    client, channel, path, kind, reply_to, chunks, state, source_id,
    item_index=None,
):
    """Post a native TGS sticker without caption and attach its attribution.

    WebP and WebM sticker documents retain their native media captions.
    ``item_index`` gives each TGS sticker in one source post an independent
    checkpoint.
    """
    legacy_tgs = (
        item_index is None
        and kind == "sticker_animated"
        and str(path).casefold().endswith(".tgs")
    )
    prefix = (
        "tgs_sticker" if legacy_tgs else
        ("sticker" if item_index is None else f"sticker_item_{item_index}")
    )
    message_key = f"{prefix}_message_id"
    attribution_key = f"{prefix}_attribution"

    if message_key not in state:
        # Reuse an upload checkpoint from the earlier caption-based path. This
        # lets an interrupted migration add the missing attribution without
        # uploading a duplicate sticker.
        existing_ids = state.get("multi_media_ids") or []
        if item_index is not None and item_index < len(existing_ids):
            state[message_key] = int(existing_ids[item_index])
        elif item_index is None and state.get("media_id") is not None:
            state[message_key] = int(state["media_id"])
        else:
            sticker = await uploads.send_media_item(
                client, channel, path, kind, reply_to, source_id,
                caption="", entities=None,
            )
            state[message_key] = int(sticker.id)
        progress.save_state(state["src"], state)

    attribution_text, attribution_entities = chunks[0]
    await _send_attribution_reply(
        client, channel, state[message_key],
        attribution_text, attribution_entities, state, source_id,
        attribution_key,
    )
    return int(state[message_key])


# Backwards-compatible internal alias for older callers and partial states.
_send_tgs_sticker_with_attribution = _send_sticker_with_attribution
