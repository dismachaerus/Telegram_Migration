"""Parse pin references and timestamps without changing service-message content."""

import re.
from datetime import datetime

from .htmlutil import css_classes, find_first, inner_text
from .htmlexport import parse_export_date
from .richtext import parse_rich_text


def extract_pinned_message_id(msg_div):
    """Return the source message ID linked by a Telegram pin service event."""
    classes = css_classes(msg_div)
    raw_id = msg_div.get("id", "")
    if "message" not in classes or "service" not in classes:
        return None
    if not re.fullmatch(r"message\d+", raw_id):
        return None
    for anchor in msg_div.iter("a"):
        match = re.fullmatch(r"#go_to_message(\d+)", anchor.get("href") or "")
        if match:
            return match.group(1)
    return None


def extract_pin_event_timestamp(msg_div, current_date=None):
    """Read a pin event's own timestamp for the pin audit report only."""
    clock_re = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")
    for element in msg_div.iter():
        if not isinstance(element.tag, str) or "date" not in css_classes(element):
            continue
        raw_values = [element.get("title") or "", inner_text(element)]
        for raw in raw_values:
            raw = raw.strip()
            if not raw:
                continue
            parsed = parse_export_date(raw)
            clock = clock_re.search(raw)
            if parsed is not None:
                return parsed
            if current_date is not None and clock:
                clock_format = "%H:%M:%S" if clock.group(1).count(":") == 2 else "%H:%M"
                try:
                    parsed_time = datetime.strptime(clock.group(1), clock_format).time()
                    return datetime.combine(current_date.date(), parsed_time)
                except ValueError:
                    pass
    return current_date


def extract_service_event_data(msg_div, current_date=None, *, pinned_message_id=None):
    """Extract a service event, preserving the original message text and date.

    The archived service-event layout stays exactly as before pin restoration:
    the surrounding date divider supplies the date, and the body is parsed
    without removing or relocating any HTML elements.
    """
    classes = css_classes(msg_div)
    if "message" not in classes or "service" not in classes:
        return None

    raw_id = msg_div.get("id", "")
    match = re.fullmatch(r"message(\d+)", raw_id)
    if not match:
        return None

    body = find_first(msg_div, "body")
    body_text, body_entities = parse_rich_text(body if body is not None else msg_div)
    body_text = body_text.strip()
    if not body_text:
        return None

    return {
        "msg_id": match.group(1),
        "joined": False,
        "timestamp": current_date,
        "service_event": True,
        "sender": "",
        "forward_header": "",
        "forward_source": None,
        "reply_src_id": str(pinned_message_id) if pinned_message_id else None,
        "reply_preview": "",
        "body_text": body_text,
        "body_entities": body_entities,
        "media": [],
        "media_note": "",
        "reactions": [],
    }
