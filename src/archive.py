"""Archive page generation for IWCB website."""

from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from pathlib import Path

from src import config
from src.feeds import FeedEntry, entry_ctx
from src.utils import make_renderer, read_template, render_and_save_html

logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)

MONTH_NAMES = list(calendar.month_name)


def _archive_entry_ctx(entry: FeedEntry) -> dict[str, str | bool]:
    """Extend the base entry context with archive-specific fields."""
    ctx: dict[str, str | bool] = entry_ctx(entry)
    tags_str = " ".join(entry.tags)
    ctx["tags"] = tags_str
    ctx["has_tags"] = len(entry.tags) > 0
    ctx["search_text"] = " ".join(
        [
            entry.title.lower(),
            entry.feed_title.lower(),
            tags_str.lower(),
            entry.summary.lower(),
        ]
    )
    return ctx


def group_entries_by_year(
    entries: list[FeedEntry],
) -> dict[int, list[FeedEntry]]:
    """Group entries by publication year."""
    by_year: defaultdict[int, list[FeedEntry]] = defaultdict(list)
    for entry in entries:
        by_year[entry.published.year].append(entry)
    return dict(by_year)


def generate_archive_index(
    entries: list[FeedEntry],
    years: list[int],
    output_dir: Path,
):
    """Generate the archive index page listing all years."""
    if not entries:
        logger.info("No entries for archive, skipping")
        return

    entries_sorted = sorted(entries, key=lambda e: e.published, reverse=True)
    total_posts = len(entries_sorted)
    member_count = len({e.feed_title for e in entries_sorted})
    first_entry = entries_sorted[0].published
    last_entry = entries_sorted[-1].published
    date_range = f"{MONTH_NAMES[last_entry.month]} {last_entry.year}–{MONTH_NAMES[first_entry.month]} {first_entry.year}"

    renderer = make_renderer()
    index_template = read_template("archive-index.html")
    years_ctx = [
        {
            "year": str(year),
            "count": str(len([e for e in entries_sorted if e.published.year == year])),
        }
        for year in years
    ]

    render_and_save_html(
        html_content=renderer.render(
            index_template,
            {
                "total_posts": str(total_posts),
                "member_count": str(member_count),
                "date_range": date_range,
                "years": years_ctx,
            },
        ),
        output_dir=output_dir / "archive",
    )
    logger.info("Generated archive index page")


def generate_archive_year(
    year: int,
    year_entries: list[FeedEntry],
    years: list[int],
    output_dir: Path,
):
    """Generate a single year's archive page."""
    # Group by month (descending)
    by_month: defaultdict[int, list[FeedEntry]] = defaultdict(list)
    for entry in year_entries:
        by_month[entry.published.month].append(entry)

    months_ctx = []
    for month in sorted(by_month.keys(), reverse=True):
        month_entries = by_month[month]
        month_entries.sort(key=lambda e: e.published, reverse=True)
        months_ctx.append(
            {
                "month": month,
                "month_name": MONTH_NAMES[month],
                "count": str(len(month_entries)),
                "entries": [_archive_entry_ctx(e) for e in month_entries],
            }
        )

    year_member_count = len({e.feed_title for e in year_entries})

    i = years.index(year)
    prev_year = str(years[i + 1]) if i + 1 < len(years) else ""
    next_year = str(years[i - 1]) if i > 0 else ""

    renderer = make_renderer()
    year_template = read_template("archive-year.html")

    render_and_save_html(
        html_content=renderer.render(
            year_template,
            {
                "year": str(year),
                "total_posts": str(len(year_entries)),
                "member_count": str(year_member_count),
                "months": months_ctx,
                "prev_year": prev_year,
                "next_year": next_year,
            },
        ),
        output_dir=output_dir / "archive" / str(year),
    )
    logger.info(f"Generated archive page for {year}")
