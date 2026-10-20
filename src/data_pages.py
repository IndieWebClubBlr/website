"""Data-driven pages (topics, prompts): YAML models, validation, and rendering.

Each page's YAML file is validated into a frozen pydantic model; invalid files
raise ValidationError with path-style locations and fail the build. Templates
consume the models directly (Pystache resolves fields and properties as
attributes).
"""

from __future__ import annotations

import html
import logging
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol

import markdown
import yaml
from feedgen.feed import FeedGenerator
from markdown.extensions.toc import slugify
from pydantic import BaseModel, ConfigDict, Field

from src import config
from src.utils import make_renderer, read_template

logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger("data_pages")

# Same extensions as src.utils.markdown_to_html, minus the table of contents.
_MARKDOWN_EXTENSIONS = ["fenced_code", "admonition", "codehilite", "smarty"]


def _markdown_block(text: str) -> str:
    """Render markdown text to a block-level HTML fragment."""
    return markdown.markdown(text, extensions=_MARKDOWN_EXTENSIONS)


def _markdown_inline(text: str) -> str:
    """Render single-paragraph markdown to inline HTML (no <p> wrapper)."""
    html_out = _markdown_block(text)
    if (
        html_out.startswith("<p>")
        and html_out.endswith("</p>")
        and html_out.count("<p>") == 1
        and html_out.count("</p>") == 1
    ):
        return html_out[3:-4]
    return html_out


def _date_display(value: date) -> str:
    """Format a date as e.g. "October 3, 2026"."""
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _section_slug(display: str) -> str:
    """Slugify a date display string using the same rules as the TOC extension."""
    return slugify(display, "-")


class PromptItem(BaseModel):
    """One prompt bullet, rendered to inline HTML."""

    model_config = ConfigDict(frozen=True)

    text: str

    @property
    def html(self) -> str:
        return _markdown_inline(self.text)


class TopicsSection(BaseModel):
    """One dated section of the topics page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    title: str
    topic: str

    @property
    def date_display(self) -> str:
        return _date_display(self.date)

    @property
    def slug(self) -> str:
        return _section_slug(self.date_display)

    @property
    def topic_html(self) -> str:
        return _markdown_block(self.topic)

    @property
    def feed_title(self) -> str:
        """Plain-text entry title: the title if set, else the date."""
        return self.title if self.title else self.date_display

    def content_html(self) -> str:
        """Feed-entry HTML; the rendered page gets its markup from the template."""
        title_html = f"<p>{html.escape(self.title)}</p>" if self.title else ""
        return f"{title_html}{self.topic_html}"


class TopicsPage(BaseModel):
    """The topics data page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sections: list[TopicsSection] = Field(min_length=1)


class PromptsSection(BaseModel):
    """One dated section of the prompts page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    prompts: list[str]

    @property
    def date_display(self) -> str:
        return _date_display(self.date)

    @property
    def slug(self) -> str:
        return _section_slug(self.date_display)

    @property
    def has_prompts(self) -> bool:
        return bool(self.prompts)

    @property
    def items(self) -> list[PromptItem]:
        return [PromptItem(text=text) for text in self.prompts]

    @property
    def feed_title(self) -> str:
        return self.date_display

    def content_html(self) -> str:
        """Feed-entry HTML; the rendered page gets its markup from the template."""
        if not self.prompts:
            return ""
        items = "".join(f"<li>{_markdown_inline(text)}</li>" for text in self.prompts)
        return f"<ul>{items}</ul>"


class PromptsPage(BaseModel):
    """The prompts data page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sections: list[PromptsSection] = Field(min_length=1)


class DataPageSection(Protocol):
    """Sections that can appear in a data page's Atom feed."""

    date: date

    @property
    def feed_title(self) -> str: ...

    def content_html(self) -> str: ...


def load_data_page(
    yaml_file: Path, page_cls: type[TopicsPage | PromptsPage]
) -> TopicsPage | PromptsPage:
    """Load and validate one data page's YAML file, sections newest first."""
    data: Any = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    page = page_cls.model_validate(data)
    return page.model_copy(
        update={"sections": sorted(page.sections, key=lambda s: s.date, reverse=True)}
    )


class _PageContext:
    """Template context for a data page: page attributes plus site-wide values.

    Data-page templates render directly against their pydantic model, but may
    also reference site-wide values such as site_url (strict Pystache raises on
    missing keys). Unknown attributes delegate to the page model.
    """

    site_url: ClassVar[str] = config.SITE_URL

    def __init__(self, page: TopicsPage | PromptsPage) -> None:
        self._page = page

    def __getattr__(self, name: str) -> Any:
        return getattr(self._page, name)


def render_data_page(page: TopicsPage | PromptsPage, template_file: str) -> str:
    """Render a validated data page to HTML.

    Args:
        page: Validated data page model.
        template_file: Template name in the templates directory.

    Returns:
        Rendered HTML content for `render_and_save_html`.
    """
    return make_renderer().render(read_template(template_file), _PageContext(page))


def generate_data_page_feed(
    page: TopicsPage | PromptsPage,
    feed_title: str,
    feed_subtitle: str,
    output_path: Path,
    page_url: str,
) -> None:
    """Create an Atom feed of one data page's sections.

    Sections with empty content (e.g. a prompts week with no prompts) are
    skipped. Entry timestamps are midnight in EVENTS_TZ on the section date.

    Args:
        page: Validated data page whose sections become feed entries.
        feed_title: Atom feed title.
        feed_subtitle: Atom feed subtitle.
        output_path: Path where the Atom file should be written.
        page_url: Site-relative page URL (e.g. "topics/").
    """
    sections: Iterable[DataPageSection] = [s for s in page.sections if s.content_html()]
    logger.info(f"Generating {feed_title} feed with {len(sections)} entries")
    feed_url = config.SITE_URL + output_path.name
    page_url_abs = config.SITE_URL + page_url

    fg = FeedGenerator()
    fg.id(feed_url)
    fg.title(feed_title)
    fg.author(name="IndieWebClub Bangalore")
    fg.link(href=feed_url, rel="self")
    fg.link(href=page_url_abs, rel="alternate")
    fg.subtitle(feed_subtitle)

    feed_updated: datetime | None = None
    for section in sections:
        ts = datetime.combine(
            section.date, datetime.min.time(), tzinfo=config.EVENTS_TZ
        )
        slug = _section_slug(_date_display(section.date))
        entry_url = f"{page_url_abs}#{slug}"

        fe = fg.add_entry(order="append")
        fe.id(entry_url)
        fe.title(section.feed_title)
        fe.link(href=entry_url, rel="alternate")
        fe.published(ts)
        fe.updated(ts)
        fe.content(section.content_html(), type="html")

        if feed_updated is None or feed_updated < ts:
            feed_updated = ts

    fg.updated(feed_updated or datetime.now(config.EVENTS_TZ))
    fg.atom_file(output_path, pretty=True)
    logger.info(f"Feed written to: {output_path}")
