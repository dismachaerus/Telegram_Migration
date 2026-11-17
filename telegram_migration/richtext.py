"""Convert the rich text of an exported message (HTML) into text plus Telegram entities."""

from urllib.parse import urlparse

from telethon.tl import types as tl

from . import exportmedia, formatfallback
from .htmlutil import css_classes
from .textutil import (
    ordered_entities,
    sanitize_text, strip_with_entities, u16len,
)


class UnsupportedFormattingEntityError(RuntimeError):
    """The installed Telethon schema cannot represent an exported HTML tag."""


_URL_SCHEMES = ("http", "https", "tg", "ftp", "ftps")


def _make_entity(name, source_id=None, **kwargs):
    cls = getattr(tl, name, None)
    if cls is None:
        raise UnsupportedFormattingEntityError(
            f"source id={source_id}: installed Telethon does not support required HTML formatting entity {name}"
        )
    try:
        return cls(**kwargs)
    except TypeError as exc:
        # Older Telethon schema layers may not expose the optional collapsed
        # blockquote flag. No other constructor error is silently ignored.
        if "collapsed" not in kwargs:
            raise UnsupportedFormattingEntityError(
                f"source id={source_id}: could not construct required Telegram entity {name}: {exc}"
            ) from exc
        compatible = dict(kwargs)
        compatible.pop("collapsed")
        try:
            return cls(**compatible)
        except TypeError as compat_exc:
            raise UnsupportedFormattingEntityError(
                f"source id={source_id}: could not construct required Telegram entity {name}: {compat_exc}"
            ) from compat_exc


def _parse_rich_text(root, *, extract_stickers=False, source_id=None):
    if root is None:
        return "", [], [], []

    parts, entities, pos = [], [], [0]
    inline_stickers, missing_stickers = [], []
    pre_depth = [0]

    def add(s):
        s = sanitize_text(s)
        if s:
            parts.append(s)
            pos[0] += u16len(s)

    def emit(name, start, end, **extra):
        length = end - start
        if length > 0:
            e = _make_entity(name, source_id=source_id, offset=start, length=length, **extra)
            if e is not None:
                entities.append(e)

    def span(child, name, **extra):
        start = pos[0]
        walk(child)
        emit(name, start, pos[0], **extra)

    def walk(el):
        if el.text:
            add(el.text)
        for child in el:
            tag = child.tag if isinstance(child.tag, str) else ""
            cls = css_classes(child) if tag else []

            if not tag:
                pass                                   # comment / processing instruction

            elif tag == "br":
                add("\n")

            elif tag == "i" and "emoji" in cls:
                add("".join(child.itertext()))

            elif tag == "a" and child.get("href"):
                href = child.get("href")
                sticker = exportmedia.resolve_inline_sticker(href) if extract_stickers else None
                start = pos[0]
                walk(child)
                end = pos[0]
                if sticker and "path" in sticker:
                    alt = sanitize_text("".join(child.itertext()))
                    if alt and end - start == u16len(alt):
                        # Keep the ordinary fallback emoji in the text. It is
                        # the exact character range that MessageEntityCustomEmoji
                        # must cover once the sticker has been added to a pack.
                        inline_stickers.append({
                            "path": sticker["path"],
                            "kind": sticker["kind"],
                            "alt": alt,
                            "offset": start,
                            "length": end - start,
                        })
                    # Otherwise keep the fallback text, but do not invent a
                    # URL entity for a local file inside the export folder.
                else:
                    if sticker and sticker.get("missing"):
                        missing_stickers.append(sticker["missing"])
                    if href.startswith("mailto:"):
                        emit("MessageEntityEmail", start, end)
                    elif urlparse(href).scheme.lower() in _URL_SCHEMES:
                        # Preserve the exported target verbatim. Do not resolve
                        # Telegram user IDs or test whether any link is reachable.
                        emit("MessageEntityTextUrl", start, end, url=href)

            elif tag in ("b", "strong"):
                span(child, "MessageEntityBold")

            elif tag in ("i", "em"):
                span(child, "MessageEntityItalic")

            elif tag in ("u", "ins"):
                span(child, "MessageEntityUnderline")

            elif tag in ("s", "strike", "del"):
                span(child, "MessageEntityStrike")

            elif tag == "code":
                if pre_depth[0]:
                    walk(child)                        # already inside <pre>: no second entity
                else:
                    span(child, "MessageEntityCode")

            elif tag == "pre":
                inner = next((c for c in child if c.tag == "code"), None)
                lang = ""
                if inner is not None:
                    for c in css_classes(inner):
                        if c.startswith("language-"):
                            lang = c[len("language-"):]
                            break
                pre_depth[0] += 1
                try:
                    span(child, "MessageEntityPre", language=lang)
                finally:
                    pre_depth[0] -= 1

            elif tag == "blockquote":
                span(child, "MessageEntityBlockquote", collapsed=False)

            elif tag == "tg-spoiler" or (tag == "span" and ("spoiler" in cls or "tg-spoiler" in cls)):
                span(child, "MessageEntitySpoiler")

            else:
                walk(child)

            if child.tail:
                add(child.tail)

    walk(root)
    raw_text = "".join(parts)
    leading_units = u16len(raw_text) - u16len(raw_text.lstrip())
    text, entities = strip_with_entities(raw_text, entities)
    entities = formatfallback.prepare_formatting(
        text, ordered_entities(entities), source_id,
        context="HTML export conversion",
    )
    kept_stickers = []
    text_length = u16len(text)
    for sticker in inline_stickers:
        offset = sticker["offset"] - leading_units
        if offset < 0 or offset + sticker["length"] > text_length:
            continue
        kept_stickers.append({**sticker, "offset": offset})
    return text, entities, kept_stickers, list(dict.fromkeys(missing_stickers))


def parse_rich_text(root, source_id=None):
    """Convert HTML rich text to Telegram text and entities."""
    text, entities, _stickers, _missing = _parse_rich_text(root, source_id=source_id)
    return text, entities


def parse_rich_text_with_stickers(root, source_id=None):
    """Parse rich text while extracting actual inline sticker files in order."""
    return _parse_rich_text(root, extract_stickers=True, source_id=source_id)
