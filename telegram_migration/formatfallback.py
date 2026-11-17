"""Strict preparation and diagnostics for Telegram formatting entities."""

from . import config, progress
from . import textutil


def _record_entity_failure(text, entities, source_id, context, exc):
    try:
        progress.append_jsonl(config.settings.format_debug_file, {
            "source_id": source_id,
            "context": context,
            "error": str(exc),
            "text_utf16_len": textutil.u16len(text or ""),
            "text_repr": (text or "")[:1000],
            "entities": [
                {
                    "type": type(entity).__name__,
                    "offset": getattr(entity, "offset", None),
                    "length": getattr(entity, "length", None),
                    "url": getattr(entity, "url", None),
                    "language": getattr(entity, "language", None),
                    "document_id": getattr(entity, "document_id", None),
                }
                for entity in (entities or [])
            ],
        })
    except Exception:
        # The original validation error is more important than diagnostics.
        pass
    print(
        f"    formatting validation failed for source id={source_id}"
        + (f" ({context})" if context else "")
        + f": {exc} (details: {config.settings.format_debug_file})"
    )


def validate_formatting(text, entities, source_id=None, *, context=None):
    """Validate entity offsets without changing any entity span."""
    try:
        return textutil.validate_entities(
            text, entities or [], source_id=source_id,
        )
    except textutil.EntityValidationError as exc:
        _record_entity_failure(text, entities, source_id, context, exc)
        raise


def prepare_formatting(text, entities, source_id=None, *, context=None):
    """Apply only Telegram's mandatory trailing-whitespace rule, then validate.

    Out-of-range, malformed, crossing, or unsupported entity ranges are never
    clipped or discarded. They are logged and raised for the source message.
    """
    try:
        return textutil.prepare_entities_for_telegram(
            text, entities or [], source_id=source_id,
        )
    except textutil.EntityValidationError as exc:
        _record_entity_failure(text, entities, source_id, context, exc)
        raise


async def with_formatting_fallback(send, kwargs, text, entities, src=None):
    """Historical call signature; prepare, then send exactly once with formatting.

    The old implementation retried formatting failures with formatting_entities=None.
    That could silently damage the archive. This compatibility wrapper now applies
    Telegram's trailing-whitespace rule, validates all remaining spans, and propagates
    every send error without a plain-text retry.
    """
    prepared = prepare_formatting(text, entities, src)
    formatting = kwargs.get("formatting_entities")
    if isinstance(formatting, (list, tuple)) and (
        not formatting or hasattr(formatting[0], "offset")
    ):
        kwargs = dict(kwargs)
        kwargs["formatting_entities"] = prepared or None
    return await send(**kwargs)
