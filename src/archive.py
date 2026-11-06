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
logger = logging.getLogger("archive")

MONTH_NAMES = list(calendar.month_name)


def _archive_entry_ctx(entry: FeedEntry) -> dict[str, str | bool]:
    """Extend the base entry context with archive-specific fields.

    Args:
        entry: Feed entry to build the context for.

    Returns:
        Entry context with tags, has_tags keys added.
    """
    ctx: dict[str, str | bool] = entry_ctx(entry)
    tags_str = " ".join(entry.tags)
    ctx["tags"] = tags_str
    ctx["has_tags"] = len(entry.tags) > 0
    return ctx


def group_entries_by_year(
    entries: list[FeedEntry],
) -> dict[int, list[FeedEntry]]:
    """Group entries by publication year.

    Args:
        entries: Feed entries to group.

    Returns:
        Mapping of publication year to the entries published that year.
    """
    by_year: defaultdict[int, list[FeedEntry]] = defaultdict(list)
    for entry in entries:
        by_year[entry.published.year].append(entry)
    return dict(by_year)


def _build_quarterly_chart_svg(entries: list[FeedEntry]) -> str:
    """Build an inline SVG stacked bar chart of posts per quarter over all time.

    Each bar covers one quarter and is split into three month segments, so the
    month-level counts stay visible (and clickable) inside the quarter.

    Args:
        entries: Feed entries to count per quarter.

    Returns:
        SVG markup for the bar chart, empty string if there are no entries.
    """
    counts: defaultdict[tuple[int, int], int] = defaultdict(int)
    for entry in entries:
        counts[(entry.published.year, entry.published.month)] += 1

    if not counts:
        return ""

    first_year, first_month = min(counts.keys())
    last_year, last_month = max(counts.keys())

    quarters: list[tuple[int, int]] = []
    y, q = first_year, (first_month - 1) // 3
    while (y, q) <= (last_year, (last_month - 1) // 3):
        quarters.append((y, q))
        q += 1
        if q > 3:
            q = 0
            y += 1

    def _quarter_total(year: int, quarter: int) -> int:
        return sum(counts.get((year, quarter * 3 + m), 0) for m in (1, 2, 3))

    max_count = max(_quarter_total(year, quarter) for year, quarter in quarters)

    bar_w = 8
    gap = 2
    # Keep the chart close to its original width when there are only a few
    # quarters; otherwise the viewBox gets tall and the bars balloon.
    min_chart_w = 170
    if len(quarters) * (bar_w + gap) - gap < min_chart_w:
        bar_w = min(40, (min_chart_w + gap) // len(quarters) - gap)
        gap = max(gap, bar_w // 4)
    chart_w = max(len(quarters) * (bar_w + gap) - gap, min_chart_w)
    chart_h = 60
    chart_top = 6
    chart_bottom = chart_top + chart_h
    segment_gap = 0.2
    month_count_font_size = 2.9
    total_font_size = 3.3
    quarter_label_font_size = 3.6
    year_label_font_size = 4.2
    quarter_label_padding = 5
    year_label_padding = 13
    quarter_label_y = chart_bottom + quarter_label_padding
    year_label_y = chart_bottom + year_label_padding
    total_h = year_label_y + 2

    quarters_ctx: list[dict[str, object]] = []
    quarter_labels: list[dict[str, str]] = []
    year_labels: list[dict[str, str]] = []
    seen_years: set[int] = set()
    for i, (year, quarter) in enumerate(quarters):
        x = i * (bar_w + gap)
        total = _quarter_total(year, quarter)
        quarter_h = (total / max_count) * chart_h
        stacked_h = 0.0
        months_ctx: list[dict[str, str | int]] = []
        for month_index, month in enumerate(
            range(quarter * 3 + 1, quarter * 3 + 4), start=1
        ):
            count = counts.get((year, month), 0)
            h = (count / max_count) * chart_h
            segment_y = chart_bottom - stacked_h - h
            stacked_h += h
            if count == 0:
                continue
            # Leave a hairline gap between the month segments of a quarter.
            has_gap = h > segment_gap * 2
            render_y = segment_y + (segment_gap if has_gap else 0)
            render_h = h - (segment_gap if has_gap else 0)
            plural = "s" if count != 1 else ""
            months_ctx.append(
                {
                    "month_index": month_index,
                    "y": f"{render_y:.2f}",
                    "h": f"{render_h:.2f}",
                    "href": f"/archive/{year}/#m-{year}-{month}",
                    "title": f"{MONTH_NAMES[month]} {year}: {count} post{plural}",
                    "show_count": render_h >= month_count_font_size + 0.1,
                    "count": str(count),
                    "count_x": f"{x + bar_w / 2}",
                    "count_y": (
                        f"{render_y + render_h / 2 + month_count_font_size * 0.35:.2f}"
                    ),
                }
            )
        # The quarter total sits above the bar, outside the stack.
        quarters_ctx.append(
            {
                "x": f"{x}",
                "w": f"{bar_w}",
                "months": months_ctx,
                "show_count": total > 0,
                "count": str(total),
                "count_x": f"{x + bar_w / 2}",
                "count_y": f"{chart_bottom - quarter_h - 1.5:.2f}",
            }
        )
        quarter_labels.append(
            {
                "x": f"{x + bar_w / 2}",
                "y": f"{quarter_label_y}",
                "label": f"Q{quarter + 1}",
            }
        )
        if year not in seen_years:
            year_labels.append({"x": f"{x}", "y": f"{year_label_y}", "year": str(year)})
            seen_years.add(year)

    first_quarter, last_quarter = quarters[0][1], quarters[-1][1]
    aria_label = (
        f"Posts per quarter from Q{first_quarter + 1} {first_year} "
        f"to Q{last_quarter + 1} {last_year}, stacked by month, "
        f"peak of {max_count} posts in a quarter"
    )
    renderer = make_renderer()
    template = read_template("archive-chart.svg")
    return renderer.render(
        template,
        {
            "chart_w": str(chart_w),
            "total_h": str(total_h),
            "aria_label": aria_label,
            "quarters": quarters_ctx,
            "quarter_labels": quarter_labels,
            "year_labels": year_labels,
            "month_count_font_size": f"{month_count_font_size}",
            "total_font_size": f"{total_font_size}",
            "quarter_label_font_size": f"{quarter_label_font_size}",
            "year_label_font_size": f"{year_label_font_size}",
        },
    )


def generate_archive_index(
    entries: list[FeedEntry],
    years: list[int],
    output_dir: Path,
):
    """Generate the archive index page listing all years.

    Args:
        entries: All feed entries to include in the archive.
        years: Sorted list of years to show links for.
        output_dir: Directory where the archive/ page should be written.
    """
    if not entries:
        logger.info("No entries for archive, skipping")
        return

    entries_sorted = sorted(entries, key=lambda e: e.published, reverse=True)
    total_posts = len(entries_sorted)
    member_count = len({e.feed_title for e in entries_sorted})
    first_entry = entries_sorted[0].published
    last_entry = entries_sorted[-1].published
    date_range = f"{MONTH_NAMES[last_entry.month]} {last_entry.year}–{MONTH_NAMES[first_entry.month]} {first_entry.year}"

    chart_svg = _build_quarterly_chart_svg(entries_sorted)

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
    """Generate a single year's archive page.

    Args:
        year: Year to generate the page for.
        year_entries: Entries published in that year.
        years: Sorted list of all archive years, for prev/next navigation.
        output_dir: Directory where the archive/<year>/ page should be written.
    """
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
