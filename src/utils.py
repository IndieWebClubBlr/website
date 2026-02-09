"""
Utility functions for IWCB website generation.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import markdown
import pystache
import requests

from src import config

logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)


class SessionManager:
    """Thread-local HTTP session manager."""

    def __init__(self, headers: dict[str, str] | None = None):
        self._sessions: dict[int, requests.Session] = {}
        self._headers: dict[str, str] = headers or {}
        self._headers.update({"User-Agent": config.UA})

    def get(self) -> requests.Session:
        """Get or create a thread-local requests session."""
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


def render_and_save_html(html_content: str, output_dir: Path):
    """
    Render HTML content with default template and save to file.

    Args:
        html_content: The HTML content to render.
        output_dir: Path where HTML file should be written.
    """
    try:
        now = datetime.now(timezone.utc)
        template_data = {
            "site_url": config.SITE_URL,
            "generated_date": now.astimezone(config.EVENTS_TZ).strftime(
                "%d %b %Y, %I:%M %p IST"
            ),
            "content": html_content,
        }
        default_template = read_template("default.html")
        renderer = pystache.Renderer()
        content = renderer.render(default_template, template_data)
        save_html(content, "index.html", output_dir)

    except Exception as e:
        logger.error(f"Failed to render and save HTML to {output_dir}/index.html: {e}")
        raise


def add_utm_params(url: str, medium: str, campaign: str) -> str:
    """
    Add UTM parameters to a URL, replacing any existing UTM parameters.

    Properly handles URLs that already have query parameters.
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    # Remove existing utm_ params
    params = {k: v for k, v in params.items() if not k.startswith("utm_")}
    # Add new utm params
    params["utm_source"] = ["blr.indiewebclub.org"]
    params["utm_medium"] = [medium]
    params["utm_campaign"] = [campaign]
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


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
            extensions=["fenced_code", "admonition", "codehilite", "smarty"],
        )
    except Exception as e:
        logger.error(f"Failed to convert markdown from {markdown_file}: {e}")
        raise
