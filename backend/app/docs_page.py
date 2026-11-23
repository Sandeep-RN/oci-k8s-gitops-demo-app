"""The Swagger UI page at /api/docs, with a dark theme (demo issues #229, #231).

FastAPI's built-in page has no dark mode, so the page is built with
get_swagger_ui_html and an extra stylesheet is inserted before </head>.
The colours match the landing page (dark background #0a0c12).
"""

from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse

# Swagger UI has no dark theme, so the colours are set explicitly. An invert()
# filter was tried first (#229) and rejected: it dims everything and leaves
# grey boxes behind some text. The palette is the landing page's.
# !important is used on text colours because Swagger's own rules are many and
# deeply nested; a theme override has to win against all of them.
DARK_CSS = """
:root { color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0a0c12; --card: #131722; --card2: #1a2030; --line: #2a3142;
          --fg: #e6edf3; --muted: #9aa4b2; --link: #6cb0ff; }
  html, body { background: var(--bg); margin: 0; }
  .swagger-ui, .swagger-ui .wrapper { background: var(--bg); color: var(--fg); }
  .swagger-ui :is(h1, h2, h3, h4, h5, p, li, td, th, label, span, small, legend, .title,
    .info .title, .info li, .info p, .info table, .opblock-tag, .opblock-summary-path,
    .opblock-description-wrapper p, .parameter__name, .parameter__type,
    .response-col_status, .response-col_description, .tab li, .model-title, .model,
    .prop-type, .renderedMarkdown p, .markdown p, .responses-inner h4, .responses-inner h5,
    table thead tr th, table thead tr td, .opblock-section-header h4,
    .opblock-section-header label, .scheme-container .schemes > label, section.models h4,
    section.models h5, .servers > label, .dialog-ux .modal-ux-header h3,
    .dialog-ux .modal-ux-content h4, .dialog-ux .modal-ux-content p) {
    color: var(--fg) !important; }
  .swagger-ui :is(.info .base-url, .parameter__extension, .parameter__in,
    .parameter__deprecated, .opblock-summary-description, .response-col_links, .prop-format) {
    color: var(--muted) !important; }
  .swagger-ui a, .swagger-ui .info a, .swagger-ui .info a * { color: var(--link) !important; }
  .swagger-ui .scheme-container { background: var(--bg); box-shadow: none;
    border-bottom: 1px solid var(--line); }
  .swagger-ui .scheme-container .wrapper { background: transparent; }
  /* The schema list: buttons and spans carry their own grey fills and dim text. */
  .swagger-ui section.models button, .swagger-ui section.models .model-box-control,
  .swagger-ui section.models .model-box, .swagger-ui section.models .model-title,
  .swagger-ui section.models .model-title__text, .swagger-ui section.models span {
    background: transparent !important; color: var(--fg) !important; }
  .swagger-ui section.models .models-control span,
  .swagger-ui section.models .model-box-control span,
  .swagger-ui section.models .prop-type { color: var(--muted) !important; }
  .swagger-ui .opblock-tag { border-bottom: 1px solid var(--line); }
  .swagger-ui .opblock-tag:hover { background: var(--card); }
  /* One tinted block per HTTP method: dark fill, coloured border, solid method label. */
  .swagger-ui .opblock { background: var(--card); border-color: var(--line); box-shadow: none; }
  .swagger-ui .opblock .opblock-summary { border-color: var(--line); }
  .swagger-ui .opblock.opblock-get { background: rgba(108,176,255,.08); border-color: #6cb0ff; }
  .swagger-ui .opblock.opblock-get .opblock-summary-method { background: #6cb0ff; }
  .swagger-ui .opblock.opblock-post { background: rgba(73,204,144,.08); border-color: #49cc90; }
  .swagger-ui .opblock.opblock-post .opblock-summary-method { background: #49cc90; }
  .swagger-ui .opblock.opblock-head { background: rgba(179,136,255,.08); border-color: #b388ff; }
  .swagger-ui .opblock.opblock-head .opblock-summary-method { background: #b388ff; }
  .swagger-ui .opblock.opblock-put { background: rgba(252,161,48,.08); border-color: #fca130; }
  .swagger-ui .opblock.opblock-put .opblock-summary-method { background: #fca130; }
  .swagger-ui .opblock.opblock-delete { background: rgba(249,62,62,.08); border-color: #f93e3e; }
  .swagger-ui .opblock.opblock-delete .opblock-summary-method { background: #f93e3e; }
  .swagger-ui .opblock .opblock-summary-method { color: #0a0c12 !important; text-shadow: none; }
  .swagger-ui .opblock .opblock-section-header { background: var(--card2); box-shadow: none; }
  .swagger-ui .opblock-body pre.microlight, .swagger-ui .highlight-code > .microlight,
  .swagger-ui .microlight { background: #05070b !important; color: #e6edf3 !important; }
  .swagger-ui .opblock-body pre.microlight span { color: inherit !important; }
  .swagger-ui table thead tr td, .swagger-ui table thead tr th {
    border-bottom: 1px solid var(--line); }
  .swagger-ui input[type=text], .swagger-ui input[type=password], .swagger-ui input[type=search],
  .swagger-ui input[type=email], .swagger-ui textarea, .swagger-ui select {
    background: var(--bg); color: var(--fg); border: 1px solid var(--line); }
  /* Schemas are drawn by Swagger's json-schema-2020-12 components, with their own classes. */
  .swagger-ui .json-schema-2020-12, .swagger-ui .json-schema-2020-12-head,
  .swagger-ui .json-schema-2020-12 button { background: transparent !important; }
  .swagger-ui .json-schema-2020-12 :is(strong, span, button, div, p, li, code) {
    color: var(--fg) !important; }
  .swagger-ui .json-schema-2020-12__attribute { color: var(--muted) !important; }
  .swagger-ui .json-schema-2020-12 button.json-schema-2020-12-expand-deep-button {
    color: var(--link) !important; }
  /* Icons are paths with fill="currentColor": they take the colour of their button. */
  .swagger-ui svg, .swagger-ui button svg, .swagger-ui .opblock-summary-control svg,
  .swagger-ui .json-schema-2020-12 svg { color: var(--fg) !important; }
  .swagger-ui .info hgroup.main a.link span.url { color: var(--link) !important; }
  .swagger-ui .btn.authorize svg, .swagger-ui .auth-wrapper .btn.authorize svg {
    color: #49cc90 !important; }
  .swagger-ui .btn { color: var(--fg); border-color: var(--muted); background: transparent;
    box-shadow: none; }
  .swagger-ui .btn.authorize { color: #49cc90; border-color: #49cc90; }
  .swagger-ui .btn.execute { background: #6cb0ff; border-color: #6cb0ff; color: #0a0c12; }
  .swagger-ui svg, .swagger-ui .opblock-summary-control svg, .swagger-ui .opblock-tag svg,
  .swagger-ui .authorization__btn svg, .swagger-ui section.models h4 svg,
  .swagger-ui .model-toggle::after { fill: var(--fg) !important; }
  .swagger-ui .btn.authorize svg { fill: #49cc90 !important; }
  .swagger-ui section.models { border: 1px solid var(--line); background: var(--card); }
  .swagger-ui section.models.is-open h4 { border-bottom: 1px solid var(--line); }
  .swagger-ui section.models .model-container { background: var(--card2); }
  .swagger-ui .model-box, .swagger-ui .model-box-control, .swagger-ui .models-control {
    background: transparent !important; }
  .swagger-ui .dialog-ux .modal-ux { background: var(--card); border: 1px solid var(--line);
    box-shadow: none; }
  .swagger-ui .dialog-ux .modal-ux-header { border-bottom: 1px solid var(--line); }
  .swagger-ui .info .title small { background: #3a4258; }
  .swagger-ui .info .title small :is(span, pre) { color: #fff !important; }
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
