"""Text and entity helpers. All offsets and lengths are UTF-16 code units, as Telegram expects."""

import copy
import re

from telethon.tl import types as tl


def u16len(s):
    """Length in UTF-16 code units (Telegram's unit for entity offsets)."""
    return len(s.encode("utf-16-le")) // 2


def _clip_entities(entities, a, b):
    """Entities restricted to [a, b) and re-based so that `a` becomes 0."""
    out = []
    for e in entities:
        s, t = max(e.offset, a), min(e.offset + e.length, b)
        if t > s:
            e2 = copy.copy(e)
            e2.offset = s - a
            e2.length = t - s
            out.append(e2)
    return out


def strip_with_entities(text, entities):
    """Strip leading/trailing whitespace and keep entities aligned.

    The leading offset is computed in UTF-16 code units (Telegram's unit)
    rather than Python codepoints. For ASCII/BMP whitespace the two are
    identical, but measuring in UTF-16 avoids an off-by-N shift if the
    composed message ever starts with a non-BMP codepoint.
    """
    stripped = text.lstrip()
    lead = u16len(text) - u16len(stripped)
    stripped = stripped.rstrip()
    if lead == 0 and len(stripped) == len(text):
        return text, entities
    return stripped, _clip_entities(entities, lead, lead + u16len(stripped))


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(s):
    """Normalise text BEFORE entity offsets are measured.

    Carriage returns become newlines and control characters are removed, so the
    text Telegram receives is exactly the text the offsets were computed on.
    """
    if not s:
        return s
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS_RE.sub("", s)


def _shift(e, shift):
    if shift == 0:
        return e
    e2 = copy.copy(e)
    e2.offset = e.offset + shift
    return e2


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def format_archive_timestamp(timestamp, date_only=False, include_seconds=False):
    """Format an exported timestamp for the readable Telegram archive header.

    Month names are deliberately locale-independent so the same export gets
    the same display on Windows, Linux, and Google Colab.
    """
    if timestamp is None:
        return ""
    day = f"{timestamp.day} {_MONTH_NAMES[timestamp.month - 1]} {timestamp.year}"
    if date_only:
        return day
    time_format = "%H:%M:%S" if include_seconds and timestamp.second else "%H:%M"
    return f"{day} · {timestamp:{time_format}}"


def compose_message_header(timestamp, sender, date_only=False, include_seconds=False):
    """Build a readable sender/date header with Telegram formatting entities.

    The sender is bold and the timestamp italic. Both offsets and lengths use
    UTF-16 code units, matching Telegram's entity format. The returned header
    ends with a blank line so the message body starts as a separate paragraph.
    """
    text = ""
    entities = []

    label = sanitize_text(str(sender or "")).replace("\n", " ").strip()
    if label:
        start = u16len(text)
        text += label
        length = u16len(label)
        if length:
            entities.append(tl.MessageEntityBold(offset=start, length=length))
        text += "\n"

    stamp = format_archive_timestamp(timestamp, date_only=date_only, include_seconds=include_seconds)
    if stamp:
        start = u16len(text)
        text += stamp
        length = u16len(stamp)
        if length:
            entities.append(tl.MessageEntityItalic(offset=start, length=length))
        text += "\n\n"
    elif label:
        text += "\n"

    return text, entities


def compose_message(watermark, sender, forward, body_text, body_entities, extra_before_body=None):
    text, entities = "", []
    parts = [watermark, sender, forward]
    if extra_before_body:
        parts.append(extra_before_body)
    parts.append((body_text, body_entities))
    for part, part_ents in parts:
        part = sanitize_text(part)
        if not part:
            continue
        base = u16len(text)
        text += part
        for e in part_ents:
            entities.append(_shift(e, base))
    return text, entities


def split_with_entities(text, entities, first_limit, limit):
    """Split text into chunks no larger than the limits.

    Whitespace is preserved at chunk boundaries: the whole input is stripped
    once at the top, then continuation chunks are NOT individually stripped,
    so ``"".join(piece for piece, _ in chunks) == stripped_text``.

    Surrogate pairs are never split across chunks. If a limit of 1 lands on a
    non-BMP character, that one chunk exceeds the limit by one UTF-16 unit
    rather than looping forever (a defensive guard; config rejects limits
    below 4, so this cannot happen through the public API).
    """
    if first_limit < 1 or limit < 1:
        raise ValueError(
            f"split limits must be at least 1 (got first_limit={first_limit}, limit={limit})"
        )
    text, entities = strip_with_entities(text, entities)
    b = text.encode("utf-16-le")
    n = len(b) // 2
    if n <= first_limit:
        return [(text, entities)]

    def unit(i):
        return b[2 * i] | (b[2 * i + 1] << 8)

    custom_emoji_ranges = [
        (int(entity.offset), int(entity.offset + entity.length))
        for entity in entities
        if type(entity).__name__ == "MessageEntityCustomEmoji"
    ]

    chunks, start, lim = [], 0, first_limit
    while start < n:
        hard = min(n, start + lim)
        if hard == n:
            end = nxt = n
        else:
            end = nxt = hard
            lo = start + (lim * 3) // 4
            for i in range(hard - 1, lo - 1, -1):
                if unit(i) in (0x0A, 0x20):
                    end = nxt = i + 1     # keep the whitespace in this chunk
                    break
            else:
                if 0xD800 <= unit(hard - 1) <= 0xDBFF:
                    end = nxt = hard - 1
        # A custom emoji entity must cover its entire fallback emoji in
        # one Telegram message. Never split its UTF-16 range between chunks.
        for entity_start, entity_end in custom_emoji_ranges:
            if entity_start < nxt < entity_end:
                if entity_start > start:
                    end = nxt = entity_start
                else:
                    # A single emoji may be longer than a deliberately tiny
                    # configured limit. Keep it intact in one slightly larger
                    # chunk rather than sending a broken custom-emoji entity.
                    end = nxt = entity_end
                break

        if nxt <= start:
            # Forward-progress guard: a surrogate pair at the boundary with
            # lim == 1 would otherwise leave nxt == start and loop forever.
            end = nxt = min(start + 2, n)
        seg = b[2 * start:2 * end].decode("utf-16-le")
        ents = _clip_entities(entities, start, end)
        if seg:
            chunks.append((seg, ents))
        start, lim = nxt, limit
    return chunks or [(text, entities)]


def _is_utf16_boundary(text, offset):
    """Whether a UTF-16 offset falls between Unicode code points."""
    encoded = (text or "").encode("utf-16-le")
    units = len(encoded) // 2
    if not isinstance(offset, int) or offset < 0 or offset > units:
        return False
    if offset in (0, units):
        return True
    previous = int.from_bytes(encoded[2 * (offset - 1):2 * offset], "little")
    following = int.from_bytes(encoded[2 * offset:2 * (offset + 1)], "little")
    return not (0xD800 <= previous <= 0xDBFF and 0xDC00 <= following <= 0xDFFF)


def clean_invalid_entities(text, entities):
    """Remove only malformed entities and preserve every valid formatting span.

    Telegram entity offsets are UTF-16 code units, but an offset must also land
    on a Unicode code-point boundary. A range can be numerically within the
    caption and still split the two UTF-16 units of a non-BMP character.
    """
    if not entities:
        return entities
    text = text or ""
    text_len = u16len(text)
    out = []
    for entity in entities:
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        if not isinstance(offset, int) or not isinstance(length, int):
            continue
        end = offset + length
        if (
            length <= 0 or offset < 0 or end > text_len
            or not _is_utf16_boundary(text, offset)
            or not _is_utf16_boundary(text, end)
        ):
            continue
        out.append(entity)
    return out
