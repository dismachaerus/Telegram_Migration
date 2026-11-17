"""Diagnose Telegram entity-bound errors without silently discarding formatting."""

from telethon.errors import FloodWaitError

from . import config, progress
from .textutil import u16len


def _is_entity_error(exc):
    """Identify Telegram entity validation errors."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "entit" in name or "invalid bounds" in msg or "entity_bounds" in msg


def _as_rows(captions, entities):
    """Pair each caption with its own entity list, preserving album alignment."""
    if isinstance(captions, (list, tuple)):
        texts = list(captions)
    else:
        texts = [captions or ""]

    if entities is None:
        rows = []
    elif isinstance(entities, (list, tuple)) and entities and all(
        isinstance(item, (list, tuple)) for item in entities
    ):
        rows = list(entities)
    elif isinstance(entities, (list, tuple)):
        rows = [entities]
    else:
        rows = [[]]

    count = max(len(texts), len(rows))
    return [
        (i, texts[i] if i < len(texts) else "",
         list(rows[i]) if i < len(rows) else [])
        for i in range(count)
    ]


def _boundary_is_valid(text, offset):
    """Return false when a UTF-16 offset splits a surrogate pair."""
    encoded = (text or "").encode("utf-16-le")
    units = len(encoded) // 2
    if not isinstance(offset, int) or offset < 0 or offset > units:
        return False
    if offset in (0, units):
        return True
    previous = int.from_bytes(encoded[2 * (offset - 1):2 * offset], "little")
    following = int.from_bytes(encoded[2 * offset:2 * (offset + 1)], "little")
    return not (0xD800 <= previous <= 0xDBFF and 0xDC00 <= following <= 0xDFFF)


def _diagnose_rows(captions, entities):
    """Describe every caption/entity row against that row's actual text."""
    diagnostics = []
    for index, text, row in _as_rows(captions, entities):
        text = text or ""
        text_len = u16len(text)
        details = []
        for entity in row:
            offset = getattr(entity, "offset", None)
            length = getattr(entity, "length", None)
            end = offset + length if isinstance(offset, int) and isinstance(length, int) else None
            valid = (
                isinstance(offset, int) and isinstance(length, int)
                and offset >= 0 and length > 0 and end <= text_len
                and _boundary_is_valid(text, offset)
                and _boundary_is_valid(text, end)
            )
            details.append({
                "type": type(entity).__name__,
                "offset": offset,
                "length": length,
                "end": end,
                "valid_bounds_and_utf16_boundaries": valid,
            })
        diagnostics.append({
            "caption_index": index,
            "text_utf16_len": text_len,
            "text_codepoint_len": len(text),
            "text": text,
            "entities": details,
        })
    return diagnostics


def _record_entity_error(src, captions, entities, exc):
    """Persist full, row-aligned diagnostics while retaining all source formatting."""
    diagnostics = _diagnose_rows(captions, entities)
    record = {
        "src": src,
        "error": f"{type(exc).__name__}: {exc}",
        "caption_rows": diagnostics,
    }
    try:
        progress.append_jsonl(config.settings.format_debug_file, record)
    except Exception:
        pass

    invalid = [
        f"caption {row['caption_index']}: "
        f"{entity['type']} offset={entity['offset']} length={entity['length']} "
        f"end={entity['end']} text_utf16_len={row['text_utf16_len']}"
        for row in diagnostics
        for entity in row["entities"]
        if not entity["valid_bounds_and_utf16_boundaries"]
    ]
    if invalid:
        detail = "; ".join(invalid)
        print(f"    invalid formatting entity for source id={src}: {detail}")
    else:
        print(
            f"    Telegram rejected entity formatting for source id={src}, "
            "but every supplied entity is within its caption's UTF-16 bounds. "
            f"Full per-caption diagnostics: {config.settings.format_debug_file}"
        )


async def with_formatting_fallback(send, kwargs, text, entities, src=None):
    """Send with formatting intact; never retry by silently removing all entities.

    Telegram's EntityBoundsInvalidError is a rejected request, so it is safe to
    report the failure, but retrying the same album without formatting changes
    the user's content. Keep the original exception and the original entities.
    """
    try:
        return await send(**kwargs)
    except FloodWaitError:
        raise
    except Exception as exc:
        if _is_entity_error(exc):
            _record_entity_error(
                src,
                kwargs.get("caption", text),
                kwargs.get("formatting_entities") or entities,
                exc,
            )
            if hasattr(exc, "add_note"):
                exc.add_note(
                    "Formatting was not discarded. Per-caption UTF-16 diagnostics "
                    f"were written to {config.settings.format_debug_file}."
                )
        raise
