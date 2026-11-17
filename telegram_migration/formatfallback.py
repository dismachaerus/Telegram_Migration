"""Retry a send without formatting when Telegram rejects message entities."""
from telethon.errors import FloodWaitError
from . import config, progress
from .textutil import u16len

def _is_entity_error(exc):
    """Identify Telegram entity validation errors without matching generic entity mentions."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "entit" in name or "invalid bounds" in msg or "entity_bounds" in msg

def _entity_list(value):
    """Flatten entities from one caption or a list of album caption rows."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [entity for item in value for entity in _entity_list(item)]
    if hasattr(value, "offset") and hasattr(value, "length"):
        return [value]
    return []

def _note_formatting_dropped(src, text, entities, exc):
    """Record why formatting was dropped, so the real cause can be found."""
    try:
        flat_entities = _entity_list(entities)
        progress.append_jsonl(config.settings.format_debug_file, {
            "src": src,
            "error": f"{type(exc).__name__}: {exc}",
            "text_utf16_len": u16len(text or ""),
            "text_repr": (text or "")[:400],
            "entities": [
                {"type": type(e).__name__, "offset": getattr(e, "offset", None),
                 "length": getattr(e, "length", None)}
                for e in flat_entities[:60]
            ],
        })
    except Exception:
        pass
    print(f"    note: Telegram rejected the formatting of source id={src} "
          f"({type(exc).__name__}); sending it as plain text (details: {config.settings.format_debug_file})")

async def with_formatting_fallback(send, kwargs, text, entities, src=None):
    """Retry without formatting if Telegram rejects any entity in a caption or album."""
    formatting = kwargs.get("formatting_entities")
    has_formatting = bool(_entity_list(formatting) or _entity_list(entities))
    try:
        return await send(**kwargs)
    except FloodWaitError:
        raise
    except Exception as exc:
        retryable = _is_entity_error(exc) or (
            has_formatting and isinstance(exc, (TypeError, ValueError))
        )
        if retryable and (has_formatting or _is_entity_error(exc)):
            _note_formatting_dropped(src, text, formatting or entities, exc)
            retry = dict(kwargs)
            retry["formatting_entities"] = None
            return await send(**retry)
        raise
