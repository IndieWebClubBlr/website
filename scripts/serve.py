#!/usr/bin/env python3
"""Development server for the generated site.

Serves ``_site/`` with ``Cache-Control: no-store``: every file is fetched
fresh, in a single round trip. Edits therefore show up immediately, and no
render-blocking revalidation stands between the browser and the first
paint.

Local caching is deliberately not emulated; in production GitHub Pages
serves every file with ``max-age=600`` and revalidates it afterwards.

Usage:
    python scripts/serve.py [port] [directory]
"""

from __future__ import annotations

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class SiteHandler(SimpleHTTPRequestHandler):
    """Static file handler that asks the browser not to cache anything."""

    def end_headers(self) -> None:
        """Send the cache header before the response body."""
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> None:
    """Serve the site on the given port."""
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    directory = sys.argv[2] if len(sys.argv) > 2 else "_site"
    handler = partial(SiteHandler, directory=directory)
    server = ThreadingHTTPServer(("", port), handler)
    print(f"Serving {directory} on http://localhost:{port}/ (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping")


if __name__ == "__main__":
    main()
