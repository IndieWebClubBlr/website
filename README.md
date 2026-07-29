# IndieWebClub Bangalore Website

This repository contains the source code for the [IndieWebClub Bangalore website](https://blr.indiewebclub.org/), a static site generated using Python. It aggregates member blog feeds, community events, and newsletter archives into a full static site with Atom feeds, an iCalendar file, a member directory, a webring, and an archive.

## Features

- Event feed and calendar
- Member blogroll page and feeds
- Member directory
- Webring
- CI/CD

## Installation

### Prerequisites

- Python 3.x
- `make`

### Installation and Setup

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/IndieWebClubBlr/website.git
    cd website
    ```

2.  **Set up the environment and install dependencies:**
    The `Makefile` provides a convenient way to set up a Python virtual environment and install the required dependencies from `requirements.txt`.

    ```bash
    make setup
    ```

    You can run `make` or `make help` at any time to see a list of all available commands.

3.  **Activate the virtual environment if required:**
    ```bash
    source venv/bin/activate
    ```

## Usage

The following commands are available through the `Makefile`.

-   **`make build`**: Build the website to `_site/`. Add `CACHE=true` for faster rebuilds using cached feeds. Add `CACHE_FALLBACK=true` to fall back to cache on fetch failures.
-   **`make serve`**: Serve the website locally at `http://localhost:8000`.
-   **`make watch`**: Automatically rebuild on file changes (requires `entr`).
-   **`make assets`**: Copy static assets (CSS, SVG, PNG) to `_site/`.
-   **`make graph`**: Generate a build dependency graph (`build_deps.svg`).
-   **`make clean`**: Remove `_site/` and `__pycache__`.
-   **`make clean_cache`**: Remove `.cache/`.
-   **`make clean_all`**: Full cleanup — `_site/`, `venv`, `.cache`.

## How it Works

The site is built by `src/generator.py` using a pull-based build system (`src/build.py`). Rules are registered with `@build.rule()` and dependencies are declared inline via `build.need()`. Tasks run in parallel via `ThreadPoolExecutor`.

The build:

1.  **Parses `blogroll.opml`** into a list of `FeedInfo` objects.
2.  **Fetches feeds** concurrently, separates weeknotes via regex, filters by recency.
3.  **Fetches events** from the Underline Center Discourse API.
4.  **Fetches newsletter archive** from an RSS feed.
5.  **Generates pages**: homepage with events, weeknotes, recent posts, on-this-day entries, and random posts; Markdown pages, member directory, archive index and per-year archive with JS search, webring tool page, newsletter page.
6.  **Generates feeds and calendar**: Atom feeds for blogroll, weeknotes, and events; iCalendar file for events.
7.  **Generates webring**: A circular ring of member websites with per-slug redirect pages.
8.  **Generates sitemap.xml** from all `index.html` files in `_site/`.

The site is deployed to GitHub Pages via GitHub Actions (`.github/workflows/update.yml`), rebuilding hourly and on push.

## Configuration

The project's behavior can be customized through the `config.py` file. This file includes settings for request timeouts, content length limits, feed and calendar file names, and more. Some of the key options you might want to modify are:

-   `MAX_SHOWN_POSTS_PER_FEED`: The number of posts to display per blog on the homepage.
-   `MAX_FEED_ENTRY_AGE_DAYS`: The time window in days for fetching recent blog posts.
-   `MAX_FEED_ENTRIES`: The maximum number of entries to fetch from each feed.

## Project Structure

A brief overview of the project's file structure:

```
/
├── .github/                   # GitHub Actions workflows
├── _site/                     # Generated website output
├── assets/                    # Static assets
│   ├── archive.js             # Archive page search
│   ├── style.css              # Styles
│   ├── favicon.svg
│   ├── indiewebcamp-button.svg
│   ├── preview.png
│   └── .nojekyll
├── pages/                     # Markdown & static HTML pages
│   ├── coc.md
│   ├── topics.md
│   ├── whatsapp/index.html
│   └── ...
├── src/                       # Python source
│   ├── build.py               # Pull-based build system
│   ├── config.py              # Configuration constants
│   ├── generator.py           # Main generator & build rules
│   ├── feeds.py               # Feed parsing & OPML
│   ├── events.py              # Discourse API events
│   ├── member_dir.py          # Member directory with IndieWeb badges
│   ├── newsletter.py          # Newsletter archive
│   ├── archive.py             # Archive pages
│   └── utils.py               # Templating, HTTP sessions, ref params
├── templates/                 # Pystache templates
│   ├── default.html           # Site layout wrapper
│   ├── index.html             # Homepage
│   ├── members.html           # Member directory
│   ├── webring.html           # Webring tool page
│   ├── webring-redirect.html  # Webring redirect pages
│   ├── archive-index.html     # Archive index
│   ├── archive-year.html      # Per-year archive
│   ├── archive-chart.svg      # Monthly bar chart
│   ├── nl-subsribe.html       # Newsletter page
│   └── nl-form.html           # Newsletter form partial
├── blogroll.opml              # Feed list
├── Makefile
├── requirements.txt
├── CNAME
└── shell.nix
```

## Contributing

Contributions are welcome! Please feel free to open an issue or submit a pull request.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
