"""Strict validation for Telegram formatting entities before sending."""

from . import config, progress
from . import textutil


def validate_formatting(text, entities, source_id=None, *, context=None):
    """Validate and order entities; log full diagnostics without altering them."""
    try:
        return textutil.validate_entities(
            text, entities or [], source_id=source_id,
        )
    except textutil.EntityValidationError as exc:
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
        raise


async def with_formatting_fallback(send, kwargs, text, entities, src=None):
    """Historical call signature; validate, then send exactly once with formatting.

    The old implementation retried formatting failures with formatting_entities=None.
    That could silently damage the archive. This compatibility wrapper now validates
    before the API call and propagates every send error without a plain-text retry.
    """
    validate_formatting(text, entities, src)
    return await send(**kwargs)
