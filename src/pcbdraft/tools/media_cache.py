"""Small local media cache shared by tools, independent of messaging services."""

from __future__ import annotations

import re
import uuid

from pcbdraft.core.runtime_environment import get_pcbdraft_dir

MAX_MEDIA_BYTES = 50 * 1024 * 1024


def _cache(data: bytes, directory: str, filename: str) -> str:
    if not isinstance(data, bytes) or len(data) > MAX_MEDIA_BYTES:
        raise ValueError("Invalid or oversized media payload")
    # Filenames are display hints only, never paths supplied by the server.
    name = re.sub(r"[^\w. -]", "_", filename.replace("\\", "/").rsplit("/", 1)[-1])
    name = name.strip(" .")[:150] or "resource.bin"
    legacy_names = {
        "images": "image_cache",
        "audio": "audio_cache",
        "documents": "document_cache",
    }
    root = get_pcbdraft_dir(f"cache/{directory}", legacy_names[directory])
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{uuid.uuid4().hex}_{name}"
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(data)
    return str(path)


def cache_image_from_bytes(data: bytes, ext: str = ".png") -> str:
    return _cache(data, "images", "image" + ext)


def cache_audio_from_bytes(data: bytes, ext: str = ".ogg") -> str:
    return _cache(data, "audio", "audio" + ext)


def cache_document_from_bytes(data: bytes, filename: str) -> str:
    return _cache(data, "documents", filename)
