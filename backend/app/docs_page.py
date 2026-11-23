"""The Swagger UI page at /api/docs, with a dark theme (demo issue #229).

FastAPI's built-in page has no dark mode, so the page is built with
get_swagger_ui_html and an extra stylesheet is inserted before </head>.
The colours match the landing page (dark background #0a0c12).
"""

from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse

# Swagger UI has no dark theme. Instead of re-styling hundreds of rules, the
# whole UI is inverted (white becomes dark, dark text becomes light) and the
# hue rotated back so colours (GET blue, POST green) stay recognisable. Code
# samples are dark already, so they are inverted a second time.
DARK_CSS = """
:root { color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  body { background: #0a0c12; margin: 0; }
  .swagger-ui { filter: invert(92%) hue-rotate(180deg); }
  .swagger-ui .microlight, .swagger-ui .highlight-code,
  .swagger-ui .opblock-body pre { filter: invert(100%) hue-rotate(180deg); }
}
"""


def swagger_page(openapi_url: str, title: str) -> HTMLResponse:
    page = get_swagger_ui_html(openapi_url=openapi_url, title=title)
    html = bytes(page.body).decode("utf-8")
    marker = "</head>"
    if marker not in html:
        # FastAPI changed its template: serve the plain page, never fail.
        return page
    return HTMLResponse(html.replace(marker, f"<style>{DARK_CSS}</style>\n{marker}", 1))
