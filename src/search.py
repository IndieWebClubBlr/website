"""Full-text search index generation using tinysearch.

Resolves post bodies (feed content when usable, otherwise a cached fetch of
the post's permalink), writes a tinysearch documents corpus, and compiles it
to a wasm engine that /search/ loads in the browser.

The whole feature is optional: when the search toolchain (the tinysearch
binary plus cargo with the wasm32-unknown-unknown target) is not installed,
the build still writes /search/ as a page that says search is unavailable,
so every link to it keeps working while the rest of the site builds.

Fetched pages are cleaned up with readability (Mozilla's article extraction
algorithm) so that navigation, sidebars and comment sections do not end up
in the index; a structural fallback covers pages readability cannot make
sense of.

The post-text cache (``.cache/search/``) is independent of the ``--cache``
and ``--cache-fallback`` flags: a successful fetch is reused forever and a
failure is retried only after ``SEARCH_FETCH_FAILURE_TTL_DAYS``, so once the
cache is warm every build mode leaves members' servers alone (a cold build
fetches the permalinks once). Cached texts carry the extractor that
produced them, so switching extractors invalidates the cache instead of
indexing stale text. The compiled engine is likewise kept as a last-good copy
(``.cache/last_tinysearch_engine.wasm``): a failing engine build (for
example a broken cargo setup) or a crates.io outage reuses it instead of
blocking the deploy.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
import snowballstemmer
from bs4 import BeautifulSoup

from src import config, ssrf
from src.feeds import FeedEntry
from src.utils import (
    SessionManager,
    has_noindex_directive,
    make_renderer,
    read_template,
    render_and_save_html,
    write_file,
)

logger = logging.getLogger("search")

# Readability logs an info message per page when its cleanup needs a retry;
# quieten it so the build log stays readable.
logging.getLogger("readability").setLevel(logging.WARNING)

# Chrome that should never be part of a post's searchable text.
_CHROME_TAGS = ("nav", "header", "footer", "aside", "form", "script", "style")

# Robots meta tags live in the document head. When a page is too large to
# index, inspect only a bounded prefix rather than the entire body.
_NOINDEX_PREFIX_BYTES = 64 * 1024

# Identifies how cached post texts were produced. Bump it when the
# extraction changes, so stale texts are refetched instead of indexed.
_EXTRACTOR = "readability-2"

# Identifies how the corpus is assembled from the entries. Bump it when
# that changes (the feed text, the summary line, ...), so the extracted
# corpus is rebuilt instead of reused.
_CORPUS_VERSION = "4"

# British spellings are normalized to their American counterparts before
# stemming, so a search for either form finds the same posts. The browser
# gets the same map inlined into the search page by ``_build_search_page``.
_SPELLING_PATH = Path(__file__).resolve().parents[1] / "assets" / "spelling.json"
_SPELLING_DATA = json.loads(_SPELLING_PATH.read_text(encoding="utf-8"))
_SPELLING_MAP: dict[str, str] = {
    str(gb): str(us) for gb, us in _SPELLING_DATA["map"].items()
}
_SPELLING_DIGEST = hashlib.sha256(
    json.dumps(_SPELLING_MAP, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

# Porter stemming is applied to the indexed text and, in
# ``assets/search.js``, to query terms as well; both sides must agree.
_STEMMER = snowballstemmer.stemmer("porter")
_TOKEN_RE = re.compile(r"(?:[^\W\d_]|')+")
_STEM_CACHE: dict[str, str] = {}


def _stem_token(token: str) -> str:
    """Stem one token; leave non-ASCII and letterless tokens unchanged."""
    if (
        not token.isascii()
        or len(token) < 2
        or not token[0].isalpha()
        or not token[-1].isalpha()
    ):
        return token
    lowered = token.lower()
    stemmed = _STEM_CACHE.get(lowered)
    if stemmed is None:
        stemmed = _STEMMER.stemWord(lowered)
        _STEM_CACHE[lowered] = stemmed
    return stemmed


def _stem_text(text: str) -> str:
    """Normalize text to lowercase, stemmed English terms."""
    stems = []
    for token in _TOKEN_RE.findall(text):
        cleaned = "".join(
            character for character in token if character.isalpha() or character == "'"
        )
        if cleaned:
            cleaned = _SPELLING_MAP.get(cleaned.lower(), cleaned)
            stems.append(_stem_token(cleaned))
    return " ".join(stems)


# Below this many characters, readability's output is treated as a miss and
# the structural fallback is used instead.
_MIN_READABLE_CHARS = 200

# A permalink fetch slower than this is worth a debug line of its own.
_SLOW_FETCH_SECONDS = 2.0

# How often the body resolution reports its progress.
_PROGRESS_EVERY = 200

# Name of the last-good compiled engine kept in config.CACHE_DIR.
_LAST_GOOD_WASM = "last_tinysearch_engine.wasm"

# Sidecar naming the corpus the last-good engine was built from, so that a
# fallback published from it keeps a meaningful name.
_LAST_GOOD_DIGEST = "last_tinysearch_engine.digest"

# Compiled engines kept per corpus digest, so a rebuild of an unchanged
# corpus deploys the identical bytes (and the browser keeps its cached
# copy of the engine).
_ENGINE_CACHE_DIR = "search_engines"
_ENGINE_CACHE_KEEP = 3

# Sidecar next to documents.json, recording the digest of the corpus inputs.
_CORPUS_META_FILE = "corpus.json"

# Where the generated engine crate is built. Keeping it out of a temporary
# directory lets cargo reuse the compiled dependencies between builds
# instead of recompiling them every time the corpus changes.
_CRATE_DIR_NAME = "search-engine"

_SEARCH_CACHE_DIR = "search"

# Written next to documents.json; tinysearch reads it from the working
# directory. The first indexed field doubles as the result title.
_TINYSEARCH_TOML = (
    "[schema]\n"
    'indexed_fields = ["title", "body"]\n'
    'metadata_fields = ["sub"]\n'
    'url_field = "url"\n'
)

# Ceiling on the wasm engine build: warm builds take ~15s, cold ones ~2min.
_ENGINE_TIMEOUT_SECONDS = 900


def _missing_toolchain() -> str | None:
    """Report why the search toolchain is unusable, if it is.

    Returns:
        A human-readable reason, or None when the toolchain is complete.
    """
    if shutil.which("tinysearch") is None:
        return "the tinysearch binary is not on PATH"
    if shutil.which("cargo") is None:
        return "cargo is not on PATH"
    rustc = shutil.which("rustc")
    if rustc is None:
        return "rustc is not on PATH"
    try:
        result = subprocess.run(
            [rustc, "--print", "sysroot"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        sysroot = Path(result.stdout.strip())
    except (OSError, subprocess.SubprocessError) as e:
        return f"rustc could not be queried ({e})"
    if not (sysroot / "lib" / "rustlib" / "wasm32-unknown-unknown" / "lib").is_dir():
        return "rustc has no wasm32-unknown-unknown standard library"
    return None


@dataclass(frozen=True)
class SearchIndexArtifact:
    """The compiled search engine written into the output directory."""

    wasm_filename: str
    doc_count: int
    from_cache: bool


def _collapse_ws(text: str) -> str:
    """Squeeze runs of whitespace down to single spaces."""
    return " ".join(text.split())


def _strip_chrome(soup: BeautifulSoup) -> None:
    """Remove navigation and other chrome from a parsed document.

    Args:
        soup: The parsed document, modified in place.
    """
    for tag in soup.find_all(_CHROME_TAGS):
        tag.decompose()


def _fallback_body_from_html(html: str) -> str:
    """Extract post text structurally, without readability.

    Prefers <article>, then <main>, then <body>, then the whole document,
    with navigation chrome removed.

    Args:
        html: Raw HTML of a page.

    Returns:
        Whitespace-collapsed plain text, "" for empty input.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    _strip_chrome(soup)
    root = soup.find("article") or soup.find("main") or soup.body or soup
    return _collapse_ws(root.get_text(separator=" ", strip=True))


def _text_from_html(html: str) -> str:
    """Extract plain text from HTML, keeping the words apart.

    ``get_text()`` without a separator runs the last word of one element
    into the first word of the next ("...mother.On life..."), which makes
    the second word unsearchable — the index matches word prefixes. A
    separator keeps them apart.

    Args:
        html: HTML to convert.

    Returns:
        Whitespace-collapsed plain text, "" for empty input.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return _collapse_ws(soup.get_text(" "))


def _readable_body_from_html(html: str) -> str:
    """Extract the article body of a page with readability.

    Args:
        html: Raw HTML of the post's permalink page.

    Returns:
        Whitespace-collapsed plain text of the extracted article, "" when
        readability cannot make sense of the page.
    """
    if not html:
        return ""
    try:
        # Imported here so that a venv without readability-lxml only costs
        # the search page, not the whole site build.
        from readability import Document

        summary = Document(html).summary()
    except Exception as e:  # noqa: BLE001 - readability raises many types
        logger.debug(f"Readability failed to parse page: {e}")
        return ""
    if not summary:
        return ""
    # Readability picks the article but can keep small link blocks (nav
    # menus) — drop the usual chrome tags from its output as well.
    soup = BeautifulSoup(summary, "html.parser")
    _strip_chrome(soup)
    return _collapse_ws(soup.get_text(" "))


def resolve_body_from_html(html: str) -> str:
    """Extract plain post text from a fetched page's HTML.

    Readability (Mozilla's article extraction) does the cleanup; pages it
    cannot handle fall back to a structural extraction.

    Args:
        html: Raw HTML of the post's permalink page.

    Returns:
        Whitespace-collapsed plain text, "" for empty input.
    """
    readable = _readable_body_from_html(html)
    if len(readable) >= _MIN_READABLE_CHARS:
        return readable
    fallback = _fallback_body_from_html(html)
    return fallback or readable


def _html_noindex(html: str) -> bool:
    """Whether a page's robots meta tags ask crawlers not to index it.

    ``name="robots"`` is the standard form; user-agent-specific tags such
    as ``name="googlebot"`` are honoured too, since a member opting out
    usually does not distinguish between crawlers.

    Args:
        html: Raw HTML of the page.

    Returns:
        True when a matching meta tag carries ``noindex`` or ``none``.
    """
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    for meta in soup.find_all("meta"):
        name = meta.get("name")
        content = meta.get("content")
        if not isinstance(name, str) or not isinstance(content, str):
            continue
        normalized = name.strip().lower()
        if normalized == "robots" or normalized.endswith("bot"):
            if has_noindex_directive(content):
                return True
    return False


def _search_cache_path(link: str) -> Path:
    """Return the cache file path for one post permalink."""
    digest = hashlib.sha256(link.encode()).hexdigest()
    return config.CACHE_DIR / _SEARCH_CACHE_DIR / digest


@dataclass(frozen=True)
class CachedPostText:
    """A post-text cache entry and its noindex state."""

    text: str | None
    fetched_at: datetime | None
    is_failure: bool
    attempts: int
    noindex: bool


def _read_search_cache(
    path: Path,
) -> CachedPostText:
    """Read one post-text cache entry.

    Entries written by a different extractor are ignored, so switching
    extractors refetches instead of indexing stale text.

    Args:
        path: Cache file path.

    Returns:
        The cached post text. text is None for failures and unreadable
        entries; fetched_at is None when no entry exists; attempts counts
        how often the fetch has been tried; noindex records whether the page
        asked not to be indexed.
    """
    if not path.exists():
        return CachedPostText(None, None, False, 0, False)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched_raw = data["fetched_at"]
        fetched_at = datetime.fromisoformat(fetched_raw)
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        attempts = int(data.get("attempts", 1))
    except (ValueError, KeyError, TypeError, OSError) as e:
        logger.debug(f"Ignoring unreadable post-text cache {path}: {e}")
        return CachedPostText(None, None, False, 0, False)
    if data.get("extractor") != _EXTRACTOR:
        logger.debug(f"Ignoring post-text cache {path}: written by another extractor")
        return CachedPostText(None, None, False, 0, False)
    noindex = bool(data.get("noindex", False))
    if data.get("error"):
        return CachedPostText(None, fetched_at, True, attempts, noindex)
    return CachedPostText(data.get("text") or "", fetched_at, False, attempts, noindex)


def _write_search_cache(
    path: Path, text: str | None, attempts: int, noindex: bool
) -> None:
    """Write one post-text cache entry, success or failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, str | bool] = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "extractor": _EXTRACTOR,
        "attempts": str(attempts),
        "noindex": noindex,
    }
    if text is None:
        payload["error"] = "fetch failed"
    else:
        payload["text"] = text
    write_file(path, json.dumps(payload))


def _decode_html(content: bytes, encoding: str | None) -> str:
    """Decode a page, falling back to UTF-8 for an unknown declared charset.

    The server controls the declared charset, so it cannot be trusted to be
    a codec Python knows: a page served as ``charset=utf8mb4`` would
    otherwise raise ``LookupError`` and take the whole search page down.

    Args:
        content: The page bytes.
        encoding: The charset the server declared, if any.

    Returns:
        The decoded page.
    """
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return content.decode(encoding or "utf-8", errors="ignore")
        except LookupError:
            return content.decode("utf-8", errors="ignore")


def _prefix_noindex(content: bytes, encoding: str | None) -> bool:
    """Check the head of a page too large to read for a robots meta tag."""
    return _html_noindex(_decode_html(content[:_NOINDEX_PREFIX_BYTES], encoding))


@dataclass(frozen=True)
class FetchedPostText:
    """A fetched post's text, noindex state, and whether the fetch completed."""

    text: str | None
    noindex: bool
    success: bool = False


def _fetch_post_text(link: str, session: requests.Session) -> FetchedPostText:
    """Fetch one permalink and extract its text.

    Args:
        link: Post permalink to fetch.
        session: HTTP session to fetch with.

    Returns:
        The extracted plain text (or None on any failure), whether the
        page asked not to be indexed through a header or meta tag, and
        whether the fetch completed successfully.
    """
    header_noindex = False
    try:
        response = ssrf.safe_get(
            session, link, timeout=config.REQUEST_TIMEOUT, stream=True
        )
        header_noindex = has_noindex_directive(response.headers.get("x-robots-tag"))
        response.raise_for_status()

        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > config.MAX_CONTENT_LENGTH
            except ValueError:
                # A malformed or duplicated header must not fail the build;
                # the size is enforced again while reading the body.
                too_large = False
            if too_large:
                logger.debug(f"Post too large ({content_length} bytes): {link}")
                prefix = b""
                for chunk in response.iter_content(chunk_size=8192):
                    prefix += chunk
                    if len(prefix) >= _NOINDEX_PREFIX_BYTES:
                        break
                noindex = header_noindex or _prefix_noindex(prefix, response.encoding)
                response.close()
                return FetchedPostText(None, noindex)

        content = b""
        for chunk in response.iter_content(chunk_size=8192):
            content += chunk
            if len(content) > config.MAX_CONTENT_LENGTH:
                logger.debug(f"Post content exceeded size limit: {link}")
                noindex = header_noindex or _prefix_noindex(content, response.encoding)
                response.close()
                return FetchedPostText(None, noindex)
    except ssrf.SSRFError as e:
        logger.warning(f"Refusing to fetch post text {link}: {e}")
        return FetchedPostText(None, header_noindex)
    except requests.RequestException as e:
        logger.debug(f"Failed to fetch post text {link}: {e}")
        return FetchedPostText(None, header_noindex)

    html = _decode_html(content, response.encoding)
    text = resolve_body_from_html(html)
    return FetchedPostText(
        text or None, header_noindex or _html_noindex(html), success=True
    )


def _search_cache_fresh(fetched_at: datetime | None) -> bool:
    """Whether a cache timestamp is within the failure retry window."""
    if fetched_at is None:
        return False
    age = datetime.now(UTC) - fetched_at
    return age < timedelta(days=config.SEARCH_FETCH_FAILURE_TTL_DAYS)


@dataclass(frozen=True)
class ResolvedBody:
    """One post's resolved search body and noindex state."""

    body: str
    noindex: bool


def _resolve_one_body(entry: FeedEntry, session: requests.Session) -> ResolvedBody:
    """Resolve one entry's search body and noindex state.

    Uses the feed-provided content when it is at least
    ``SEARCH_FEED_BODY_MIN_CHARS`` characters long and needs no fetch;
    otherwise fetches the permalink (cached, with a retry TTL for
    failures) and keeps whichever of feed content, fetched text, and
    summary is longest, capped at ``SEARCH_BODY_MAX_CHARS``. A cached
    fetch is discarded when the feed content is used instead.

    Args:
        entry: Feed entry to resolve a body for.
        session: HTTP session for permalink fetches.

    Returns:
        The resolved body and whether the permalink asked not to be
        indexed.
    """
    feed_text = _text_from_html(entry.content or "")
    cache_path = _search_cache_path(entry.link)
    if len(feed_text) >= config.SEARCH_FEED_BODY_MIN_CHARS:
        # The feed text supersedes the permalink. Drop any cached fetch so
        # a stale failure cannot keep forcing the corpus to be re-extracted
        # on every build: this path never refreshes its timestamp.
        if cache_path.exists():
            _ = cache_path.unlink()
        return ResolvedBody(feed_text[: config.SEARCH_BODY_MAX_CHARS], False)

    cached = _read_search_cache(cache_path)
    noindex = cached.noindex

    fetched_text: str | None = None
    if not cached.is_failure and cached.fetched_at is not None:
        fetched_text = cached.text
    elif cached.is_failure and _search_cache_fresh(cached.fetched_at):
        # Recent failure: do not hammer the server again this build.
        logger.debug(f"Skipping recently failed post text: {entry.link}")
    elif cached.is_failure and cached.attempts >= config.SEARCH_FETCH_MAX_ATTEMPTS:
        # A permalink that keeps failing is not going to start working;
        # stop asking so dead links are not retried forever.
        logger.debug(
            f"Giving up on post text {entry.link} after {cached.attempts} attempts"
        )
    else:
        started = time.monotonic()
        fetched = _fetch_post_text(entry.link, session)
        fetched_text = fetched.text
        noindex = fetched.noindex
        if not fetched.success:
            # A failed retry is not evidence that a previously observed
            # noindex directive has gone away.
            noindex = cached.noindex or noindex
        _write_search_cache(cache_path, fetched.text, cached.attempts + 1, noindex)
        elapsed = time.monotonic() - started
        if elapsed >= _SLOW_FETCH_SECONDS:
            logger.debug(
                f"Fetched post text in {elapsed:.2f}s "
                f"({len(fetched_text or '')} chars): {entry.link}"
            )

    summary_text = _normalize_summary(entry.summary or "")
    candidates = [feed_text, fetched_text or "", summary_text]
    best = max(candidates, key=len)
    return ResolvedBody(best[: config.SEARCH_BODY_MAX_CHARS], noindex)


def _peel_creator_prefix(text: str) -> str:
    """Remove leading ``@handle:`` creator prefixes.

    Prefixed summaries reach the corpus through the site's own archive
    feed, which carries the blogroll's creator prefix. Peeling it keeps
    the digest independent of whether a post arrived from a member feed
    or from the archive.

    Args:
        text: Raw summary text, possibly prefixed.

    Returns:
        The summary without creator prefixes.
    """
    return re.sub(r"^(?:@[^:]+:\s*)+", "", text)


def _normalize_summary(summary: str) -> str:
    """Peel creator prefixes and guillemets off a summary.

    The blogroll feed rules prepend a ``@handle:`` creator prefix to
    summaries in place, and the same post can arrive prefixed (from the
    site's own archive feed) or unprefixed (from the member feed) — the
    dedupe may keep either. Peeling both makes the text identical in every
    case, which keeps the indexed body *and* the corpus digest stable
    across builds.

    Args:
        summary: Raw summary text, possibly prefixed and wrapped.

    Returns:
        Plain summary text.
    """
    text = html.unescape(summary)
    while True:
        stripped = _peel_creator_prefix(text)
        if stripped != text:
            text = stripped
            continue
        if len(text) >= 2 and text.startswith("«") and text.endswith("»"):
            text = text[1:-1]
            continue
        break
    return text.strip()


def _interleave_by_host(links: list[str]) -> list[str]:
    """Order links so that consecutive ones come from different hosts.

    A plain shuffle still lets the busiest site show up several times in a
    row; taking one post per site in turn spreads the requests evenly over
    the members' sites, so the concurrent fetches rarely hit the same host.

    Args:
        links: The permalinks to order.

    Returns:
        The links, interleaved by host.
    """
    by_host: dict[str, list[str]] = {}
    for link in links:
        by_host.setdefault(urlparse(link).netloc, []).append(link)
    groups = list(by_host.values())
    for group in groups:
        random.shuffle(group)
    random.shuffle(groups)
    ordered: list[str] = []
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index < len(group):
                ordered.append(group[index])
    return ordered


@dataclass(frozen=True)
class ResolvedPost:
    """One resolved post in the concurrent body pass."""

    link: str
    body: str
    noindex: bool


@dataclass(frozen=True)
class ResolvedBodies:
    """Search bodies and the links whose pages asked not to be indexed."""

    bodies: dict[str, str]
    noindex_links: frozenset[str]


def resolve_post_bodies(entries: list[FeedEntry]) -> ResolvedBodies:
    """Resolve search bodies for all entries concurrently.

    Args:
        entries: All corpus entries; duplicate links are resolved once.

    Returns:
        The plain-text bodies by post link, and the set of links whose
        permalink asked not to be indexed. Entries that could not be
        resolved map to "".
    """
    unique: dict[str, FeedEntry] = {}
    for entry in entries:
        _ = unique.setdefault(entry.link, entry)

    session_manager = SessionManager()
    # Interleave by host before fetching: in corpus order the posts of one
    # site come first, so the workers would hit that site in a burst.
    links = _interleave_by_host(list(unique))
    logger.info(f"Resolving search bodies for {len(links)} posts")

    def resolve(link: str) -> ResolvedPost:
        try:
            resolved = _resolve_one_body(unique[link], session_manager.get())
            return ResolvedPost(link, resolved.body, resolved.noindex)
        except Exception as e:  # noqa: BLE001 - one bad post must not skip search
            logger.warning(f"Failed to resolve post text {link}: {e}")
            return ResolvedPost(link, "", False)

    bodies: dict[str, str] = {}
    noindex_links: set[str] = set()
    try:
        with ThreadPoolExecutor(
            max_workers=config.MAX_WORKERS,
            thread_name_prefix="SearchFetcher",
        ) as executor:
            started = time.monotonic()
            for index, post in enumerate(executor.map(resolve, links), 1):
                bodies[post.link] = post.body
                if post.noindex:
                    noindex_links.add(post.link)
                if index % _PROGRESS_EVERY == 0:
                    logger.info(
                        f"Resolved {index}/{len(links)} bodies in {time.monotonic() - started:.1f}s"
                    )
    finally:
        session_manager.close_all()

    fetched = sum(1 for b in bodies.values() if b)
    logger.info(
        f"Resolved bodies for {fetched}/{len(links)} posts"
        f" ({len(noindex_links)} noindex)"
    )
    return ResolvedBodies(bodies, frozenset(noindex_links))


def _unescape(text: str) -> str:
    """Decode HTML entities that survived the feed's CDATA wrapping."""
    return html.unescape(text)


def _entry_sub(entry: FeedEntry) -> str:
    """Build the display/metadata line: author, date, and tags."""
    date = entry.published.astimezone(config.EVENTS_TZ).date().isoformat()
    sub = f"{entry.feed_title} · {date}"
    tags = [_unescape(tag) for tag in entry.tags if tag]
    if tags:
        sub += " · " + ", ".join(tags[: config.MAX_SHOWN_TAGS])
    return sub


def write_documents(
    entries: list[FeedEntry],
    bodies: dict[str, str],
    noindex_links: Collection[str],
    docs_dir: Path,
) -> int:
    """Write tinysearch's documents.json and tinysearch.toml.

    Args:
        entries: All corpus entries, newest first; first link wins.
        bodies: Resolved plain-text bodies by link.
        noindex_links: Links whose permalink asked not to be indexed.
        docs_dir: Directory to write the corpus into.

    Returns:
        Number of documents written.
    """
    excluded = set(noindex_links)
    docs: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.link in seen or entry.link in excluded:
            continue
        seen.add(entry.link)
        title = _unescape(entry.title)
        sub = _entry_sub(entry)
        body = _collapse_ws(bodies.get(entry.link, ""))[: config.SEARCH_BODY_MAX_CHARS]
        docs.append(
            {
                "title": title,
                "url": entry.link,
                "body": _stem_text(f"{title} {sub} {body}"),
                "sub": sub,
            }
        )

    docs_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    documents_path = docs_dir / "documents.json"
    write_file(documents_path, json.dumps(docs, ensure_ascii=False))
    write_file(docs_dir / "tinysearch.toml", _TINYSEARCH_TOML)
    logger.info(
        f"Wrote {len(docs)} search documents to {documents_path} "
        f"in {time.monotonic() - started:.2f}s"
    )
    return len(docs)


def _gzip_bytes(data: bytes) -> bytes:
    """Compress bytes with gzip, deterministically.

    The gzip header carries a timestamp; zeroing it keeps the output
    byte-identical for identical input, so the compressed artifact does not
    change between builds of the same engine.

    Args:
        data: Bytes to compress.

    Returns:
        The gzip-compressed bytes.
    """
    buffer = io.BytesIO()
    with gzip.GzipFile(
        fileobj=buffer, mode="wb", compresslevel=9, mtime=0
    ) as compressed:
        _ = compressed.write(data)
    return buffer.getvalue()


def _last_good_digest() -> str:
    """Return the corpus digest the last-good engine was built from.

    Returns:
        The digest, or "unknown" when the sidecar is missing.
    """
    sidecar = config.CACHE_DIR / _LAST_GOOD_DIGEST
    if not sidecar.exists():
        return "unknown"
    try:
        return sidecar.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _newest_cached_engine(engine_cache_dir: Path) -> Path | None:
    """Return the most recently built cached engine, if any.

    Args:
        engine_cache_dir: Directory holding the per-corpus engine copies.

    Returns:
        Path of the newest cached engine, or None when the cache is empty.
    """
    engines = sorted(
        engine_cache_dir.glob("*.wasm"), key=lambda path: path.stat().st_mtime
    )
    return engines[-1] if engines else None


def _publish_previous_engine(
    engine_cache_dir: Path, output_dir: Path, current_digest: str
) -> str | None:
    """Copy the previous engine generation into the output directory.

    A browser can still hold the /search/ page from an earlier deploy, and
    that page points at the engine of that deploy. The CI build starts from
    an empty output directory, so the previous generation is copied back in
    from the engine cache; without it those pages would fail to load their
    engine until the page itself is refetched.

    Args:
        engine_cache_dir: Directory holding the cached engines.
        output_dir: Site output directory.
        current_digest: Digest of the engine that was just written.

    Returns:
        The file name of the engine that was copied, or None when there is
        no previous generation.
    """
    candidates = [
        path for path in engine_cache_dir.glob("*.wasm") if path.stem != current_digest
    ]
    if not candidates:
        return None
    previous = max(candidates, key=lambda path: path.stat().st_mtime)
    wasm_bytes = previous.read_bytes()
    name = f"tinysearch_engine.{previous.stem}.wasm"
    write_file(output_dir / name, wasm_bytes)
    write_file(output_dir / f"{name}.gz", _gzip_bytes(wasm_bytes))
    return name


def _prune_old_engines(directory: Path, pattern: str, keep: int) -> None:
    """Delete engine generations older than the newest ``keep`` ones.

    The previous generation is kept in the site output because browsers may
    still hold the cached /search/ page that points at it; deleting it
    would break their search until they reload the page.

    Args:
        directory: Directory holding the engine files.
        pattern: Glob pattern matching the engine files.
        keep: Number of generations to keep.
    """
    generations: dict[str, float] = {}
    for path in directory.glob(pattern):
        key = path.name.removesuffix(".gz")
        generations[key] = max(generations.get(key, 0.0), path.stat().st_mtime)
    ordered = sorted(generations.items(), key=lambda item: item[1], reverse=True)
    for key, _ in ordered[keep:]:
        for path in directory.glob(key + "*"):
            _ = path.unlink()


def _engine_cache_digest(docs_dir: Path, optimized: bool) -> str:
    """Hash the inputs the engine is built from.

    The optimization flag is part of the key: an engine built without
    wasm-opt has different bytes, so it must not be served (or reused) by
    a build that can optimize.

    Args:
        docs_dir: Directory holding ``documents.json`` and ``tinysearch.toml``.
        optimized: Whether wasm-opt is available for this build.

    Returns:
        A short digest identifying this engine's inputs.
    """
    return hashlib.sha256(
        (docs_dir / "documents.json").read_bytes()
        + b"\x00"
        + (docs_dir / "tinysearch.toml").read_bytes()
        + b"\x00"
        + (b"wasm-opt" if optimized else b"no-wasm-opt")
    ).hexdigest()[:16]


def run_tinysearch(
    docs_dir: Path, output_dir: Path, doc_count: int
) -> SearchIndexArtifact:
    """Compile the documents corpus into a hashed wasm engine.

    The file name is hashed from the indexed corpus (documents.json), the
    schema, and whether wasm-opt is available, not from the engine bytes:
    those differ on every build even when the corpus does not, because the
    indexer iterates a hash map whose order changes from run to run.
    Hashing the inputs keeps the URL stable across rebuilds that did not
    change what is searchable, so browsers can cache the engine, and
    changes it exactly when the index (or its optimization) changes.

    On success the engine is refreshed as the last-good copy in
    ``config.CACHE_DIR``; on failure that copy is reused so a broken
    toolchain or a crates.io outage cannot block the build. With neither a
    successful compile nor a last-good copy, this raises.

    Args:
        docs_dir: Directory holding documents.json and tinysearch.toml.
        output_dir: Site output directory for the hashed wasm file.
        doc_count: Number of documents in the corpus (for reporting).

    Returns:
        Artifact describing the written engine file.

    Raises:
        RuntimeError: When the compile fails and no last-good engine exists.
    """
    # Paths must be absolute: the CLI runs with cwd=docs_dir, so a relative
    # -p would resolve inside docs_dir instead of next to it.
    docs_dir = docs_dir.resolve()
    output_dir = output_dir.resolve()
    out_dir = docs_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    wasm_path = out_dir / "tinysearch_engine.wasm"
    last_good_path = config.CACHE_DIR / _LAST_GOOD_WASM
    engine_cache_dir = config.CACHE_DIR / _ENGINE_CACHE_DIR

    # Build the generated engine crate in a stable place, so cargo can reuse
    # the compiled dependencies instead of rebuilding them on every build
    # that changes the corpus. It lives under CARGO_HOME, which is already
    # the cached, gitignored home of the crates.io downloads.
    cargo_home = Path(os.environ.get("CARGO_HOME", str(config.CACHE_DIR / "cargo")))
    crate_dir = cargo_home.expanduser() / _CRATE_DIR_NAME
    crate_dir.mkdir(parents=True, exist_ok=True)

    # The engine's bytes differ on every build even when the corpus does
    # not: the indexer iterates a hash map, whose order changes from run
    # to run. Hashing the corpus gives the file name (and with it the URL) a
    # meaning: it changes exactly when the searchable content changes. The
    # schema and the availability of wasm-opt are part of the key too: both
    # change the engine's bytes, so a cached engine built under different
    # conditions must not be reused.
    optimized = shutil.which("wasm-opt") is not None
    documents_digest = _engine_cache_digest(docs_dir, optimized)

    cached_engine = engine_cache_dir / f"{documents_digest}.wasm"
    # The digest of the corpus the published engine was built from; a
    # fallback engine keeps the digest of its own (older) corpus.
    engine_digest = documents_digest
    if cached_engine.exists():
        # Same corpus as a previous build: reuse the exact same bytes, so
        # the deployed file (and its ETag) does not change and browsers
        # keep their cached copy.
        wasm_bytes = cached_engine.read_bytes()
        from_cache = True
        logger.info(f"Reusing cached search engine {cached_engine}")
    else:
        cmd = [
            "tinysearch",
            "--release",
            "-m",
            "wasm",
            *(["-o"] if optimized else []),
            "-p",
            str(out_dir),
            "--crate-path",
            str(crate_dir),
            "documents.json",
        ]
        logger.info(
            "Compiling the search engine with tinysearch"
            + (
                " and optimizing it with wasm-opt"
                if optimized
                else " (wasm-opt not found; skipping the optional optimization)"
            )
        )
        logger.debug(f"Running tinysearch: {' '.join(cmd)}")
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=docs_dir,
                capture_output=True,
                text=True,
                timeout=_ENGINE_TIMEOUT_SECONDS,
            )
            if proc.returncode != 0 or not wasm_path.exists():
                detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
                raise RuntimeError(f"tinysearch exited {proc.returncode}: {detail}")
            logger.debug(
                f"tinysearch finished in {time.monotonic() - started:.2f}s: "
                f"{wasm_path.stat().st_size} bytes"
            )
            wasm_bytes = wasm_path.read_bytes()
            _ = last_good_path.parent.mkdir(parents=True, exist_ok=True)
            write_file(last_good_path, wasm_bytes)
            # Remember which corpus it was built from.
            write_file(
                config.CACHE_DIR / _LAST_GOOD_DIGEST,
                documents_digest,
            )
            from_cache = False
        except (
            FileNotFoundError,
            OSError,
            RuntimeError,
            subprocess.TimeoutExpired,
        ) as e:
            fallback = _newest_cached_engine(engine_cache_dir)
            if fallback is None and last_good_path.exists():
                fallback = last_good_path
            if fallback is None:
                detail = (
                    "tinysearch engine build failed and no cached engine exists "
                    f"({e}). The search toolchain is missing: it needs the "
                    "tinysearch binary and cargo with the wasm32-unknown-unknown "
                    "target (see the README for the nix and non-nix setup), or "
                    "restore .cache/last_tinysearch_engine.wasm."
                )
                raise RuntimeError(detail) from e
            logger.warning(
                f"tinysearch engine build failed ({e}); reusing cached engine {fallback}"
            )
            wasm_bytes = fallback.read_bytes()
            from_cache = True
            # A fallback engine was built for an older corpus, so it keeps
            # its own name: the file name identifies the corpus the engine
            # was built from. The last-good copy carries that corpus in a
            # sidecar, because its own name says nothing about it.
            engine_digest = (
                fallback.stem if fallback != last_good_path else _last_good_digest()
            )

        # Remember the engine for this corpus: the next build of the same
        # corpus then deploys the identical bytes. A fallback engine is not
        # saved here: it was built for an older corpus.
        if not from_cache:
            _ = engine_cache_dir.mkdir(parents=True, exist_ok=True)
            write_file(engine_cache_dir / f"{documents_digest}.wasm", wasm_bytes)
            _prune_old_engines(engine_cache_dir, "*.wasm", keep=_ENGINE_CACHE_KEEP)

    # The file name carries the digest of the corpus the engine was built
    # from, so the URL always identifies the corpus it searches.
    wasm_filename = f"tinysearch_engine.{engine_digest}.wasm"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_file(output_dir / wasm_filename, wasm_bytes)
    # GitHub Pages' CDN serves a pre-compressed sibling (.gz/.br) when one
    # exists next to the original, with the right Content-Encoding header.
    # wasm is not in the on-the-fly gzip types, so ship the .gz ourselves.
    started = time.monotonic()
    gz_bytes = _gzip_bytes(wasm_bytes)
    logger.debug(f"Gzipped the engine in {time.monotonic() - started:.2f}s")
    write_file(output_dir / f"{wasm_filename}.gz", gz_bytes)
    previous_engine = _publish_previous_engine(
        engine_cache_dir, output_dir, engine_digest
    )
    _prune_old_engines(output_dir, "tinysearch_engine.*.wasm*", keep=2)

    logger.info(
        f"Search engine {'reused from cache' if from_cache else 'built'}: "
        + f"{wasm_filename} ({len(wasm_bytes)} bytes, {len(gz_bytes)} bytes gzipped, "
        + f"{doc_count} documents)"
    )
    if previous_engine:
        logger.info(
            "Also kept the previous engine for browsers with a cached "
            f"/search/ page: {previous_engine}"
        )
    return SearchIndexArtifact(
        wasm_filename=wasm_filename,
        doc_count=doc_count,
        from_cache=from_cache,
    )


def _failed_search_links() -> list[str]:
    """List the permalinks whose post text could not be fetched.

    Only failures can change the resolved bodies: a successful fetch is
    never repeated, so its text is stable. A failure that later succeeds
    (after the retry window) does change the body, and that is what this
    list captures.

    Returns:
        The cache file names of the permalinks whose fetch failed. The name
        is the SHA256 digest of the link, which is all the digest needs.
    """
    failed: list[str] = []
    cache_dir = config.CACHE_DIR / _SEARCH_CACHE_DIR
    if not cache_dir.is_dir():
        return failed
    for path in cache_dir.iterdir():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("error"):
            failed.append(path.name)
    return sorted(failed)


def _cached_noindex_links(corpus_names: set[str]) -> list[str]:
    """List corpus permalinks whose cached page asked not to be indexed.

    The noindex state is part of the corpus: adding or removing a page from
    the index has to invalidate the extracted corpus even when the feed
    entries themselves did not change.

    Args:
        corpus_names: Cache file names of the links in the current corpus.

    Returns:
        The cache file names of the current-extractor entries marked noindex.
    """
    noindex: list[str] = []
    cache_dir = config.CACHE_DIR / _SEARCH_CACHE_DIR
    if not cache_dir.is_dir():
        return noindex
    for path in cache_dir.iterdir():
        if not path.is_file() or path.name not in corpus_names:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("extractor") != _EXTRACTOR:
            continue
        if data.get("noindex"):
            noindex.append(path.name)
    return sorted(noindex)


def _cached_noindex_urls(entries: list[FeedEntry]) -> list[str]:
    """List corpus URLs whose cached permalink asked not to be indexed.

    Args:
        entries: The current search corpus entries.

    Returns:
        Sorted post URLs marked noindex by the current extractor's cache.
    """
    links_by_name = {
        _search_cache_path(entry.link).name: entry.link for entry in entries
    }
    noindex_names = _cached_noindex_links(set(links_by_name))
    return sorted(links_by_name[name] for name in noindex_names)


def _stale_search_failures(corpus_names: set[str]) -> list[str]:
    """List the failed permalinks that are due for another attempt.

    A failure is retried once its retry window has passed, but only while
    it has attempts left. The corpus is only re-extracted when its digest
    changes, so without this list a failed permalink would stay unindexed
    until an unrelated post changed the corpus.

    Failures for links that are no longer in the corpus are ignored: they
    are never retried, so their timestamp never advances and they would
    otherwise force the corpus to be re-extracted on every build.

    Args:
        corpus_names: Cache file names of the links in the current corpus.

    Returns:
        The cache file names of the failures that are due for a retry.
    """
    stale: list[str] = []
    cache_dir = config.CACHE_DIR / _SEARCH_CACHE_DIR
    if not cache_dir.is_dir():
        return stale
    for path in cache_dir.iterdir():
        if not path.is_file() or path.name not in corpus_names:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not data.get("error"):
            continue
        try:
            fetched_at = datetime.fromisoformat(data["fetched_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        try:
            attempts = int(data.get("attempts", 1))
        except (TypeError, ValueError):
            attempts = 1
        if attempts >= config.SEARCH_FETCH_MAX_ATTEMPTS:
            continue
        if not _search_cache_fresh(fetched_at):
            stale.append(path.name)
    return sorted(stale)


def corpus_digest(entries: list[FeedEntry]) -> str:
    """Hash the inputs the indexed corpus is derived from.

    The digest is deliberately cheap: it hashes the raw feed content and
    the summaries rather than the extracted text, so an unchanged corpus can
    be reused without re-extracting anything. Everything that can change
    the index is included — the entries, the permalinks whose text could
    not be fetched, and the settings that shape the bodies.

    Args:
        entries: The corpus entries, in the order they are indexed.

    Returns:
        A digest of the corpus inputs.
    """
    started = time.monotonic()
    failed = _failed_search_links()
    corpus_names = {_search_cache_path(entry.link).name for entry in entries}
    noindex = _cached_noindex_links(corpus_names)
    parts = [
        _EXTRACTOR,
        _CORPUS_VERSION,
        "spelling:" + _SPELLING_DIGEST,
        str(config.SEARCH_BODY_MAX_CHARS),
        str(config.SEARCH_FEED_BODY_MIN_CHARS),
        "failed:" + ",".join(failed),
        "noindex:" + ",".join(noindex),
    ]
    for entry in entries:
        parts.append(
            "\x00".join(
                [
                    entry.link,
                    # The title and the publication date reach the index
                    # (the title as the document title, the date in the
                    # metadata line), so they have to be part of the digest.
                    entry.title,
                    entry.published.isoformat(),
                    entry.feed_title,
                    entry.feed_url,
                    ",".join(entry.tags),
                    hashlib.sha256(
                        (entry.content or "").encode("utf-8", "replace")
                    ).hexdigest(),
                    hashlib.sha256(
                        _normalize_summary(entry.summary or "").encode(
                            "utf-8", "replace"
                        )
                    ).hexdigest(),
                ]
            )
        )
    digest = hashlib.sha256("\x01".join(parts).encode("utf-8", "replace")).hexdigest()[
        :32
    ]
    logger.debug(
        f"Corpus digest {digest} over {len(entries)} entries, "
        f"{len(failed)} failed permalinks, in {time.monotonic() - started:.2f}s"
    )
    return digest


def _read_corpus_meta(docs_dir: Path) -> dict[str, str]:
    """Read the corpus metadata sidecar.

    Args:
        docs_dir: Directory holding the corpus.

    Returns:
        The stored metadata, or an empty mapping when there is none.
    """
    path = docs_dir / _CORPUS_META_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug(f"Ignoring unreadable corpus metadata {path}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _write_corpus_meta(docs_dir: Path, digest: str, doc_count: int) -> None:
    """Write the corpus metadata sidecar.

    Args:
        docs_dir: Directory holding the corpus.
        digest: Digest of the corpus inputs.
        doc_count: Number of documents in the corpus.
    """
    write_file(
        docs_dir / _CORPUS_META_FILE,
        json.dumps({"digest": digest, "doc_count": doc_count}),
    )


def generate_search_page(
    entries: list[FeedEntry],
    output_dir: Path,
    excluded_feed_urls: Collection[str] = (),
) -> str:
    """Build the search index and write the /search/ page.

    This is the entry point the site generator uses: it resolves the post
    bodies, compiles the tinysearch engine and renders the page. The search
    feature is optional — without the toolchain, or when the engine cannot
    be built, a fallback page is written instead. Nothing that goes wrong
    here may fail the build, so every error ends the search page and no more.

    Args:
        entries: The corpus entries to index.
        output_dir: Site output directory.
        excluded_feed_urls: Feed URLs whose entries must stay out of the
            search index. They are still rendered everywhere else.

    Returns:
        The URL of the page relative to the site root ("search/").
    """
    try:
        page_url = _build_search_page(entries, output_dir, excluded_feed_urls)
    except Exception as e:  # noqa: BLE001 - the search page is optional
        logger.warning(f"Could not build the search page: {e}")
        page_url = None

    if page_url is None:
        _render_unavailable_search_page(output_dir)
        return "search/"
    return page_url


def _render_unavailable_search_page(output_dir: Path) -> None:
    """Write /search/ when no search engine could be built.

    Args:
        output_dir: Site output directory.
    """
    content = make_renderer().render(
        read_template("search.html"),
        {"available": False},
    )
    render_and_save_html(
        html_content=content,
        page_url="search/",
        output_dir=output_dir / "search",
    )


def _build_search_page(
    entries: list[FeedEntry],
    output_dir: Path,
    excluded_feed_urls: Collection[str] = (),
) -> str | None:
    """Build the search index and write the /search/ page.

    Args:
        entries: The corpus entries to index.
        output_dir: Site output directory.
        excluded_feed_urls: Feed URLs whose entries must stay out of the
            search index.

    Returns:
        The URL of the page relative to the site root ("search/"), or None
        when no engine could be built.
    """
    reason = _missing_toolchain()
    if reason:
        logger.warning(
            f"Search is unavailable: {reason}. Rendering an empty search page. "
            "See the README for how to install the search toolchain."
        )
        return None

    excluded = set(excluded_feed_urls)
    for feed_url in excluded:
        logger.debug(f"Noindex feed excluded from search: {feed_url}")

    if excluded:
        filtered: list[FeedEntry] = []
        for entry in entries:
            if entry.feed_url in excluded:
                continue
            filtered.append(entry)
        logger.info(
            f"Excluded {len(entries) - len(filtered)} noindex entries from search"
        )
        entries = filtered

    docs_dir = config.CACHE_DIR / "search_docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    digest = corpus_digest(entries)
    logger.info(f"Corpus digest: {digest}")
    meta = _read_corpus_meta(docs_dir)
    stale = _stale_search_failures(
        {_search_cache_path(entry.link).name for entry in entries}
    )
    if stale:
        logger.info(f"{len(stale)} failed permalinks are due for a retry")
    if (
        meta.get("digest") == digest
        and not stale
        and (docs_dir / "documents.json").exists()
    ):
        # Nothing searchable changed: reuse the extracted corpus instead of
        # re-parsing every feed's content.
        doc_count = int(meta.get("doc_count", 0))
        logger.info(f"Corpus unchanged ({doc_count} documents), reusing it")
        page_noindex_urls = _cached_noindex_urls(entries)
    else:
        resolved = resolve_post_bodies(entries)
        doc_count = write_documents(
            entries, resolved.bodies, resolved.noindex_links, docs_dir
        )
        page_noindex_urls = sorted(resolved.noindex_links)
        # The permalinks that failed to fetch are part of the corpus, so the
        # digest is computed after the bodies are resolved: the next build
        # then finds this corpus instead of extracting it again.
        _write_corpus_meta(docs_dir, corpus_digest(entries), doc_count)

    for link in page_noindex_urls:
        logger.debug(f"Noindex page excluded from search: {link}")
    logger.info(f"Excluded {len(page_noindex_urls)} noindex pages from search")

    # The schema shapes the index, so write it even when the corpus was
    # reused: a changed schema has to reach the engine build.
    write_file(docs_dir / "tinysearch.toml", _TINYSEARCH_TOML)

    try:
        artifact = run_tinysearch(docs_dir, output_dir, doc_count)
    except RuntimeError as e:
        # The search page is optional: a failed engine build must not take
        # the rest of the site down with it.
        logger.warning(f"Search engine build failed: {e}")
        return None

    content = make_renderer().render(
        read_template("search.html"),
        {
            "site_url": config.SITE_URL,
            "available": True,
            "wasm_file": artifact.wasm_filename,
            "ref": config.SITE_DOMAIN,
            "spelling_map_json": json.dumps(
                _SPELLING_MAP, sort_keys=True, separators=(",", ":")
            ),
        },
    )
    render_and_save_html(
        html_content=content,
        page_url="search/",
        output_dir=output_dir / "search",
    )
    return "search/"
