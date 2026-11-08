"""Find and classify the media files of an exported message."""

import os.
from urllib.parse import unquote, urlsplit

from . import config
from .htmlutil import css_classes, find_first


_GIF_LABELS = ("gif", "animation", "animated gif")


def _label_says_gif(label):
    """True only when a whole line of the export's media block is the GIF/Animation label.

    A file name or title that merely contains the word "GIF" (for example
    "my GIF collection.mp4") must not make an ordinary video a GIF.
    """
    prefixes = tuple(x + "," for x in _GIF_LABELS)
    for line in str(label or "").splitlines():
        t = line.strip().casefold()
        if t in _GIF_LABELS or t.startswith(prefixes):
            return True
    return False


def _is_gif(path, anchor_classes=(), export_label=""):
    """Identify GIFs only from explicit export metadata or a .gif filename.

    Telegram Desktop commonly stores animations as silent MP4 files. Their
    export markup may flag them as animated or label the media as GIF /
    Animation. A short, silent MP4 by itself is not enough evidence.
    """
    classes = {str(c).casefold() for c in anchor_classes}
    if any(c == "gif" or "animated" in c for c in classes):
        return True
    if os.path.splitext(str(path))[1].casefold() == ".gif":
        return True
    return _label_says_gif(export_label)


def _classify(rel, wrap_class, full, anchor_classes=(), export_label=""):
    """Classify export media using both wrapper/anchor markup and file extension."""
    rel = rel.replace("\\", "/")
    lower_rel = rel.casefold()
    classes = {str(c).casefold() for c in anchor_classes}
    is_sticker = (
        wrap_class == "sticker_wrap"
        or "sticker_wrap" in classes
        or "stickers/" in lower_rel
    )
    ext = os.path.splitext(lower_rel)[1]

    if is_sticker:
        if ext in (".webp", ".png"):
            return "sticker_static"
        if ext == ".tgs":
            return "sticker_animated"
        if ext == ".webm":
            return "sticker_video"
        return "sticker_unknown"

    if wrap_class == "roundvideo_file_wrap" or "round_video_messages" in lower_rel:
        return "round_video"
    if "voice_messages" in lower_rel:
        return "voice"
    if ext == ".gif":
        return "gif"
    if "video_files" in lower_rel or ext in (".mp4", ".mov", ".m4v", ".avi"):
        return "gif" if _is_gif(full, anchor_classes, export_label) else "video"
    # Telegram Desktop distinguishes actual photos by their photos/ export path
    # and/or photo-specific markup. Do not infer "photo" solely from .jpg/.png:
    # those extensions can also be ordinary files whose document form matters.
    photo_markup = any(
        c in {"photo", "photo_wrap", "photo_file_wrap"} or c.startswith("photo_")
        for c in classes
    )
    if "photos/" in lower_rel or photo_markup:
        return "photo"
    return "document"


def _resolve_media_path(relative):
    """Path of an exported file, or None. The HTML may write file names raw or percent-encoded."""
    for candidate in (relative, unquote(relative)):
        path = os.path.join(config.settings.base_dir, candidate)
        if os.path.exists(path):
            return path
    return None


def _media_wrappers(msg):
    """Yield the export's media wrappers in the same order used by classification."""
    for wrap_class in ("media_wrap", "sticker_wrap", "roundvideo_file_wrap"):
        wrap = find_first(msg, wrap_class)
        if wrap is not None:
            yield wrap_class, wrap


def _is_local_media_reference(reference):
    """Return whether an export reference looks like a local exported file path."""
    reference = str(reference or "").strip()
    if not reference or reference.startswith(("#", "//")):
        return False
    parsed = urlsplit(reference)
    return not parsed.scheme and not parsed.netloc and bool(parsed.path)


def _reference_key(reference):
    """Normalize a media reference for duplicate detection without resolving it."""
    return os.path.normcase(os.path.normpath(unquote(str(reference)).replace("\\", "/")))


def find_media(msg):
    """Return existing exported media files, preferring each anchor's full file over its thumbnail.

    If an anchor points to a missing file, its image ``src`` is usually only a
    thumbnail. Do not silently substitute that thumbnail for the original.
    Missing references are reported separately by :func:`find_missing_media`.
    """
    results, seen = [], set()
    for wrap_class, wrap in _media_wrappers(msg):
        # Telegram Desktop's anchor href is the full exported media. An img src
        # nested inside it is normally only a thumbnail, so use img src as a
        # fallback only when the wrapper has no local file anchors at all.
        anchors = [
            a for a in wrap.iter("a")
            if _is_local_media_reference(a.get("href"))
        ]
        references = [(a.get("href"), a) for a in anchors]
        if not references:
            references = [
                (img.get("src"), img)
                for img in wrap.iter("img")
                if _is_local_media_reference(img.get("src"))
            ]

        export_label = "\n".join(t.strip() for t in wrap.itertext() if t and t.strip())
        for reference, source_element in references:
            path = _resolve_media_path(reference)
            if not path or path in seen:
                continue
            seen.add(path)
            if source_element.tag == "a":
                child_image_classes = [
                    cls for img in source_element.iter("img") for cls in css_classes(img)
                ]
            else:
                child_image_classes = css_classes(source_element)
            results.append((path, _classify(
                reference, wrap_class, path,
                css_classes(wrap) + css_classes(source_element) + child_image_classes,
                export_label,
            )))
    return results


def find_missing_media(msg):
    """Return local media references present in the HTML but absent from the export folder.

    Anchor hrefs take precedence over nested image src values because those src
    values are commonly reduced-size thumbnails. For wrappers without local
    anchors (notably stickers), img src is treated as the media reference.
    """
    missing, seen = [], set()
    for _wrap_class, wrap in _media_wrappers(msg):
        anchors = [
            a for a in wrap.iter("a")
            if _is_local_media_reference(a.get("href"))
        ]
        if anchors:
            references = [a.get("href") for a in anchors]
        else:
            references = [
                img.get("src") for img in wrap.iter("img")
                if _is_local_media_reference(img.get("src"))
            ]

        for reference in references:
            if _resolve_media_path(reference) is not None:
                continue
            key = _reference_key(reference)
            if key in seen:
                continue
            seen.add(key)
            # Keep the export-relative reference in the archive note; never
            # expose an absolute filesystem path in the migrated message.
            label = unquote(str(reference)).replace("\\", "/")
            while label.startswith("./"):
                label = label[2:]
            missing.append(label or os.path.basename(str(reference)))
    return missing


def media_note(msg_div):
    wrap = find_first(msg_div, "media_wrap")
    if wrap is None:
        return ""
    lines = [t.strip() for t in wrap.itertext() if t and t.strip()]
    if not lines:
        return ""
    note = " | ".join(lines)
    return "[" + (note[:500] + "..." if len(note) > 500 else note) + "]"


def album_compatible(media):
    """Telegram albums hold photos/videos together, or documents together. Nothing else."""
    kinds = {kind for _path, kind in media}
    if kinds <= {"photo", "video"}:
        return True
    if kinds == {"document"}:
        return True
    return False
