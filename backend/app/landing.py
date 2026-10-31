"""The landing page at / (demo issue #103): static HTML, built once at startup.

The page is not part of the redirect SLO: its metric route is "/", and the
recording rules in the gitops repo select route="/{code}" only.
"""

import logging
from pathlib import Path

from app import links

log = logging.getLogger("app")

PAGE = Path(__file__).parent / "static" / "index.html"
# Replaced by the example link's list item, or by nothing.
EXAMPLE_MARKER = "<!--example-->"

HEADERS = {
    # Static between deploys, so caches may keep it briefly.
    "Cache-Control": "public, max-age=300",
    # The page has no scripts and loads nothing from elsewhere; the CSP makes
    # that a rule the browser enforces. Inline CSS is the only thing it allows.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
}


def render(example_code: str | None) -> str:
    """The page, with an example short link if a valid code is given.

    The code is configuration (EXAMPLE_LINK_CODE), not part of the page: a link
    is a row in one cluster's database, so each cluster has its own. Only a
    valid code is shown; base62 only, so it cannot inject HTML. An invalid one
    is logged and left out, never fatal: a cosmetic setting must not stop the
    app that serves the redirects. A missing PAGE file does fail, at startup.
    """
    item = ""
    if example_code and links.is_valid_code(example_code):
        item = (
            f'  <li><a href="/{example_code}">/{example_code}</a>: an example short '
            "link; it redirects to the project's main repo.</li>\n"
        )
    elif example_code:
        log.warning("EXAMPLE_LINK_CODE %r is not a valid code; no example link shown", example_code)
    return PAGE.read_text(encoding="utf-8").replace(EXAMPLE_MARKER, item)
