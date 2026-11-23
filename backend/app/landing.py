"""The landing page at / (demo issue #103): HTML built once at startup.

It has one inline script for the live status strip (demo #230); the CSP carries
that script's hash.

The page is not part of the redirect SLO: its metric route is "/", and the
recording rules in the gitops repo select route="/{code}" only.
"""

import base64
import hashlib
import logging
import re
from pathlib import Path

from app import links

log = logging.getLogger("app")

PAGE = Path(__file__).parent / "static" / "index.html"
# Replaced by the example link's list item, or by nothing.
EXAMPLE_MARKER = "<!--example-->"

# Exactly one inline script, with no attributes: the CSP hashes its text, and the
# hash is only meaningful if no other script can run.
_SCRIPT = re.compile(r"<script>(.*?)</script>", re.DOTALL)
_ANY_SCRIPT = re.compile(r"<script\b", re.IGNORECASE)


def headers(html: str) -> dict[str, str]:
    """Response headers for the page, with the CSP hash of its one inline script.

    Computed at startup from the HTML that is actually served, so editing the
    script can never leave a stale hash. Raises ValueError unless the page has
    exactly one plain <script> (no src, no attributes): fail at startup, not as
    a silently dead strip in every browser.
    """
    scripts = _SCRIPT.findall(html)
    if len(scripts) != 1 or len(_ANY_SCRIPT.findall(html)) != 1:
        raise ValueError("the landing page must have exactly one plain inline <script>")
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode()
    return {
        # Static between deploys, so caches may keep it briefly.
        "Cache-Control": "public, max-age=300",
        # The page loads nothing from elsewhere; the CSP makes that a rule the
        # browser enforces. Inline CSS, the one hashed script (the live status
        # strip, demo #230) and same-origin fetches (its /api/status poll) are
        # all it allows. No unsafe-inline for scripts, no eval.
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; "
            f"script-src 'sha256-{digest}'; connect-src 'self'; base-uri 'none'; "
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
