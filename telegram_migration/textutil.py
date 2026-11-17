"""Text and entity helpers. All offsets and lengths are UTF-16 code units, as Telegram expects."""

import copy
import re

from telethon.tl import types as tl


def u16len(s):
    """Length in UTF-16 code units (Telegram's unit for entity offsets)."""
    return len(s.encode("utf-16-le")) // 2


class EntityValidationError(ValueError):
    """A formatting entity cannot be represented safely for Telegram."""


def ordered_entities(entities):
    """Return entities in Telegram's offset order, outer spans before inner ones.

    Invalid offset/length values sort to the beginning and are then reported by
    validate_entities with the entity's type and original values.
    """
    def key(entity):
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        safe_offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else -1
        safe_length = length if isinstance(length, int) and not isinstance(length, bool) else 0
        return safe_offset, -safe_length

    return sorted(list(entities or []), key=key)


def validate_entities(text, entities, *, source_id=None):
    """Validate entity ranges without clipping, dropping, or otherwise repairing them.

    Telegram entity offsets are UTF-16 code units. Ranges must start and end
    between complete Unicode code points, remain within the message, avoid
    crossing overlaps, and exclude trailing whitespace. Nested entities are
    supported; preformatted entities cannot be nested with other entities.
    """
    text = text or ""
    entities = ordered_entities(entities)
    try:
        encoded = text.encode("utf-16-le")
    except UnicodeEncodeError as exc:
        raise EntityValidationError(
            f"source id={source_id}: message text contains an unpaired Unicode surrogate"
        ) from exc

    text_length = len(encoded) // 2
    boundaries = {0}
    position = 0
    for char in text:
        codepoint = ord(char)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise EntityValidationError(
                f"source id={source_id}: message text contains an unpaired Unicode surrogate"
            )
        position += 2 if codepoint > 0xFFFF else 1
        boundaries.add(position)

    ranges = []
    for index, entity in enumerate(entities):
        entity_type = type(entity).__name__
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)

        def fail(reason):
            context = (
                f"source id={source_id}, entity #{index} {entity_type}, "
                f"offset={offset!r}, length={length!r}: {reason}; "
                f"text_utf16_length={text_length}, text={text[:240]!r}"
            )
            raise EntityValidationError(context)

        if isinstance(offset, bool) or not isinstance(offset, int):
            fail("offset is not an integer")
        if isinstance(length, bool) or not isinstance(length, int):
            fail("length is not an integer")
        if offset < 0:
            fail("offset is negative")
        if length <= 0:
            fail("length is zero or negative")
        end = offset + length
        if offset >= text_length or end > text_length:
            fail("entity range is outside the message text")
        if offset not in boundaries or end not in boundaries:
            fail("entity range splits a UTF-16 surrogate pair")

        selected = encoded[offset * 2:end * 2].decode("utf-16-le")
        if selected.rstrip() != selected:
            fail("entity range includes trailing whitespace or newline")

        ranges.append((offset, end, entity))

    # Telegram supports properly nested entities but not crossing ranges.
    stack = []
    for start, end, entity in ranges:
        while stack and start >= stack[-1][1]:
            stack.pop()
        if stack and end > stack[-1][1]:
            parent = stack[-1][2]
            raise EntityValidationError(
                f"source id={source_id}: crossing entity ranges: "
                f"{type(parent).__name__}({stack[-1][0]}:{stack[-1][1]}) and "
                f"{type(entity).__name__}({start}:{end}); text={text[:240]!r}"
            )
        if type(entity).__name__ == "MessageEntityPre":
            for other_start, other_end, other in ranges:
                if other is entity:
                    continue
                if start < other_end and other_start < end:
                    raise EntityValidationError(
                        f"source id={source_id}: MessageEntityPre cannot overlap "
                        f"{type(other).__name__}; text={text[:240]!r}"
                    )
        stack.append((start, end, entity))

    return entities


def prepare_entities_for_telegram(text, entities, *, source_id=None):
    """Apply Telegram's required trailing-whitespace rule, then validate strictly.

    This only shortens an entity's invisible trailing whitespace span, as
    specified by Telegram. It never clips an out-of-range entity or drops
    meaningful formatted text; malformed ranges raise EntityValidationError.
    """
    prepared = trim_entity_trailing_whitespace(text, entities or [])
    return validate_entities(text, prepared, source_id=source_id)


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
    # Telegram expects entities in ascending offset order. At the same
    # offset, an outer span must precede its shorter nested spans.
    entities = ordered_entities(entities)
    entities = prepare_entities_for_telegram(text, entities)
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
    text, entities = strip_with_entities(text, prepare_entities_for_telegram(text, entities))
    entities = prepare_entities_for_telegram(text, entities)
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
        ents = prepare_entities_for_telegram(
            seg, _clip_entities(entities, start, end)
        )
        if seg:
            chunks.append((seg, ents))
        start, lim = nxt, limit
    return chunks or [(text, entities)]


def trim_entity_trailing_whitespace(text, entities):
    """Shorten valid entity spans so they do not include trailing whitespace.

    Telegram requires entity lengths to exclude trailing whitespace and
    newlines. Invalid bounds are deliberately left untouched for callers that
    perform strict validation; URL targets and all other metadata are preserved.
    """
    if not entities:
        return entities
    encoded = (text or "").encode("utf-16-le")
    text_len = len(encoded) // 2
    out = []
    for entity in entities:
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        if (
            not isinstance(offset, int)
            or not isinstance(length, int)
            or length <= 0
            or offset < 0
            or offset + length > text_len
        ):
            out.append(entity)
            continue

        # surrogatepass leaves malformed split-pair ranges intact so a strict
        # caller can diagnose them rather than hiding the invalid boundary.
        visible = encoded[offset * 2:(offset + length) * 2].decode(
            "utf-16-le", errors="surrogatepass"
        )
        trimmed_length = u16len(visible.rstrip())
        if trimmed_length <= 0:
            # Leave the range untouched so strict validation reports it.
            # Never silently discard an entity that covers only whitespace.
            out.append(entity)
            continue
        if trimmed_length < length:
            entity = copy.copy(entity)
            entity.length = trimmed_length
        out.append(entity)
    return out


def clean_invalid_entities(text, entities):
    """Backward-compatible name for strict validation; no invalid entity is repaired."""
    return validate_entities(text, entities)
