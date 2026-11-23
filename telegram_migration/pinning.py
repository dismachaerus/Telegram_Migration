"""Restore Telegram message pins described by Telegram Desktop service events."""

import json
import os
import re


_PIN_LINK = re.compile(r"^#go_to_message(\d+)$")


def extract_pinned_message_id(service_elem):
    """Return the source message ID linked from a pin service event, if present."""
    for anchor in service_elem.iter("a"):
        match = _PIN_LINK.fullmatch((anchor.get("href") or "").strip())
        if match:
            return match.group(1)
    return None


def _report_path():
    return os.path.join(os.path.abspath(os.curdir), "migration_pins_report.jsonl")


def load_applied_pin_events(base_dir):
    """Load event IDs for pin actions confirmed by Telegram in an earlier run."""
    path = os.path.join(base_dir, "migration_pins_report.jsonl")
    applied = set()
    if not os.path.isfile(path):
        return applied
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if row.get("status") == "pinned" and row.get("event_src") is not None:
                applied.add(str(row["event_src"]))
    return applied


def _record_pin(base_dir, event_src, target_src, target_dst):
    path = os.path.join(base_dir, "migration_pins_report.jsonl")
    record = {
        "event_src": str(event_src),
        "target_src": str(target_src),
        "target_dst": int(target_dst),
        "status": "pinned",
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


async def apply_pin_event(client, channel, event_src, target_src, id_map,
                          applied_pin_events, base_dir, dry_run=False):
    """Pin the already-migrated destination message named by a service event."""
    event_src = str(event_src)
    target_src = str(target_src)
    if event_src in applied_pin_events:
        return

    target_dst = id_map.get(target_src)
    if target_dst is None:
        raise RuntimeError(
            f"pin event source id={event_src} refers to source message id={target_src}, "
            "but that message has no destination ID mapping"
        )

    if dry_run:
        print(
            f"  [dry] pin event id={event_src}: "
            f"source id={target_src} -> destination id={target_dst}"
        )
        applied_pin_events.add(event_src)
        return

    await client.pin_message(channel, int(target_dst), notify=False)
    _record_pin(base_dir, event_src, target_src, target_dst)
    applied_pin_events.add(event_src)
    print(
        f"  pinned source id={target_src} "
        f"(destination id={target_dst}) from service event id={event_src}"
    )
