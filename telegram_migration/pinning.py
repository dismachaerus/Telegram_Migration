"""Restore each Telegram pin service event at its position in the export."""

import json
import os


def _successful_event_ids(report_file):
    completed = set()
    if not os.path.exists(report_file):
        return completed
    with open(report_file, encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if (
                isinstance(record, dict)
                and record.get("status") == "pinned"
                and record.get("event_src_id") is not None
            ):
                completed.add(str(record["event_src_id"]))
    return completed


def _append_report(report_file, record):
    os.makedirs(os.path.dirname(os.path.abspath(report_file)), exist_ok=True)
    with open(report_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


async def pin_service_event(
    client,
    channel,
    event_source_id,
    target_source_id,
    id_map,
    source_ids,
    processed_source_ids,
    timestamp,
    report_file,
    *,
    dry_run=False,
):
    """Pin one target immediately when its service event is encountered.

    Successful event IDs are checkpoints, so a resumed migration can repair a
    missing pin without resending an already-migrated service notification.
    """
    event_id = str(event_source_id or "")
    target_id = str(target_source_id or "")
    if not event_id or not target_id:
        return "unresolved"

    if event_id in _successful_event_ids(report_file):
        return "already_pinned"

    destination_id = id_map.get(target_id)
    record = {
        "event_src_id": event_id,
        "target_src_id": target_id,
        "target_dst_id": int(destination_id) if destination_id is not None else None,
        "timestamp": timestamp.isoformat(sep=" ") if timestamp is not None else None,
    }

    if destination_id is None:
        if target_id in source_ids and target_id not in processed_source_ids:
            print(
                f"    PIN DEFERRED event id={event_id}: "
                f"target source id={target_id} has not been migrated yet"
            )
            return "deferred"
        record["status"] = "unresolved"
        record["error"] = "No migrated destination ID exists for the pinned source message."
        _append_report(report_file, record)
        print(
            f"    PIN UNRESOLVED event id={event_id}: "
            f"target source id={target_id} has no destination message"
        )
        return "unresolved"

    if dry_run:
        print(
            f"    [dry] pin event id={event_id}: "
            f"source id={target_id} -> destination id={destination_id}"
        )
        return "previewed"

    try:
        await client.pin_message(channel, int(destination_id), notify=False)
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        _append_report(report_file, record)
        print(
            f"    PIN FAILED event id={event_id}: "
            f"source id={target_id} -> destination id={destination_id}: {exc}"
        )
        return "failed"

    record["status"] = "pinned"
    _append_report(report_file, record)
    print(
        f"    Pinned at event id={event_id}: "
        f"source id={target_id} -> destination id={destination_id}"
    )
    return "pinned"
