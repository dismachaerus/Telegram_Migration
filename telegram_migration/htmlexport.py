"""Read one message out of a Telegram Desktop HTML export."""

import os
import re
from datetime import datetime
from urllib.parse import parse_qs
from urllib.parse import urlparse

from . import config, exportmedia
from .htmlutil import css_classes, find_first, inner_text, text_without
from .richtext import parse_rich_text
from .textutil import sanitize_text
from .constants import HTML_FILE_PATTERN


_DATE_SUFFIX = re.compile(r"\s*UTC[+-]\d{2}:\d{2}\s*$|\s*UTC\s*$")


def discover_export_files(base_dir):
    """Return Telegram Desktop HTML export pages in message order."""
    if not os.path.isdir(base_dir):
        return []
    pattern = re.compile(HTML_FILE_PATTERN)
    found = []
    for name in os.listdir(base_dir):
        match = pattern.match(name)
        if match:
            found.append((int(match.group(1) or 1), os.path.join(base_dir, name)))
    return [path for _, path in sorted(found)]


def parse_export_date(s):
    if not s:
        return None
    s = _DATE_SUFFIX.sub("", s).strip()
    for fmt in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _forwarded_body(msg_div):
    """Return Telegram Desktop's forwarded-message body, not its avatar.

    Desktop exports commonly place a ``forwarded userpic_wrap`` before the
    actual ``forwarded body``. Selecting the first element with the
    ``forwarded`` class therefore finds only the avatar and loses attribution.
    Prefer the explicit body class, with a guarded fallback for older export
    variants that omit it.
    """
    forwarded_elements = []
    for el in msg_div.iter():
        if not isinstance(el.tag, str):
            continue
        classes = css_classes(el)
        if "forwarded" not in classes:
            continue
        if "body" in classes:
            return el
        forwarded_elements.append(el)

    for el in forwarded_elements:
        classes = css_classes(el)
        if "userpic_wrap" in classes:
            continue
        if find_first(el, "from_name") is not None or find_first(el, "text") is not None:
            return el
    return None


def _forward_name(fwd_div):
    if fwd_div is None:
        return ""
    from_name = find_first(fwd_div, "from_name")
    a = next(from_name.iter("a"), None) if from_name is not None else None
    if a is not None and inner_text(a):
        return inner_text(a)
    return text_without(from_name, "date")


def _forward_timestamp(fwd_div):
    """Return the original forwarded-message timestamp from Desktop HTML.

    Telegram Desktop places this in a ``date details`` span nested inside
    the forwarded author's ``from_name`` element. Some export variants have
    a useful ``title`` attribute while others only expose visible text.
    """
    if fwd_div is None:
        return None, False, False
    from_name = find_first(fwd_div, "from_name")
    if from_name is None:
        return None, False, False
    for el in from_name.iter():
        if not isinstance(el.tag, str) or "date" not in css_classes(el):
            continue
        raw = (el.get("title") or "").strip()
        visible = inner_text(el).strip()
        parsed = parse_export_date(raw) if raw else None
        if parsed is None and visible:
            raw = visible
            parsed = parse_export_date(visible)
        if parsed is not None:
            has_time = bool(re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", raw))
            has_seconds = bool(re.search(r"\b\d{1,2}:\d{2}:\d{2}\b", raw))
            return parsed, has_time, has_seconds
    return None, False, False


def extract_forward_source(msg_div):
    fwd_div = _forwarded_body(msg_div)
    if fwd_div is None:
        return None

    name = _forward_name(fwd_div)
    author_a = next(fwd_div.iter("a"), None)
    href = (author_a.get("href") if author_a is not None else "") or ""

    if href.startswith("https://t.me/") or href.startswith("http://t.me/"):
        m = re.match(r"https?://t\.me/(?:c/)?([^/?#]+)/(\d+)", href)
        if m and not href.startswith(("https://t.me/c/", "http://t.me/c/")):
            return (m.group(1), int(m.group(2)))
        return ("__text__", name)

    if href.startswith("tg://openmessage"):
        qs = parse_qs(urlparse(href).query)
        uid = qs.get("user_id", [None])[0]
        mid = qs.get("message_id", [None])[0]
        if uid and mid and mid.isdigit():
            return (str(uid), int(mid))
        return ("__text__", name)

    return ("__text__", name)


def _extract_html_reactions(msg_div):
    """Best-effort extraction for reaction markup, if a particular export has it."""
    found = []
    seen = set()
    for el in msg_div.iter():
        if not isinstance(el.tag, str):
            continue
        classes = {c.casefold() for c in css_classes(el)}
        if not any("reaction" in c for c in classes):
            continue
        # Skip container nodes; their children carry the individual emoji/count.
        if any(c in {"reactions", "message_reactions", "reactions_wrap", "reaction_wrap", "reactions-container"}
               for c in classes):
            continue

        raw_attrs = {
            k: v for k, v in el.attrib.items()
            if k.casefold().startswith("data-") or k.casefold() in ("title", "aria-label", "alt")
        }
        label = (el.get("data-emoji") or el.get("data-reaction") or el.get("emoji") or
                 el.get("aria-label") or el.get("title") or "").strip()
        if not label:
            for img in el.iter("img"):
                label = (img.get("alt") or img.get("title") or img.get("data-emoji") or "").strip()
                if label:
                    break
        text = inner_text(el)
        count = None
        for attr in ("data-count", "data-reaction-count", "count"):
            value = el.get(attr)
            if value and str(value).isdigit():
                count = int(value)
                break
        if count is None:
            match = re.search(r"(?:^|\s)(\d+)\s*$", text)
            if match:
                count = int(match.group(1))
        if not label:
            label = re.sub(r"\s*\d+\s*$", "", text).strip()
        if not label or label.isdigit():
            continue
        key = (label, count)
        if key in seen:
            continue
        seen.add(key)
        found.append({"label": label, "count": count, "actors": [], "raw": raw_attrs, "source": "html"})
    return found


def _reply_preview(reply_div):
    if reply_div is None:
        return ""
    text = inner_text(reply_div)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _extract_reply_source_id(msg_div):
    reply_div = find_first(msg_div, "reply_to")
    if reply_div is None:
        return None
    a = next(reply_div.iter("a"), None)
    if a is None:
        return None
    match = re.search(r"#go_to_message(\d+)", a.get("href") or "")
    return match.group(1) if match else None


def extract_date_separator(msg_div):
    """Return the calendar date from Telegram Desktop's negative-ID date divider.

    Telegram Desktop writes date dividers as ``message service`` elements with
    negative IDs (for example ``message-1``). Actual service events retain their
    positive message IDs, so they can be distinguished and migrated separately.
    """
    classes = css_classes(msg_div)
    raw_id = msg_div.get("id", "")
    if "message" not in classes or "service" not in classes:
        return None
    if not re.fullmatch(r"message-\d+", raw_id):
        return None
    body = find_first(msg_div, "body")
    label = inner_text(body if body is not None else msg_div)
    return parse_export_date(label)


def extract_service_event_data(msg_div, current_date=None):
    """Extract a real Telegram service event from a Desktop HTML export.

    Negative ``message-N`` records are date separators, not Telegram events;
    callers should process them with :func:`extract_date_separator` instead.
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
        "timestamp_date_only": current_date is not None,
        "service_event": True,
        "sender": "",
        "forward_header": "",
        "forward_source": None,
        "reply_src_id": None,
        "reply_preview": "",
        "body_text": body_text,
        "body_entities": body_entities,
        "media": [],
        "media_note": "",
        "reactions": [],
    }


def extract_message_data(msg_div):
    classes = css_classes(msg_div)
    if "message" not in classes or "service" in classes:
        return None

    raw_id = msg_div.get("id", "")
    if not raw_id.startswith("message"):
        return None
    msg_id = raw_id[len("message"):]
    if not msg_id.isdigit():
        return None

    body = find_first(msg_div, "body")
    if body is None:
        body = msg_div

    date_div = None
    for el in body.iter():
        if el.tag == "div" and "date" in css_classes(el) and el.get("title"):
            date_div = el
            break
    timestamp = parse_export_date(date_div.get("title")) if date_div is not None else None

    sender = ""
    if config.settings.preserve_sender_names:
        for child in body:
            if isinstance(child.tag, str) and "from_name" in css_classes(child):
                sender = text_without(child, "date")
                break

    fwd_div = _forwarded_body(msg_div)
    forward_name = (
        sanitize_text(_forward_name(fwd_div)).replace("\n", " ").strip()
        if fwd_div is not None else ""
    )
    (forward_timestamp, forward_timestamp_has_time,
     forward_timestamp_has_seconds) = _forward_timestamp(fwd_div)
    # The source and original timestamp are rendered in a quoted attribution
    # header in the same reconstructed message/caption as the forwarded payload.
    forward_header = f"Forwarded from {forward_name}\n" if forward_name else ""
    fwd_src = extract_forward_source(msg_div) if config.settings.native_forward else None

    reply_div = find_first(msg_div, "reply_to")
    preview = _reply_preview(reply_div)
    reply_src_id = _extract_reply_source_id(msg_div)

    text_div = find_first(msg_div, "text")
    body_text, body_entities = parse_rich_text(text_div)

    media = exportmedia.find_media(msg_div)
    missing_media = exportmedia.find_missing_media(msg_div)
    if missing_media:
        # Keep the message even when one or more referenced attachments are
        # absent. If other attachments exist, they are still uploaded normally.
        note = "\n".join(f"[Media not found: {reference}]" for reference in missing_media)
    else:
        note = "" if media else exportmedia.media_note(msg_div)
    if note:
        body_text = (body_text + "\n\n" if body_text else "") + note

    return {
        "msg_id": msg_id,
        "joined": "joined" in classes,
        "timestamp": timestamp,
        "sender": sender,
        "forward_header": forward_header,
        "forward_name": forward_name,
        "forward_timestamp": forward_timestamp,
        "forward_timestamp_has_time": forward_timestamp_has_time,
        "forward_timestamp_has_seconds": forward_timestamp_has_seconds,
        "is_forwarded": fwd_div is not None,
        "forward_source": fwd_src,
        "reply_src_id": reply_src_id,
        "reply_preview": preview,
        "body_text": body_text,
        "body_entities": body_entities,
        "media": media,
        "media_note": note,
        "reactions": _extract_html_reactions(msg_div),
    }



def _album_candidate(data, effective_sender):
    """Whether one joined HTML record is eligible for conservative album inference."""
    if not data.get("joined"):
        return False
    if len(data.get("media") or []) != 1:
        return False
    if data.get("reply_src_id") or data.get("reactions") or data.get("media_note"):
        return False
    if data.get("forward_source") and data["forward_source"][0] != "__text__":
        # A native-forward source ID identifies a real source message; leave it
        # to the ordinary per-message forwarding path.
        return False
    path, kind = data["media"][0]
    if kind not in ("photo", "video", "document"):
        return False
    if not effective_sender and not data.get("forward_name"):
        return False
    if data.get("is_forwarded") and not data.get("forward_name"):
        return False
    return bool(data.get("timestamp"))


def _album_records_compatible(left, right):
    """Check that two adjacent records share the same plausible media-group context."""
    if not right.get("joined"):
        return False
    if left.get("_album_sender") != right.get("_album_sender"):
        return False
    if left.get("is_forwarded") != right.get("is_forwarded"):
        return False
    if left.get("forward_name", "") != right.get("forward_name", ""):
        return False
    if left.get("reply_src_id") or right.get("reply_src_id"):
        return False

    left_time, right_time = left.get("timestamp"), right.get("timestamp")
    if left_time is None or right_time is None:
        return False
    gap = (right_time - left_time).total_seconds()
    if gap < 0 or gap > 2:
        return False

    left_forward_time = left.get("forward_timestamp")
    right_forward_time = right.get("forward_timestamp")
    if (left_forward_time is None) != (right_forward_time is None):
        return False
    if left_forward_time is not None:
        forward_gap = (right_forward_time - left_forward_time).total_seconds()
        if forward_gap < 0 or forward_gap > 2:
            return False

    return exportmedia.album_compatible(left["media"] + right["media"])


def infer_media_groups(files):
    """Infer likely media groups from consecutive joined records in Desktop HTML.

    Desktop HTML does not retain Telegram's grouped_id, so this is deliberately
    conservative. Every inferred member must be a joined, single-media record;
    adjacent records must have the same sender/forward context, compatible media,
    no reply edge, and timestamps no more than two seconds apart. Groups larger
    than Telegram's ten-item limit are rejected as a whole rather than split.
    """
    from lxml import etree

    groups = {}
    for page in files:
        pending = []
        overflow = False
        last_sender = ""

        def flush():
            nonlocal pending, overflow
            if not overflow and 2 <= len(pending) <= 10:
                groups[pending[0]["msg_id"]] = pending[:]
            pending = []
            overflow = False

        with open(page, "rb") as export_file:
            context = etree.iterparse(
                export_file, events=("end",), tag="div", html=True, encoding="utf-8"
            )
            for _event, elem in context:
                if "message" not in css_classes(elem):
                    continue

                classes = css_classes(elem)
                if "service" in classes:
                    flush()
                    continue

                data = extract_message_data(elem)
                if data is None:
                    flush()
                    continue

                if data.get("sender"):
                    last_sender = data["sender"]
                effective_sender = data.get("sender") or (
                    last_sender if data.get("joined") else ""
                )

                if not _album_candidate(data, effective_sender):
                    flush()
                else:
                    data["sender"] = effective_sender
                    data["_album_sender"] = effective_sender or data.get("forward_name", "")
                    if not pending:
                        pending = [data]
                    elif _album_records_compatible(pending[-1], data):
                        pending.append(data)
                        if len(pending) > 10:
                            overflow = True
                    else:
                        flush()
                        pending = [data]

                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]

        flush()

    return groups


def inherit_forwarded_sender(data, context):
    """Restore a forwarded author omitted from joined continuation messages."""
    if not data.get("is_forwarded"):
        context["last_forward_name"] = ""
    elif data.get("forward_name"):
        context["last_forward_name"] = data["forward_name"]
    elif data.get("joined") and context.get("last_forward_name"):
        data["forward_name"] = context["last_forward_name"]
        data["forward_header"] = f"Forwarded from {data['forward_name']}\n"
    else:
        context["last_forward_name"] = ""
