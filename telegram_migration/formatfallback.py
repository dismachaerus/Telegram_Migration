"""Send messages without ever silently discarding their formatting entities."""


async def with_formatting_fallback(send, kwargs, text, entities, src=None):
    """Compatibility wrapper: send exactly once and propagate every failure.

    The historical implementation retried entity-related failures with
    formatting_entities=None. That could silently destroy formatting in the
    destination. Entity errors must instead reach the normal failure logger so
    the offending source message can be diagnosed and retried after a real fix.
    The unused arguments remain in the signature to avoid changing all callers.
    """
    return await send(**kwargs)
