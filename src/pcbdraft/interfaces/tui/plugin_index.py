"""Configured plugin index — fetch, cache, search, and name resolution.

PCBDraft has no built-in community marketplace. Users may configure a
machine-readable JSON index with ``plugins.index_url``; each configured URL is
cached locally under ``PCBDRAFT_RUNTIME_HOME/cache/`` with a TTL.

Fallback chain for a configured index: remote index → cached copy (fresh or
stale) → empty list. With no configured index the loader returns an empty
list without accessing the network or an old cache.

The index is discovery metadata ONLY.  **Indexed ≠ audited** — inclusion in
the index means the entry's metadata was reviewed, not that the plugin's code
was audited.  Install keeps its existing consent/review flow, and index
entries pin an immutable ref (tag or commit SHA).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pcbdraft.core.runtime_environment import get_runtime_home

logger = logging.getLogger(__name__)

# Cache the fetched index for 24 hours; a stale cache is still used when the
# explicitly configured remote is temporarily unreachable.
INDEX_CACHE_TTL = 24 * 3600

_FETCH_TIMEOUT = 10.0
_MAX_INDEX_BYTES = 5 * 1024 * 1024  # refuse absurdly large index payloads

SECURITY_FOOTER = (
    "Indexed \u2260 audited: inclusion in the index is a metadata review only, "
    "not a code audit. Review a plugin before enabling it."
)


@dataclass
class PluginIndexEntry:
    """One community plugin index entry."""

    name: str
    description: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    repo: str = ""  # "owner/name"
    ref: str = ""  # pinned tag or commit SHA
    subdir: str | None = None  # path within the repo (monorepos)
    homepage: str | None = None
    capabilities: list[str] = field(default_factory=list)
    api_version: int | None = None
    added_at: str | None = None

    @property
    def install_identifier(self) -> str:
        """Identifier accepted by the existing install path (owner/repo[/subdir])."""
        return f"{self.repo}/{self.subdir}" if self.subdir else self.repo

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "author": self.author,
            "tags": list(self.tags),
            "repo": self.repo,
            "ref": self.ref,
        }
        if self.subdir:
            d["subdir"] = self.subdir
        if self.homepage:
            d["homepage"] = self.homepage
        if self.capabilities:
            d["capabilities"] = list(self.capabilities)
        if self.api_version is not None:
            d["api_version"] = self.api_version
        if self.added_at:
            d["added_at"] = self.added_at
        return d


def _cache_path(index_url: str) -> Path:
    """Return a cache path isolated to one configured index URL."""
    digest = hashlib.sha256(index_url.encode("utf-8")).hexdigest()[:16]
    return get_runtime_home() / "cache" / f"plugin_index_{digest}.json"


def get_index_url() -> str | None:
    """Return the explicitly configured ``plugins.index_url``, if any."""
    try:
        from pcbdraft.model.configuration import cfg_get, load_config_readonly

        override = cfg_get(load_config_readonly(), "plugins", "index_url", default=None)
        if isinstance(override, str) and override.strip():
            return override.strip()
    except Exception:  # pragma: no cover - config loading must never break search
        logger.debug("plugin index: config override lookup failed", exc_info=True)
    return None


def _parse_entries(raw: Any) -> list[PluginIndexEntry]:
    """Parse a decoded index document into entries, skipping malformed items."""
    if isinstance(raw, dict):
        items = raw.get("plugins", [])
    elif isinstance(raw, list):  # bare-list form also accepted
        items = raw
    else:
        raise ValueError("Plugin index must be a JSON object or list.")
    if not isinstance(items, list):
        raise ValueError("Plugin index 'plugins' field must be a list.")

    entries: list[PluginIndexEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        repo = item.get("repo")
        if not isinstance(name, str) or not name.strip():
            continue
        if (
            not isinstance(repo, str)
            or repo.count("/") != 1
            or not all(repo.split("/"))
        ):
            logger.debug(
                "plugin index: skipping entry %r with invalid repo %r", name, repo
            )
            continue
        subdir = item.get("subdir")
        api_version = item.get("api_version")
        entries.append(
            PluginIndexEntry(
                name=name.strip(),
                description=str(item.get("description") or ""),
                author=str(item.get("author") or ""),
                tags=[
                    str(t) for t in item.get("tags") or [] if isinstance(t, (str, int))
                ],
                repo=repo.strip(),
                ref=str(item.get("ref") or ""),
                subdir=str(subdir).strip("/")
                if isinstance(subdir, str) and subdir.strip("/")
                else None,
                homepage=str(item["homepage"]) if item.get("homepage") else None,
                capabilities=[str(c) for c in item.get("capabilities") or []],
                api_version=int(api_version)
                if isinstance(api_version, (int, str)) and str(api_version).isdigit()
                else None,
                added_at=str(item["added_at"]) if item.get("added_at") else None,
            )
        )
    return entries


def _read_cache(
    index_url: str, *, max_age: float | None
) -> list[PluginIndexEntry] | None:
    """Return cached entries if the cache exists (and is younger than *max_age*)."""
    cache = _cache_path(index_url)
    try:
        if not cache.is_file():
            return None
        if max_age is not None:
            age = time.time() - cache.stat().st_mtime
            if age > max_age:
                return None
        return _parse_entries(json.loads(cache.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        logger.debug("plugin index: cache read failed: %s", exc)
        return None


def _write_cache(index_url: str, text: str) -> None:
    try:
        cache = _cache_path(index_url)
        cache.parent.mkdir(parents=True, exist_ok=True)
        from pcbdraft.core.runtime_utils import atomic_write_text

        atomic_write_text(cache, text)
    except OSError as exc:  # pragma: no cover - best effort
        logger.debug("plugin index: cache write failed: %s", exc)


def _fetch_remote(index_url: str) -> list[PluginIndexEntry] | None:
    """Fetch and parse the remote index; cache the raw payload on success."""
    try:
        import httpx

        resp = httpx.get(index_url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text
        if len(text.encode("utf-8", errors="ignore")) > _MAX_INDEX_BYTES:
            raise ValueError("Plugin index payload exceeds size limit.")
        entries = _parse_entries(json.loads(text))
        _write_cache(index_url, text)
        return entries
    except Exception as exc:
        logger.debug("plugin index: remote fetch failed (%s): %s", index_url, exc)
        return None


def load_index(
    *, refresh: bool = False, offline: bool = False
) -> tuple[list[PluginIndexEntry], str]:
    """Load the plugin index.

    Returns ``(entries, source)`` where *source* is one of ``"remote"``,
    ``"cache"``, or ``"none"``.

    A missing ``plugins.index_url`` returns ``([], "none")`` without reading
    cache or attempting network access. For a configured URL the order is:
    fresh cache (unless *refresh*) → remote → stale cache → empty list.
    ``offline=True`` skips the network entirely.
    """
    index_url = get_index_url()
    if index_url is None:
        return [], "none"

    if not refresh:
        cached = _read_cache(index_url, max_age=INDEX_CACHE_TTL)
        if cached is not None:
            return cached, "cache"

    if not offline:
        remote = _fetch_remote(index_url)
        if remote is not None:
            return remote, "remote"

    stale = _read_cache(index_url, max_age=None)
    if stale is not None:
        return stale, "cache"

    return [], "none"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _score_entry(entry: PluginIndexEntry, term: str) -> float:
    """Fuzzy relevance score for *entry* against lowercase *term* (0 = no match)."""
    import difflib

    name = entry.name.lower()
    desc = entry.description.lower()
    tags = [t.lower() for t in entry.tags]

    if term == name:
        return 100.0
    score = 0.0
    if term in name:
        score = max(score, 80.0)
    if any(term == t for t in tags):
        score = max(score, 70.0)
    if any(term in t for t in tags):
        score = max(score, 55.0)
    if term in desc:
        score = max(score, 50.0)
    if term in entry.author.lower():
        score = max(score, 40.0)
    # Fuzzy close-match on the name for typo tolerance.
    ratio = difflib.SequenceMatcher(None, term, name).ratio()
    if ratio >= 0.6:
        score = max(score, ratio * 60.0)
    return score


def search_index(
    entries: list[PluginIndexEntry], term: str, *, capability: str | None = None
) -> list[PluginIndexEntry]:
    """Rank *entries* against *term* (fuzzy on name/description/tags/author).

    An empty *term* matches everything (browse mode). ``capability`` filters
    entries by declared capability.
    """
    pool = entries
    if capability:
        cap = capability.lower()
        pool = [e for e in pool if any(cap == c.lower() for c in e.capabilities)]

    term = (term or "").strip().lower()
    if not term:
        return sorted(pool, key=lambda e: e.name)

    scored = [(e, _score_entry(e, term)) for e in pool]
    matched = [(e, s) for e, s in scored if s > 0]
    matched.sort(key=lambda pair: (-pair[1], pair[0].name))
    return [e for e, _s in matched]


def resolve_name(
    entries: list[PluginIndexEntry], name: str
) -> tuple[PluginIndexEntry | None, list[PluginIndexEntry]]:
    """Resolve a bare plugin *name* against the index.

    Returns ``(entry, candidates)``: an exact (case-insensitive) unique match
    in ``entry``, otherwise ``entry is None`` and ``candidates`` holds any
    partial matches (empty = nothing similar, >1 on exact = ambiguous).
    """
    lowered = name.strip().lower()
    exact = [e for e in entries if e.name.lower() == lowered]
    if len(exact) == 1:
        return exact[0], exact
    if len(exact) > 1:
        return None, exact
    partial = [e for e in entries if lowered in e.name.lower()]
    if len(partial) == 1:
        return partial[0], partial
    return None, partial
