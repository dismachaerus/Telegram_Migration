"""Import Telegram Desktop inline custom emojis into a bot-owned emoji set.

Telegram Desktop exports inline custom emojis as links to local sticker files,
with the fallback emoji as the link text. This module creates/reuses custom
emoji sticker sets and maps each exported asset to its Telegram document ID.
The rich-text parser then uses those IDs to create MessageEntityCustomEmoji
entities at the original UTF-16 offsets.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
import json
import os
import re
from collections import OrderedDict

from lxml import etree
from telethon.tl import functions, types as tl

from . import config, exportmedia, mediatools
from .htmlutil import css_classes, find_first
from .textutil import sanitize_text


_CACHE_VERSION = 1
_CACHE_NAME = "migration_custom_emoji_ids.json"
_PACK_SIZE = 200


@lru_cache(maxsize=4096)
def _asset_key_cached(path: str, kind: str, alt: str, mtime_ns: int, size: int) -> str:
    """Hash a file once per unchanged path/version during one migration run."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    identity = "\0".join((digest.hexdigest(), kind, os.path.splitext(path)[1].casefold(), alt))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _asset_key(path: str, kind: str, alt: str) -> str:
    stat = os.stat(path)
    return _asset_key_cached(
        os.path.abspath(path), kind, alt,
        getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
        stat.st_size,
    )


def reference_key(path: str, kind: str, alt: str) -> str:
    """Fast per-export lookup key used after the content hashes are prepared."""
    return "\0".join((os.path.normcase(os.path.abspath(path)), kind, alt))


def collect_inline_custom_emojis(files):
    """Find unique, existing inline emoji assets in export order.

    Returns (assets, reference_count). Each asset has a stable key, absolute
    path, kind, fallback emoji and a unique filename retained in Telegram's
    DocumentAttributeFilename so an interrupted pack build can be recovered.
    """
    assets = OrderedDict()
    reference_count = 0

    for page in files:
        with open(page, "rb") as export_file:
            context = etree.iterparse(
                export_file, events=("end",), tag="div", html=True, encoding="utf-8"
            )
            for _event, elem in context:
                classes = css_classes(elem)
                if "message" not in classes or "service" in classes:
                    continue
                text_div = find_first(elem, "text")
                if text_div is not None:
                    for anchor in text_div.iter("a"):
                        reference = anchor.get("href")
                        sticker = exportmedia.resolve_inline_sticker(reference)
                        if not sticker or "path" not in sticker:
                            continue
                        alt = sanitize_text("".join(anchor.itertext()))
                        if not alt or alt != alt.strip():
                            continue
                        kind = sticker["kind"]
                        if kind not in ("sticker_static", "sticker_animated", "sticker_video"):
                            continue
                        path = sticker["path"]
                        key = _asset_key(path, kind, alt)
                        reference_count += 1
                        alias = reference_key(path, kind, alt)
                        if key not in assets:
                            ext = os.path.splitext(path)[1].casefold()
                            assets[key] = {
                                "key": key,
                                "path": path,
                                "kind": kind,
                                "alt": alt,
                                "filename": f"tmx_{key}{ext}",
                                "reference_keys": [alias],
                            }
                        elif alias not in assets[key]["reference_keys"]:
                            assets[key]["reference_keys"].append(alias)
                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]

    return list(assets.values()), reference_count


def _cache_path():
    return os.path.join(config.settings.base_dir, _CACHE_NAME)


def _load_cache():
    path = _cache_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        if value.get("version") != _CACHE_VERSION or not isinstance(value.get("document_ids"), dict):
            return {}
        return value["document_ids"]
    except Exception as exc:
        print(f"  warning: could not read custom-emoji cache: {type(exc).__name__}: {exc}")
        return {}


def _save_cache(document_ids):
    path = _cache_path()
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(
                {"version": _CACHE_VERSION, "document_ids": document_ids},
                handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
        os.replace(temporary, path)
        return True
    except Exception as exc:
        print(f"  warning: could not save custom-emoji cache: {type(exc).__name__}: {exc}")
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
        return False


def _pack_short_name(keys, bot_username):
    digest = hashlib.sha256("\n".join(sorted(keys)).encode("ascii")).hexdigest()[:12]
    username = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "", str(bot_username).casefold()))
    if not username:
        raise RuntimeError("The Telegram bot needs a username to own a custom-emoji sticker set.")
    short_name = f"tgmig_{digest}_by_{username}"
    if len(short_name) > 64:
        raise RuntimeError("The bot username is too long for a Telegram sticker-set short name.")
    return short_name


def _mime_and_attributes(asset):
    path, kind = asset["path"], asset["kind"]
    ext = os.path.splitext(path)[1].casefold()
    filename = asset["filename"]
    attributes = [
        tl.DocumentAttributeFilename(file_name=filename),
        tl.DocumentAttributeCustomEmoji(
            free=True,
            alt=asset["alt"],
            stickerset=tl.InputStickerSetEmpty(),
        ),
    ]
    if kind == "sticker_static" and ext == ".webp":
        return "image/webp", attributes, False
    if kind == "sticker_animated" and ext == ".tgs":
        return "application/x-tgsticker", attributes, False
    if kind == "sticker_video" and ext == ".webm":
        metadata = mediatools.probe_video_metadata(path) or {}
        width = max(1, int(metadata.get("width") or 512))
        height = max(1, int(metadata.get("height") or 512))
        duration = max(0.1, float(metadata.get("duration") or 1.0))
        attributes.append(tl.DocumentAttributeVideo(
            duration=duration, w=width, h=height,
            supports_streaming=False, nosound=True,
        ))
        if not metadata:
            print(
                f"  warning: no video metadata for inline emoji {filename}; "
                "using 512x512 / 1s sticker attributes"
            )
        return "video/webm", attributes, True
    raise ValueError(f"Unsupported inline custom-emoji file: {path}")


async def _upload_custom_emoji_document(client, asset):
    """Upload one sticker document without posting it to the destination channel."""
    mime_type, attributes, nosound_video = _mime_and_attributes(asset)
    uploaded_file = await client.upload_file(
        asset["path"], file_name=asset["filename"]
    )
    media = tl.InputMediaUploadedDocument(
        file=uploaded_file,
        mime_type=mime_type,
        attributes=attributes,
        force_file=True,
        nosound_video=nosound_video,
    )
    result = await client(functions.messages.UploadMediaRequest(
        peer=tl.InputPeerEmpty(),
        media=media,
    ))
    document = getattr(result, "document", None)
    if document is None or not hasattr(document, "id"):
        raise RuntimeError(
            f"Telegram did not return a document while uploading {asset['filename']}"
        )
    return document


def _input_sticker_item(document, alt):
    reference = tl.InputDocument(
        id=int(document.id),
        access_hash=int(document.access_hash),
        file_reference=document.file_reference,
    )
    return tl.InputStickerSetItem(document=reference, emoji=alt)


def _document_names(sticker_set):
    """Index set documents by the stable filename embedded at upload time."""
    found = {}
    for document in getattr(sticker_set, "documents", ()) or ():
        filename = ""
        alt = ""
        for attribute in getattr(document, "attributes", ()) or ():
            if isinstance(attribute, getattr(tl, "DocumentAttributeFilename", ())):
                filename = attribute.file_name
            elif isinstance(attribute, getattr(tl, "DocumentAttributeCustomEmoji", ())):
                alt = attribute.alt
        if filename:
            found[(filename, alt)] = document
    return found


def _is_missing_sticker_set(exc):
    message = f"{type(exc).__name__}: {exc}".casefold()
    return any(token in message for token in (
        "stickerset_invalid",
        "sticker set is invalid",
        "stickersetinvalid",
    ))


def _is_short_name_occupied(exc):
    message = f"{type(exc).__name__}: {exc}".casefold()
    return any(token in message for token in ("pack_short_name_occupied", "packshortnameoccupied", "short name is occupied", "shortnameoccupied"))


async def _get_sticker_set(client, short_name):
    try:
        return await client(functions.messages.GetStickerSetRequest(
            stickerset=tl.InputStickerSetShortName(short_name=short_name),
            hash=0,
        ))
    except Exception as exc:
        if _is_missing_sticker_set(exc):
            return None
        raise


async def _create_sticker_set(client, short_name, title, asset, document):
    return await client(functions.stickers.CreateStickerSetRequest(
        user_id=tl.InputUserSelf(),
        title=title[:64],
        short_name=short_name,
        stickers=[_input_sticker_item(document, asset["alt"])],
        emojis=True,
    ))


async def _add_sticker_to_set(client, short_name, asset, document):
    return await client(functions.stickers.AddStickerToSetRequest(
        stickerset=tl.InputStickerSetShortName(short_name=short_name),
        sticker=_input_sticker_item(document, asset["alt"]),
    ))


async def _prepare_pack(client, assets, pack_index, username, cache, result_ids):
    keys = [asset["key"] for asset in assets]
    short_name = _pack_short_name(keys, username)
    sticker_set = await _get_sticker_set(client, short_name)
    names = _document_names(sticker_set) if sticker_set is not None else {}
    missing = []

    for asset in assets:
        document = names.get((asset["filename"], asset["alt"]))
        cached = cache.get(asset["key"])
        if document is not None:
            document_id = int(document.id)
        elif sticker_set is not None and isinstance(cached, dict) and cached.get("pack") == short_name:
            # Some older layers may omit filename attributes from returned
            # documents; trust a cache entry only while its owning pack exists.
            document_id = int(cached["id"])
        else:
            missing.append(asset)
            continue
        result_ids[asset["key"]] = document_id
        cache[asset["key"]] = {"id": document_id, "pack": short_name}

    if sticker_set is None and assets:
        # A new pack must be created with at least one sticker. If the first
        # file is invalid, try the next one rather than losing the whole pack.
        while missing and sticker_set is None:
            asset = missing.pop(0)
            try:
                document = await _upload_custom_emoji_document(client, asset)
                sticker_set = await _create_sticker_set(
                    client, short_name,
                    f"Telegram Migration custom emojis {pack_index}",
                    asset, document,
                )
                result_ids[asset["key"]] = int(document.id)
                cache[asset["key"]] = {"id": int(document.id), "pack": short_name}
                _save_cache(cache)
            except Exception as exc:
                if _is_short_name_occupied(exc):
                    sticker_set = await _get_sticker_set(client, short_name)
                    if sticker_set is not None:
                        names = _document_names(sticker_set)
                        for existing_asset in assets:
                            existing = names.get((
                                existing_asset["filename"], existing_asset["alt"]
                            ))
                            if existing is not None:
                                document_id = int(existing.id)
                                result_ids[existing_asset["key"]] = document_id
                                cache[existing_asset["key"]] = {
                                    "id": document_id, "pack": short_name
                                }
                    else:
                        print(f"  warning: could not reopen custom-emoji pack {short_name}")
                else:
                    print(
                        f"  warning: could not add inline custom emoji {asset['filename']}: "
                        f"{type(exc).__name__}: {exc}"
                    )
        if sticker_set is None:
            # No asset could create this pack. Keep all original fallback glyphs.
            return

    for asset in missing:
        if asset["key"] in result_ids:
            continue
        try:
            document = await _upload_custom_emoji_document(client, asset)
            await _add_sticker_to_set(client, short_name, asset, document)
            document_id = int(document.id)
            result_ids[asset["key"]] = document_id
            cache[asset["key"]] = {"id": document_id, "pack": short_name}
            _save_cache(cache)
        except Exception as exc:
            print(
                f"  warning: could not add inline custom emoji {asset['filename']}: "
                f"{type(exc).__name__}: {exc}. Its fallback glyph will remain plain text."
            )


async def prepare_inline_custom_emojis(client, files, *, dry_run=False):
    """Create/reuse bot-owned custom emoji sticker sets and return asset-key -> document ID.

    Dry runs never create sticker sets or upload files. Unavailable/unsupported
    assets are left as their original fallback emoji in the message text.
    """
    assets, references = collect_inline_custom_emojis(files)
    if not assets:
        return {}
    if dry_run:
        print(
            f"  DRY RUN: found {references} inline custom-emoji references "
            f"({len(assets)} unique assets); no sticker sets were created."
        )
        return {}

    me = await client.get_me()
    username = getattr(me, "username", None)
    if not username:
        raise RuntimeError("The migration bot must have a username to create custom-emoji packs.")

    cache = _load_cache()
    result_ids = {}
    pack_count = (len(assets) + _PACK_SIZE - 1) // _PACK_SIZE
    for index in range(pack_count):
        pack_assets = assets[index * _PACK_SIZE:(index + 1) * _PACK_SIZE]
        await _prepare_pack(client, pack_assets, index + 1, username, cache, result_ids)

    _save_cache(cache)
    reference_ids = {
        alias: result_ids[asset["key"]]
        for asset in assets if asset["key"] in result_ids
        for alias in asset["reference_keys"]
    }
    print(
        f"  Prepared {len(result_ids)}/{len(assets)} inline custom emoji assets "
        f"for {references} references in {pack_count} sticker set(s)."
    )
    if len(result_ids) != len(assets):
        print(
            "  warning: some custom emojis could not be registered; "
            "their original fallback glyphs will be sent as plain emoji."
        )
    return reference_ids


def apply_custom_emoji_entities(data, document_ids):
    """Attach MessageEntityCustomEmoji to fallback glyphs at their original offsets."""
    if not data or not document_ids:
        return 0
    entity_type = getattr(tl, "MessageEntityCustomEmoji", None)
    if entity_type is None:
        return 0
    text = data.get("body_text") or ""
    entities = list(data.get("body_entities") or [])
    added = 0
    for sticker in data.get("inline_stickers") or []:
        try:
            key = reference_key(sticker["path"], sticker["kind"], sticker["alt"])
        except (OSError, KeyError):
            continue
        document_id = document_ids.get(key)
        if document_id is None:
            continue
        offset = int(sticker["offset"])
        length = int(sticker["length"])
        if offset < 0 or length <= 0:
            continue
        # Telegram ignores a custom-emoji entity unless it wraps precisely its
        # declared fallback emoji; guard that invariant before sending.
        fragment = text.encode("utf-16-le")[2 * offset:2 * (offset + length)].decode("utf-16-le", errors="ignore")
        if fragment != sticker["alt"]:
            continue
        entities.append(entity_type(
            offset=offset,
            length=length,
            document_id=int(document_id),
        ))
        added += 1
    if added:
        entities.sort(key=lambda entity: int(getattr(entity, "offset", 0)))
        data["body_entities"] = entities
    return added
