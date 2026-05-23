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


def _build_monthly_chart_svg(entries: list[FeedEntry]) -> str:
    """Build an inline SVG bar chart of posts per month over all time."""
    counts: defaultdict[tuple[int, int], int] = defaultdict(int)
    for entry in entries:
        counts[(entry.published.year, entry.published.month)] += 1

    if not counts:
        return ""

    first_year, first_month = min(counts.keys())
    last_year, last_month = max(counts.keys())

    months: list[tuple[int, int]] = []
    y, m = first_year, first_month
    while (y, m) <= (last_year, last_month):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    max_count = max(counts.values())

    bar_w = 8
    gap = 2
    chart_h = 50
    year_label_font_size = 1
    count_label_font_size = 2.5
    month_label_padding = 3
    year_label_padding = 7
    month_label_y = chart_h + month_label_padding
    year_label_y = chart_h + year_label_padding
    chart_w = max(len(months) * (bar_w + gap) - gap, 1)
    total_h = year_label_y + year_label_font_size

    bars: list[dict[str, str | bool]] = []
    month_labels: list[dict[str, str]] = []
    year_labels: list[dict[str, str]] = []
    seen_years: set[int] = set()
    for i, (year, month) in enumerate(months):
        count = counts.get((year, month), 0)
        h = (count / max_count) * chart_h if max_count else 0
        x = i * (bar_w + gap)
        plural = "s" if count != 1 else ""
        # Only render count label when bar is tall enough to fit the text
        show_count = count > 0 and h >= count_label_font_size + 1
        bars.append(
            {
                "x": f"{x}",
                "y": f"{chart_h - h:.2f}",
                "w": f"{bar_w}",
                "h": f"{h:.2f}",
                "chart_h": f"{chart_h}",
                "href": f"/archive/{year}/#m-{year}-{month}",
                "title": f"{MONTH_NAMES[month]} {year}: {count} post{plural}",
                "show_count": show_count,
                "count": str(count),
                "count_x": f"{x + bar_w / 2}",
                "count_y": f"{chart_h - 1}",
            }
        )
        month_labels.append(
            {
                "x": f"{x + bar_w / 2}",
                "y": f"{month_label_y}",
                "label": MONTH_NAMES[month][0:3],
            }
        )
        if year not in seen_years:
            year_labels.append({"x": f"{x}", "y": f"{year_label_y}", "year": str(year)})
            seen_years.add(year)

    aria_label = (
        f"Posts per month from {MONTH_NAMES[first_month]} {first_year} "
        f"to {MONTH_NAMES[last_month]} {last_year}, "
        f"peak of {max_count} posts in a month"
    )
    renderer = make_renderer()
    template = read_template("archive-chart.svg")
    return renderer.render(
        template,
        {
            "chart_w": str(chart_w),
            "total_h": str(total_h),
            "aria_label": aria_label,
            "bars": bars,
            "month_labels": month_labels,
            "year_labels": year_labels,
        },
    )


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

    chart_svg = _build_monthly_chart_svg(entries_sorted)

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
                "chart_svg": chart_svg,
                "has_chart": bool(chart_svg),
            },
        ),
        page_url="archive/",
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
    for month in sorted(by_month.keys()):
        month_entries = by_month[month]
        month_entries.sort(key=lambda e: e.published)
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
    next_year = str(years[i + 1]) if i + 1 < len(years) else ""
    prev_year = str(years[i - 1]) if i > 0 else ""

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
        page_url=f"archive/{year}/",
        output_dir=output_dir / "archive" / str(year),
    )
    logger.info(f"Generated archive page for {year}")
