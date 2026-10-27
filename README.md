# IndieWebClub Bangalore Website

This repository contains the source code for the [IndieWebClub Bangalore website](https://blr.indiewebclub.org/), a static site generated using Python. It aggregates member blog feeds, community events, and newsletter archives into a full static site with Atom feeds, an iCalendar file, a member directory, a webring, an archive, and a full-text search page.

## Features

- Event feed and calendar
- Homepage with event and writing previews
- Dedicated events and writings pages with top navigation
- Blogroll and member posts on the writings page
- Member directory
- Webring
- Full-text search over every post (runs in the browser, no server)
- CI/CD

## Installation

The site builds with or without [nix](https://nixos.org). The nix shell is the
easy path because it provides the whole toolchain; without nix you install the
same tools yourself.

### Prerequisites

- Python 3.x
- `make`
- Optionally, for the `/search/` page (see [Search toolchain](#search-toolchain)):
  - the [tinysearch](https://github.com/tinysearch/tinysearch) binary
  - Rust with the `wasm32-unknown-unknown` target (cargo + rustc)
  - optionally [binaryen](https://github.com/WebAssembly/binaryen) for `wasm-opt`
    (it only makes the compiled search engine smaller; the build works without it)

The search toolchain is **optional**: without it the build still succeeds, and
`/search/` is kept as a page that says search is unavailable, so the top
navigation, writings page, and archive links always work. That way you can work
on the rest of the site without installing a Rust toolchain.

### Option A: with nix (recommended)

The repository ships a `shell.nix` with everything: the Python dependencies,
the prebuilt `tinysearch` binary, Rust (including the `wasm32-unknown-unknown`
standard library) and `binaryen`.

```bash
git clone https://github.com/IndieWebClubBlr/website.git
cd website

# enter the nix shell (it has the Python dependencies, tinysearch, Rust
# and binaryen):
nix-shell
```

Inside the shell:

```bash
make setup   # Python virtual environment (pip install -r requirements.txt)
make build   # builds the site into _site/
```

`nix-shell` also provides `black`, `ruff` and `basedpyright` for linting.

### Option B: without nix

```bash
git clone https://github.com/IndieWebClubBlr/website.git
cd website

make setup   # creates venv/ and installs requirements.txt
source venv/bin/activate
```

Then install the [search toolchain](#search-toolchain) and check it:

```bash
make check-tools
```

### Search toolchain

The `/search/` page is a [tinysearch](https://github.com/tinysearch/tinysearch)
engine compiled to WebAssembly. Compiling it needs a Rust toolchain with the
`wasm32-unknown-unknown` target and the `tinysearch` CLI. It is optional: when
the tools are missing, the build logs a warning and renders `/search/` without
the search UI, so the link still resolves.

Without nix:

```bash
# 1. Rust (rustup bundles the wasm linker, so nothing else is needed)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
rustup target add wasm32-unknown-unknown

# 2. tinysearch — download the release binary for your platform
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)   asset=aarch64-apple-darwin ;;
  Darwin-x86_64)  asset=x86_64-apple-darwin ;;
  Linux-x86_64)   asset=x86_64-unknown-linux-gnu ;;
  *) echo "no prebuilt binary for $(uname -s)-$(uname -m); see the releases page"; exit 1 ;;
esac
curl -LO "https://github.com/tinysearch/tinysearch/releases/download/v0.11.1/tinysearch-v0.11.1-$asset.tar.gz"
tar -xzf "tinysearch-v0.11.1-$asset.tar.gz"
sudo install -m755 tinysearch /usr/local/bin/tinysearch

# 3. optional: wasm-opt, to shrink the compiled engine
brew install binaryen          # macOS
sudo apt-get install binaryen # Debian/Ubuntu
```

`make check-tools` verifies that `tinysearch`, `cargo` and the wasm32 standard
library are present and prints what to do if they are not. The check is
optional too — it only tells you whether the `/search/` page will have a
working engine.

The engine is compiled only when the indexed corpus, the index schema, or the
availability of `wasm-opt` changes. It is cached in `.cache/search_engines/`,
keyed by those inputs, so a rebuild of an unchanged corpus reuses the
identical bytes; a warm compile takes a second or two, while the first build
also compiles the engine crate's dependencies. (GitHub Pages derives the ETag
from the file's timestamp, so it still changes with every deploy — the bytes
are what stay the same.)

## Usage

The following commands are available through the `Makefile`.

-   **`make build`**: Build the website to `_site/`. Add `CACHE=true` for faster rebuilds using cached feeds. Add `CACHE_FALLBACK=true` to fall back to cache on fetch failures.
-   **`make serve`**: Serve the website locally at `http://localhost:8000`.
-   **`make watch`**: Automatically rebuild on file changes (requires `entr`).
-   **`make assets`**: Copy static assets (CSS, SVG, PNG, JS) to `_site/`.
-   **`make check-tools`**: Check that the search toolchain (`tinysearch`, cargo with the wasm32 target) is available.
-   **`make graph`**: Generate a build dependency graph (`build_deps.svg`).
-   **`make clean`**: Remove `_site/` and `__pycache__`.
-   **`make clean_cache`**: Remove the caches (`.cache/` and the generated engine crate).
-   **`make clean_all`**: Full cleanup — `_site/`, `venv`, `.cache`.

## How it Works

The site is built by `src/generator.py` using a pull-based build system (`src/build.py`). Rules are registered with `@build.rule()` and dependencies are declared inline via `build.need()`. Tasks run in parallel via `ThreadPoolExecutor`.

The build:

1.  **Parses `blogroll.opml`** into a list of `FeedInfo` objects.
2.  **Fetches feeds** concurrently, separates weeknotes via regex, filters by recency.
3.  **Fetches events** from the Underline Center Discourse API.
4.  **Fetches newsletter archive** from an RSS feed.
5.  **Resolves search bodies**: the post text of every entry, taken from the feed's own content when it is long enough and otherwise fetched from the post's permalink (cached in `.cache/search/`).
6.  **Compiles the search engine** with tinysearch into a WebAssembly module, and writes the documents corpus it was built from (`.cache/search_docs/`).
7.  **Generates pages**: homepage with event and writing previews; events page (upcoming and previous); writings page (recent posts, weeknotes, on-this-day, random posts); Markdown pages, member directory, archive index and per-year archive with JS search, webring tool page, newsletter page, full-text search page.
8.  **Generates feeds and calendar**: Atom feeds for blogroll, weeknotes, events, topics and prompts; iCalendar file for events.
9.  **Generates webring**: A circular ring of member websites with per-slug redirect pages.
10. **Generates sitemap.xml** from all `index.html` files in `_site/`.

The site is deployed to GitHub Pages via GitHub Actions (`.github/workflows/update.yml`), rebuilding hourly and on push.

### Caching

Fetches are cached under `.cache/` so later builds can avoid them:

- **Member feeds** are cached as *parsed entries* (`.cache/feeds/<sha256(url)>.json`), not as raw bytes: parsing a raw feed is the most expensive part of the feed phase, and a parsed-entry cache skips it. The cache records the hash of the raw feed it was built from, so a feed that changed is parsed again.
- **Search data**: post texts fetched from a permalink (`.cache/search/`), the extracted corpus (`.cache/search_docs/`) and the compiled engines (`.cache/search_engines/`).
- **Events, favicons, IndieWeb features and the newsletter archive** are cached as JSON/XML. The favicon and IndieWeb caches are dropped at random (4% of builds) so they are refreshed periodically, and the newsletter archive expires after a day.

`make build CACHE=true` reads feed entries from the cache instead of fetching them; a feed cache miss fetches live and warms the cache. `make build CACHE_FALLBACK=true` always fetches live and falls back to the cache when a feed cannot be reached. Either way, a cold build still fetches the permalinks of the posts whose text is not in the feed (`.cache/search/`), later builds reuse them, and the newsletter archive and member probes refresh on their own schedules.

The parsed-entry cache does not cover how the feeds are parsed, so a change to the parsing only takes effect after a cache clear: `make clean_cache` locally, and the weekly `.github/workflows/clear_cache.yml` in CI.

### Full-text search

`/search/` is a standalone page: it downloads a tinysearch engine (~1 MB,
gzipped) and queries it in the browser, so no query ever reaches a server and
nothing is logged. The engine is built at build time from the whole corpus
(all posts, back to 2001) and its file name carries a hash of that corpus and
how it is indexed, so the URL changes when the index or its settings change.

Queries are kept in the URL (`/search/?q=policy`), so a search can be linked.
After the typing pause or Enter, the page adds a history entry; back and
forward restore the previous query and its results.

A member can opt out of the full-text index without leaving the blogroll,
homepage, archive or generated feeds. Add `noindex="true"` to their
`blogroll.opml` outline, or send `X-Robots-Tag: noindex` (`none` also works)
on the feed or a post permalink, or use a robots meta tag such as
`<meta name="robots" content="noindex">`. Page-level header/meta tags are
seen when the permalink is fetched for its search body; posts whose feeds
carry full content rely on the feed-level signal. Cached page signals are
refreshed when the search cache is cleared (weekly in CI) or the extractor
is bumped. The build log lists every excluded URL.

Both the index and the query are Porter-stemmed, so `policies` finds `policy`
(and vice versa) while prefix matching still works from three characters. The
same stemmer runs on both sides: Python's `snowballstemmer` at build time and
the vendored `assets/stemmer.js` in the browser. Keeping the two in sync
matters — if the stems differ, a query silently stops matching its own index.
The browser copy is a minified, dead-code-trimmed build of
[snowball-stemmer.jsx](https://github.com/shibukawa/snowball-stemmer.jsx)'s
Porter stemmer (MIT), retaining only the code reachable from `stemQuery()`.

British and American spellings are normalized to one form before stemming,
using a committed one-way GB→US map (`assets/spelling.json`) built from the
MIT-licensed Breame and uk2us lists. The same map is inlined into `/search/`,
so `colour` and `color` (and their inflections) find the same posts.

### Fetching and SSRF

Almost every URL the site fetches comes from outside the site: the feeds in
`blogroll.opml`, the permalinks inside them, the favicons the members' pages
point at. A feed could point the build at a service that is only reachable
from the build host (a cloud metadata endpoint, a service on the build
machine's LAN), so every request goes through `src/ssrf.py`, which

- refuses anything that is not `http`/`https`,
- refuses host names that are known to be private (`localhost` and friends),
- checks the URL that `requests` normalizes and connects to rather than the
  raw string, so the two cannot disagree about the host,
- resolves the host and refuses the request when *any* of the resolved
  addresses is private, loopback, link-local, multicast or otherwise
  reserved — including the IPv4 addresses embedded in IPv6 ones
  (`::ffff:127.0.0.1`, `64:ff9b::7f00:1`, `2002:7f00:1::`, Teredo, ISATAP),
- and follows redirects by hand, checking every hop, so a public URL cannot
  redirect the build to an internal one.

A NAT64 (`64:ff9b::/96`), 6to4 or Teredo address is refused as a whole, even
when the address it embeds is public. An IPv6-only network that reaches the
internet through a NAT64 gateway therefore cannot be fetched from.

The check mirrors the SSRF guard in
[feed-repeat](https://github.com/abhin4v/feed-repeat). Like that guard, it
resolves the name and the HTTP client resolves it again, so a host whose DNS
answers differently for the two lookups (DNS rebinding) is not covered.

## Configuration

The project's behavior can be customized through the `src/config.py` file. This file includes settings for request timeouts, content length limits, feed and calendar file names, search body limits, and more. Some of the key options you might want to modify are:

-   `MAX_SHOWN_POSTS_PER_FEED`: The number of posts to display per blog in the writings lists and homepage preview.
-   `MAX_SHOWN_HOMEPAGE_EVENTS`: The number of upcoming events to preview on the homepage (default 2).
-   `MAX_SHOWN_HOMEPAGE_POSTS`: The number of posts to preview on the homepage (default 5).
-   `MIN_FEED_ENTRY_AGE_HOURS`: Entries younger than this many hours are skipped (default 2).
-   `MAX_FEED_ENTRIES`: Maximum number of entries parsed from each feed (default 1,000).
-   `SEARCH_BODY_MAX_CHARS`: How much of each post is indexed (default 25,000 characters).
-   `SEARCH_FEED_BODY_MIN_CHARS`: How long feed-provided content must be to be indexed as-is (default 500); shorter posts are fetched from their permalink.

## Project Structure

A brief overview of the project's file structure:

```
/
├── .github/                   # GitHub Actions workflows
├── _site/                     # Generated website output
├── assets/                    # Static assets
│   ├── archive.js             # Archive page search
│   ├── search.js              # Full-text search page (loads the wasm engine)
│   ├── spelling.json          # British→American spelling map for search
│   ├── stemmer.js             # Porter stemmer for search queries (mirrors search.py)
│   ├── style.css              # Styles
│   ├── favicon.svg
│   ├── indiewebcamp-button.svg
│   ├── preview.png
│   └── .nojekyll
├── data/                      # YAML data pages (topics, prompts)
├── pages/                     # Markdown & static HTML pages
│   ├── coc.md
│   ├── 2026-se/index.html
│   ├── whatsapp/index.html
│   └── ...
├── scripts/                   # Helper scripts
│   ├── gen_build_graph.py     # Build dependency graph
│   └── serve.py               # Local development server
├── src/                       # Python source
│   ├── build.py               # Pull-based build system
│   ├── config.py              # Configuration constants
│   ├── data_pages.py          # YAML data pages
│   ├── generator.py           # Main generator & build rules
│   ├── feeds.py               # Feed parsing & OPML
│   ├── events.py              # Discourse API events
│   ├── member_dir.py          # Member directory with IndieWeb badges
│   ├── newsletter.py          # Newsletter archive
│   ├── archive.py             # Archive pages
│   ├── search.py              # Search corpus & tinysearch engine
│   ├── ssrf.py                # SSRF guard for outbound requests
│   └── utils.py               # Templating, HTTP sessions, ref params
├── templates/                 # Pystache templates
│   ├── default.html           # Site layout wrapper with top navigation
│   ├── index.html             # Homepage
│   ├── events.html            # Events page
│   ├── writings.html          # Writings page
│   ├── event-card.html        # Shared event card partial
│   ├── post-card.html         # Shared post card partial
│   ├── members.html           # Member directory
│   ├── search.html            # Full-text search page
│   ├── topics.html            # Topics data page
│   ├── prompts.html           # Writing prompts data page
│   ├── webring.html           # Webring tool page
│   ├── webring-redirect.html  # Webring redirect pages
│   ├── archive-index.html     # Archive index
│   ├── archive-year.html      # Per-year archive
│   ├── archive-chart.svg      # Monthly bar chart
│   ├── nl-subsribe.html       # Newsletter page
│   └── nl-form.html           # Newsletter form partial
├── blogroll.opml              # Feed list
├── nix/                       # Nix packaging
│   └── tinysearch.nix         # Prebuilt tinysearch binary (not in nixpkgs)
├── Makefile
├── watch.sh                   # Rebuild/copy on file changes (used by `make watch`)
├── requirements.txt
├── CNAME
└── shell.nix                  # Nix development environment
```

## Contributing

Contributions are welcome! Please feel free to open an issue or submit a pull request.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
