"""Verify that migrated replies point at the right destination messages."""

import asyncio

from . import config, progress


def _reply_id_from_message(msg):
    reply_header = getattr(msg, "reply_to", None) if msg is not None else None
    actual = getattr(reply_header, "reply_to_msg_id", None) if reply_header is not None else None
    if actual is None and msg is not None:
        actual = getattr(msg, "reply_to_msg_id", None)
    return actual


def source_reply_target(data):
    """Return the source message that should become this destination reply."""
    if data.get("is_forwarded"):
        target = data.get("forward_reply_src_id")
        return str(target) if target else None
    target = data.get("reply_src_id")
    return str(target) if target else None


def resolve_reply(data, source_id, id_map, pending_replies, ctx):
    """Resolve or defer the source reply that should become Telegram's native reply."""
    target_src = source_reply_target(data)
    reply_fallback = ("", [])
    if not target_src:
        return None, reply_fallback, False

    reply_to = id_map.get(target_src)
    if reply_to is not None:
        return reply_to, reply_fallback, False
    if target_src in ctx["source_ids"] and target_src not in ctx["processed_source_ids"]:
        if not config.settings.dry_run:
            if pending_replies.get(source_id) != target_src:
                pending_replies[source_id] = target_src
                progress.save_pending_replies(pending_replies)
        else:
            print(f"  [dry] would defer reply id={source_id} until target id={target_src} is migrated")
        return None, reply_fallback, True

    if data.get("is_forwarded") and data.get("forward_reply_src_id"):
        return None, reply_fallback, False

    fallback = f"[Original reply target #{target_src} is unavailable in the destination]\n"
    if data.get("reply_preview"):
        fallback += f"Original reply preview: {data['reply_preview']}\n"
    return None, (fallback + "\n", []), False


async def verify_all_reply_links(client, channel, source_reply_map, id_map, audit):
    """Verify every available source reply, batching message reads. Results go into `audit`."""
    if not config.settings.verify_replies or not source_reply_map:
        return

    candidates = []
    for source_id, source_target in source_reply_map.items():
        source_id, source_target = str(source_id), str(source_target)
        if source_id not in id_map:
            continue
        expected = id_map.get(source_target)
        if expected is None:
            audit.append({
                "src": source_id,
                "source_reply_to": source_target,
                "dst": int(id_map[source_id]),
                "verified": False,
                "status": "target_not_migrated",
            })
        else:
            candidates.append((source_id, source_target, int(id_map[source_id]), int(expected)))

    print(f"Verifying reply links for {len(candidates)} migrated source message(s)...")
    # Telegram's GetMessages request accepts multiple IDs; use batches to avoid
    # making one network round-trip per reply in a large archive.
    batch_size = 100
    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset:offset + batch_size]
        dest_ids = [row[2] for row in batch]
        try:
            received = await client.get_messages(channel, ids=dest_ids)
            if received is None:
                received = []
            elif not isinstance(received, (list, tuple)):
                received = [received]
            by_id = {int(m.id): m for m in received if m is not None and getattr(m, "id", None) is not None}
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            for source_id, source_target, dest_id, expected in batch:
                audit.append({
                    "src": source_id,
                    "source_reply_to": source_target,
                    "dst": dest_id,
                    "expected_destination_reply_to": expected,
                    "verified": False,
                    "status": "verification_unavailable",
                    "error": reason,
                })
            print(f"  Warning: could not verify a batch of {len(batch)} replies: {reason}")
            continue

        for source_id, source_target, dest_id, expected in batch:
            msg = by_id.get(dest_id)
            actual = _reply_id_from_message(msg)
            verified = actual == expected
            audit.append({
                "src": source_id,
                "source_reply_to": source_target,
                "dst": dest_id,
                "expected_destination_reply_to": expected,
                "actual_destination_reply_to": actual,
                "verified": verified,
                "status": "verified" if verified else ("message_not_found" if msg is None else "mismatch"),
            })
            if not verified:
                print(f"  WARNING: reply id={source_id} expected destination target {expected}, got {actual}")
        await asyncio.sleep(0.1)
