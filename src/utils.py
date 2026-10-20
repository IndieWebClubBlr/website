"""
Utility functions for IWCB website generation.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import markdown
import pystache
import requests
from markdown.extensions.toc import TocExtension

from src import config

logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger("utils")

# Third-party libraries that log at DEBUG and would otherwise flood a
# verbose build: the markdown library logs every extension it loads, and
# urllib3/requests log every connection and retry.
for noisy_logger in ("MARKDOWN", "urllib3", "requests"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)


class SessionManager:
    """Thread-local HTTP session manager."""

    def __init__(self, headers: dict[str, str] | None = None):
        """Create a session manager with the given default headers.

        Args:
            headers: Extra headers sent with every request. The site
                User-Agent is always added on top of these.
        """
        self._sessions: dict[int, requests.Session] = {}
        self._headers: dict[str, str] = headers or {}
        self._headers.update({"User-Agent": config.UA})

    def get(self) -> requests.Session:
        """Get or create a thread-local requests session.

        Returns:
            The calling thread's session, created on first use.
        """
        thread_id = threading.get_ident()
        if thread_id not in self._sessions:
            session = requests.Session()
            session.headers.update(self._headers)
            self._sessions[thread_id] = session
        return self._sessions[thread_id]

    def close_all(self):
        """Close all thread-local sessions."""
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()


def read_template(file_name: str) -> str:
    """
    Read a template file.

    Args:
        file_name: Name of the template file to read.

    Returns:
        Template file contents as string.
    """
    try:
        template_path = Path("templates") / file_name
        with open(template_path) as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"Template file {file_name} not found.")
        raise


def save_html(content: str, output_file: str, output_dir: Path):
    """
    Save HTML content to a file.

    Args:
        content: HTML content to save.
        output_file: Output filename.
        output_dir: Directory where file should be written.
    """
    output_path = output_dir.joinpath(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _ = output_path.write_text(content, encoding="utf-8")
    logger.info(f"HTML file written to: {output_path}")


def make_renderer():
    """Create a Pystache renderer that searches the repository templates directory.

    Returns:
        Renderer configured to raise on missing template tags.
    """
    templates_dir = (Path(__file__).resolve().parent.parent / "templates").resolve()
    return pystache.Renderer(
        search_dirs=[str(templates_dir)],
        file_extension=False,
        missing_tags="strict",
    )


def render_and_save_html(html_content: str, page_url: str, output_dir: Path):
    """
    Render HTML content with default template and save to file.

    Args:
        html_content: The HTML content to render.
        page_url: The relative URL of the page without the initial slash.
        output_dir: Path where HTML file should be written.
    """
    try:
        now = datetime.now(timezone.utc)
        template_data = {
            "site_url": config.SITE_URL,
            "page_url": f"{config.SITE_URL}{page_url}",
            "generated_date": now.astimezone(config.EVENTS_TZ).strftime(
                "%d %b %Y, %I:%M %p IST"
            ),
            "content": html_content,
        }
        default_template = read_template("default.html")
        renderer = make_renderer()
        content = renderer.render(default_template, template_data)
        save_html(content, "index.html", output_dir)

    except Exception as e:
        logger.error(f"Failed to render and save HTML to {output_dir}/index.html: {e}")
        raise


def add_ref_param(url: str, ref: str = config.SITE_DOMAIN) -> str:
    """
    Add ref parameter to a URL, replacing any existing ref param.

    Operates on the raw query string rather than parsing and re-encoding
    it: a parse/encode round trip drops valueless params (`&preview`) and
    re-cases escapes (`%2f` -> `%2F`), which would make an archived link
    stop deduplicating against the member's own feed. Existing parameters
    are preserved byte for byte; only `ref=` entries are replaced.

    Args:
        url: URL to add the parameter to.
        ref: Value for the ref parameter.

    Returns:
        URL with the ref parameter set, replacing any existing value.
    """
    parts = urlsplit(url)
    kept = [
        pair for pair in parts.query.split("&") if pair and not pair.startswith("ref=")
    ]
    kept.append(f"ref={quote(ref, safe='')}")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(kept), parts.fragment)
    )


def markdown_to_html(markdown_file: Path) -> str:
    """
    Convert a Markdown file to HTML.

    Args:
        markdown_file: Path to the Markdown file.

    Returns:
        HTML string.
    """
    try:
        markdown_content = markdown_file.read_text(encoding="utf-8")
        return markdown.markdown(
            markdown_content,
            extensions=[
                "fenced_code",
                "admonition",
                "codehilite",
                "smarty",
                TocExtension(toc_depth="2-3"),
            ],
        )
    except Exception as e:
        logger.error(f"Failed to convert markdown from {markdown_file}: {e}")
        raise


def cache_path(url: str) -> Path:
    """Return the cache file path for a URL.

    Every URL-keyed cache (feeds, events) lives in `config.CACHE_DIR` and
    is named after the SHA256 hash of the URL, so all fetchers share one
    naming scheme.

    Args:
        url: The URL whose cache entry is needed.

    Returns:
        Path of the cache file for this URL.
    """
    return config.CACHE_DIR / hashlib.sha256(url.encode()).hexdigest()
