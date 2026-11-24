"""The /status page: charts drawn as inline SVG, in Python (demo #235, ADR 0027).

No client JavaScript at all, so the CSP has no script-src and there is nothing
to inject into. The page is built from NUMBERS and CONSTANTS only:

- every coordinate goes through _n(), which prints a finite, bounded decimal;
- every label is a constant or a number formatted here (_fmt_*, _stamp);
- nothing a Prometheus answer carries as text (a label, a pod name, an address,
  an image) is ever passed to this module: the snapshot holds numbers, and a commit
  tag as 7 hex characters that history.py checked against a 40-hex pattern;
- html.escape() wraps the few strings that are not numbers anyway, so a future
  change that lets text through still cannot produce markup.

A gap in a series is a gap in the line, never a zero.
"""

import html
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from app.history_model import Deploy, DeploysData, HistorySnapshot, SeriesData
from app.status import SLO_TARGET_PERCENT

log = logging.getLogger("app")

# Exactly the CSP of the strictest page on the site: no script of any kind.
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
CACHE_CONTROL = "public, max-age=60"
NO_STORE = "no-store"

# ---- geometry (SVG user units; the SVG scales to its container) -----------------------
VIEW_W, VIEW_H = 480, 200
PLOT_LEFT, PLOT_RIGHT = 66.0, 468.0
PLOT_TOP, PLOT_BOTTOM = 10.0, 170.0
MAX_HOVER_POINTS = 40  # hover targets per chart: keeps the page small
MIN_TICK_GAP = 3.0  # user units between two drawn deploy ticks
VALUE_LIMIT = 1e15  # beyond this a "value" is garbage and drawn as a gap
STALE_CHART_SECONDS = 30.0  # a chart older than its snapshot by more than this says so
_TICK_INTERVALS = 4
_NICE = (1.0, 1.2, 1.6, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0)
_ZOOM_RANGES = (1.0, 2.0, 4.0, 10.0, 20.0, 40.0, 100.0)  # percent shown below 100


@dataclass(frozen=True)
class _Spec:
    key: str  # the RANGE_QUERIES key
    title: str
    kind: str  # pct | rate | bytes | count
    axis: str  # unit (0..100 %), zoom (the top of 0..100 %), zero (0 to a nice maximum)
    digits: int  # decimals in hover and summary text of a percent
    blurb: str  # one plain sentence, also the chart's description
    flip: bool = False  # draw 1 - value (error ratio -> availability)
    target: bool = False  # draw the SLO target line


SLO_CHARTS = (
    _Spec(
        "budget",
        "SLO error budget left",
        "pct",
        "unit",
        2,
        "Share of the 7-day error budget still unspent. 100% means no failed redirects.",
    ),
    _Spec(
        "errors",
        "Redirect availability",
        "pct",
        "zoom",
        3,
        "Share of redirects that did not fail, over a rolling 7 days.",
        flip=True,
        target=True,
    ),
    _Spec(
        "latency",
        "Fast redirects",
        "pct",
        "zoom",
        3,
        "Share of redirects answered within the latency target, over a rolling 7 days.",
    ),
)
NODE_CHARTS = (
    _Spec(
        "rps",
        "Redirects per second",
        "rate",
        "zero",
        3,
        "Redirects per second, 5 minute average. This includes the in-cluster probe.",
    ),
    _Spec(
        "cpu", "Node CPU in use", "pct", "zero", 1, "Share of node CPU in use, 5 minute average."
    ),
    _Spec(
        "mem",
        "Node memory in use",
        "pct",
        "zero",
        1,
        "Share of node memory in use, by MemAvailable.",
    ),
    _Spec(
        "net",
        "Node network transmit",
        "bytes",
        "zero",
        1,
        "Bytes per second the node sends, all traffic.",
    ),
    _Spec("pods", "Pods running", "count", "zero", 0, "Pods in the Running phase, whole cluster."),
)
ALL_CHARTS = SLO_CHARTS + NODE_CHARTS


# ---- numbers and text -----------------------------------------------------------------
def _n(x: float) -> str:
    """A coordinate: finite, bounded, one decimal. The only way a number enters a
    path or an attribute."""
    if not isinstance(x, int | float) or not math.isfinite(x):
        x = 0.0
    return f"{max(-1e5, min(1e5, float(x))):.1f}"


def _line(cls: str, x1: float, y1: float, x2: float, y2: float) -> str:
    return f'<line class="{cls}" x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}"/>'


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _usable(v: object) -> bool:
    return (
        isinstance(v, int | float)
        and not isinstance(v, bool)
        and math.isfinite(v)
        and 0.0 <= v <= VALUE_LIMIT
    )


def _stamp(ts: object) -> str:
    """UTC time as digits: 2026-11-24 10:30 UTC."""
    try:
        if not isinstance(ts, int | float) or not math.isfinite(ts):
            return "an unknown time"
        return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")
    except OverflowError, OSError, ValueError:
        return "an unknown time"


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def _words(seconds: float) -> str:
    """A window or a step in words: 24 hours, 7 days, 30 minutes, 90 seconds."""
    s = int(round(seconds))
    if s >= 2 * 86400 and s % 86400 == 0:
        return _plural(s // 86400, "day")
    if s >= 3600 and s % 3600 == 0:
        return _plural(s // 3600, "hour")
    if s >= 60 and s % 60 == 0:
        return _plural(s // 60, "minute")
    return _plural(s, "second")


def _trim(x: float) -> str:
    """3.9 -> '3.9', 6.0 -> '6'."""
    text = f"{x:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _step_digits(step: float) -> int:
    for d in range(7):
        if abs(round(step, d) - step) <= 1e-9 * max(1.0, abs(step)):
            return d
    return 6


def _nice_top(x: float) -> float:
    # 1e-9: below it the axis would be all zeros anyway, and a subnormal such as 5e-324
    # makes 10.0 ** floor(log10(x)) underflow to 0.0 (review M1).
    if not (math.isfinite(x) and x > 1e-9):
        return 1.0
    base = 10.0 ** math.floor(math.log10(x))
    if not base > 0:  # belt and braces: never divide by an underflowed power of ten
        return 1.0
    m = x / base
    for c in _NICE:
        if m <= c + 1e-9:
            return c * base
    return 10.0 * base


def _unit(kind: str, top: float) -> tuple[float, str]:
    """(divisor, suffix): a raw value is shown as raw / divisor + suffix."""
    if kind == "pct":
        return 0.01, "%"
    if kind == "rate":
        return 1.0, "/s"
    if kind == "bytes":
        for div, suffix in ((1.0, " B/s"), (1024.0, " KB/s"), (1048576.0, " MB/s")):
            if top < div * 1024:
                return div, suffix
        return 1073741824.0, " GB/s"
    return 1.0, ""


def _fmt(spec: _Spec, shown: float, suffix: str) -> str:
    """A value in display units, with the chart's own precision."""
    if spec.kind == "pct":
        digits = spec.digits
    elif spec.kind == "rate":
        digits = 3 if shown < 0.1 else 2 if shown < 10 else 1
    elif spec.kind == "bytes":
        digits = 0 if suffix == " B/s" else 1
    else:
        digits = 0
    return f"{shown:.{digits}f}{suffix}"


# ---- one chart ------------------------------------------------------------------------
@dataclass(frozen=True)
class _Axis:
    low: float  # display units
    high: float
    div: float
    suffix: str


def _axis(spec: _Spec, shown: list[float]) -> _Axis:
    raw_top = max(shown) * 1.0 if shown else 0.0
    div, suffix = _unit(spec.kind, raw_top)
    if spec.axis == "unit":
        return _Axis(0.0, 100.0, div, suffix)
    if spec.axis == "zoom":
        low = min(shown) / div if shown else 100.0
        if spec.target:
            low = min(low, SLO_TARGET_PERCENT)
        for rng in _ZOOM_RANGES:
            if 100.0 - low <= rng * 0.92:
                return _Axis(100.0 - rng, 100.0, div, suffix)
        return _Axis(0.0, 100.0, div, suffix)
    top = (raw_top / div) * 1.1 if shown else 1.0
    return _Axis(0.0, _nice_top(top), div, suffix)


def _y(axis: _Axis, shown: float) -> float:
    frac = (shown - axis.low) / (axis.high - axis.low)
    frac = max(0.0, min(1.0, frac))
    return PLOT_BOTTOM - frac * (PLOT_BOTTOM - PLOT_TOP)


def _x(start: float, end: float, ts: float) -> float:
    frac = (ts - start) / (end - start)
    return PLOT_LEFT + max(0.0, min(1.0, frac)) * (PLOT_RIGHT - PLOT_LEFT)


def _x_ticks(start: float, end: float) -> list[tuple[float, str]]:
    """UTC gridlines: every 6 hours inside a day or two, else every day."""
    window = end - start
    if window <= 2 * 86400:
        every, fmt = 21600, "%H:%M"
    else:
        every, fmt = 86400, "%m-%d"
    first = math.ceil(start / every) * every
    out = []
    t = first
    while t <= end and len(out) < 12:
        out.append((t, datetime.fromtimestamp(t, UTC).strftime(fmt)))
        t += every
    return out


def _path(points: list[tuple[float, float] | None]) -> str:
    """A line with a break at every None. A single point is a zero-length stroke
    (drawn as a dot by the round line cap)."""
    parts: list[str] = []
    run: list[tuple[float, float]] = []

    def flush() -> None:
        if len(run) == 1:
            parts.append(f"M{_n(run[0][0])} {_n(run[0][1])}h0.1")
        elif run:
            head, *rest = run
            parts.append(f"M{_n(head[0])} {_n(head[1])}")
            parts.extend(f"L{_n(px)} {_n(py)}" for px, py in rest)
        run.clear()

    for p in points:
        if p is None:
            flush()
        else:
            run.append(p)
    flush()
    return "".join(parts)


def _ticks(start: float, end: float, marks: tuple[Deploy, ...] | None) -> list[Deploy]:
    if not marks:
        return []
    return [
        m
        for m in marks
        if isinstance(m.ts, int | float) and math.isfinite(m.ts) and start <= m.ts <= end
    ]


def _chart(
    spec: _Spec,
    data: object,
    marks: tuple[Deploy, ...] | None,
    snapshot_as_of: float,
) -> str:
    chart_id = f"chart-{spec.key}"
    ok = isinstance(data, SeriesData)
    window = (data.end - data.start) if ok else 0.0
    step = data.step if ok else 0.0

    # Display values, one per grid slot: None for a gap or garbage.
    raw: list[float | None] = []
    if ok:
        for v in data.values:
            if not _usable(v):
                raw.append(None)
            else:
                raw.append(max(0.0, min(1.0, 1.0 - v)) if spec.flip else float(v))
    present = [v for v in raw if v is not None]
    has_data = bool(present)

    head = f'<section class="chart" id="{chart_id}">\n<h3>{_e(spec.title)}</h3>\n'
    if ok and window > 0 and step > 0:
        span_words = f"{_words(window)}, one point every {_words(step)}"
    else:
        span_words = "no window"
    meta = f'<p class="meta">{_e(span_words)}.</p>\n' if ok else ""

    body: list[str] = []
    summary = ""
    desc_extra = ""
    if has_data:
        axis = _axis(spec, present)
        body.extend(_frame(axis, data.start, data.end, spec))
        body.extend(_nodata_zone(raw, data))
        last_x = -math.inf
        for d in _ticks(data.start, data.end, marks):
            x = _x(data.start, data.end, d.ts)
            if x - last_x < MIN_TICK_GAP:
                continue  # deploys minutes apart would paint a solid bar; the list has them all
            last_x = x
            text = _e(f"deploy {d.short} at {_stamp(d.ts)}")
            body.append(
                f'<g class="deploy"><title>{text}</title>'
                + _line("deploy-hit", x, PLOT_TOP, x, PLOT_BOTTOM)
                + _line("deploy", x, PLOT_TOP, x, PLOT_BOTTOM)
                + "</g>"
            )
        pts: list[tuple[float, float] | None] = []
        for i, v in enumerate(raw):
            if v is None:
                pts.append(None)
            else:
                ts = data.start + i * data.step
                pts.append((_x(data.start, data.end, ts), _y(axis, v / axis.div)))
        body.append(f'<path class="line" d="{_path(pts)}"/>')
        last_i = max(i for i, v in enumerate(raw) if v is not None)
        lx, ly = pts[last_i]  # type: ignore[misc]
        body.append(f'<circle class="last" cx="{_n(lx)}" cy="{_n(ly)}" r="3"/>')
        body.extend(_hover(spec, raw, data, axis))
        lo, hi, last = min(present), max(present), raw[last_i]
        shown = [_fmt(spec, v / axis.div, axis.suffix) for v in (lo, hi, last)]  # type: ignore[arg-type]
        summary = f'<p class="summary">min {shown[0]} | max {shown[1]} | last {shown[2]}</p>\n'
        desc_extra = f" Minimum {shown[0]}, maximum {shown[1]}, last {shown[2]}."
    else:
        body.append(
            f'<rect class="nodata" x="{_n(PLOT_LEFT)}" y="{_n(PLOT_TOP)}" '
            f'width="{_n(PLOT_RIGHT - PLOT_LEFT)}" height="{_n(PLOT_BOTTOM - PLOT_TOP)}"/>'
            f'<text class="empty" x="{_n((PLOT_LEFT + PLOT_RIGHT) / 2)}" '
            f'y="{_n((PLOT_TOP + PLOT_BOTTOM) / 2)}" text-anchor="middle">no data</text>'
        )
        summary = '<p class="summary">no data</p>\n'
        desc_extra = " No data."

    desc = (
        f"Line chart. {span_words}. {spec.blurb}{desc_extra}"
        if ok
        else (f"Line chart. {spec.blurb} No data.")
    )
    svg = (
        f'<svg class="chart-svg" viewBox="0 0 {VIEW_W} {VIEW_H}" role="img" '
        f'aria-labelledby="t-{spec.key} d-{spec.key}">'
        f'<title id="t-{spec.key}">{_e(spec.title)}</title>'
        f'<desc id="d-{spec.key}">{_e(desc)}</desc>' + "".join(body) + "</svg>\n"
    )

    notes: list[str] = []
    if spec.key == "rps":
        notes.append("This includes the in-cluster probe.")
    if has_data:
        have = len(present) * step  # points present, not the first-to-last span (review L5)
        if have < window - 2 * step:
            if window >= 2 * 86400:
                have_t, want_t, unit = have / 86400, window / 86400, "days"
            else:
                have_t, want_t, unit = have / 3600, window / 3600, "hours"
            have_text = "less than 0.1" if _trim(have_t) == "0" else _trim(have_t)
            notes.append(f"This chart has {have_text} of {_trim(want_t)} {unit} of data.")
    if ok and snapshot_as_of - data.as_of > STALE_CHART_SECONDS:
        notes.append(
            f"Last updated {_stamp(data.as_of)}: the newest refresh could not update this chart."
        )
    note_html = "".join(f'<p class="note">{_e(n)}</p>\n' for n in notes)
    return head + meta + svg + summary + note_html + "</section>\n"


def _frame(axis: _Axis, start: float, end: float, spec: _Spec) -> list[str]:
    """Gridlines, y labels, x labels, and the SLO target line."""
    out: list[str] = []
    step_y = (axis.high - axis.low) / _TICK_INTERVALS
    digits = _step_digits(step_y)
    for i in range(_TICK_INTERVALS + 1):
        value = axis.low + i * step_y
        y = _y(axis, value)
        out.append(
            _line("grid", PLOT_LEFT, y, PLOT_RIGHT, y)
            + f'<text class="tick-label" x="{_n(PLOT_LEFT - 6)}" y="{_n(y + 3.5)}" '
            f'text-anchor="end">{value:.{digits}f}{_e(axis.suffix)}</text>'
        )
    for ts, label in _x_ticks(start, end):
        x = _x(start, end, ts)
        out.append(
            _line("grid", x, PLOT_TOP, x, PLOT_BOTTOM)
            + f'<text class="tick-label" x="{_n(x)}" y="{_n(PLOT_BOTTOM + 15)}" '
            f'text-anchor="middle">{_e(label)}</text>'
        )
    out.append(
        f'<line class="axis" x1="{_n(PLOT_LEFT)}" y1="{_n(PLOT_BOTTOM)}" '
        f'x2="{_n(PLOT_RIGHT)}" y2="{_n(PLOT_BOTTOM)}"/>'
    )
    if spec.target and axis.low <= SLO_TARGET_PERCENT <= axis.high:
        y = _y(axis, SLO_TARGET_PERCENT)
        out.append(
            _line("target", PLOT_LEFT, y, PLOT_RIGHT, y)
            + f'<text class="target-label" x="{_n(PLOT_LEFT + 4)}" y="{_n(y - 4)}" '
            f'text-anchor="start">SLO target {SLO_TARGET_PERCENT:g}%</text>'
        )
    return out


def _nodata_zone(raw: list[float | None], data: SeriesData) -> list[str]:
    """Shade the part of the window before the first sample: history is shorter."""
    first = next(i for i, v in enumerate(raw) if v is not None)
    if first < max(2, len(raw) // 20):
        return []
    x2 = _x(data.start, data.end, data.start + first * data.step)
    width = x2 - PLOT_LEFT
    out = [
        f'<rect class="nodata" x="{_n(PLOT_LEFT)}" y="{_n(PLOT_TOP)}" '
        f'width="{_n(width)}" height="{_n(PLOT_BOTTOM - PLOT_TOP)}"/>'
    ]
    if width >= 70:
        out.append(
            f'<text class="empty" x="{_n(PLOT_LEFT + width / 2)}" '
            f'y="{_n(PLOT_TOP + 26)}" text-anchor="middle">no data yet</text>'
        )
    return out


def _hover(spec: _Spec, raw: list[float | None], data: SeriesData, axis: _Axis) -> list[str]:
    """Invisible circles with native <title> tooltips, at most MAX_HOVER_POINTS."""
    idx = [i for i, v in enumerate(raw) if v is not None]
    if len(idx) > MAX_HOVER_POINTS:
        stride = math.ceil(len(idx) / MAX_HOVER_POINTS)
        picked = idx[::stride]
        if picked[-1] != idx[-1]:
            picked.append(idx[-1])
        idx = picked
    out = []
    for i in idx:
        v = raw[i]
        ts = data.start + i * data.step
        text = _e(f"{_stamp(ts)}: {_fmt(spec, v / axis.div, axis.suffix)}")  # type: ignore[operator]
        out.append(
            f'<circle class="hit" cx="{_n(_x(data.start, data.end, ts))}" '
            f'cy="{_n(_y(axis, v / axis.div))}" r="6"><title>{text}</title></circle>'  # type: ignore[operator]
        )
    return out


# ---- the page -------------------------------------------------------------------------
_CSS = """
  :root { --bg: #fff; --fg: #1f2328; --muted: #59636e; --link: #0969da; --card: #f6f8fa;
          --line: #d1d9e0; --accent: #0969da; --accent-bg: #ddf4ff; --warn: #9a6700; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0a0c12; --fg: #e6edf3; --muted: #9aa4b2; --link: #6cb0ff; --card: #131722;
            --line: #2a3142; --accent: #6cb0ff; --accent-bg: #16253d; --warn: #d4a72c; }
  }
  * { box-sizing: border-box; }
  body { font: 16px/1.55 system-ui, sans-serif; margin: 0; color: var(--fg); background: var(--bg); }
  a { color: var(--link); }
  a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .wrap { max-width: 64rem; margin: 0 auto; padding: 0 1rem; }
  header { border-bottom: 1px solid var(--line); }
  nav { display: flex; flex-wrap: wrap; gap: .25rem 1.25rem; padding: .75rem 1rem; align-items: center; }
  nav .brand { font-weight: 600; margin-right: auto; color: var(--fg); text-decoration: none; }
  nav a { text-decoration: none; }
  nav a:hover { text-decoration: underline; }
  h1 { font-size: 1.9rem; margin: 2rem 0 .25rem; line-height: 1.2; }
  h2 { font-size: 1.15rem; margin: 2.25rem 0 .75rem; }
  h3 { font-size: 1rem; margin: 0 0 .1rem; }
  p.lead { color: var(--muted); margin-top: 0; font-size: 1.05rem; }
  code { font: .9em ui-monospace, monospace; background: var(--card); padding: .1em .3em;
         border-radius: 4px; }
  .data-note { color: var(--muted); font-size: .9rem; margin: .25rem 0; }
  .banner { color: var(--warn); border: 1px solid var(--warn); border-radius: 8px;
            padding: .5rem .75rem; font-weight: 600; }
  .charts { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(min(100%, 25rem), 1fr)); }
  .chart { border: 1px solid var(--line); border-radius: 8px; background: var(--card);
           padding: .75rem 1rem; min-width: 0; }
  .chart .meta, .chart .note { color: var(--muted); font-size: .85rem; margin: 0; }
  .chart .note { color: var(--warn); }
  .chart .summary { margin: .25rem 0 0; font-size: .9rem; font-variant-numeric: tabular-nums; }
  svg.chart-svg { width: 100%; height: auto; display: block; margin-top: .25rem; }
  svg .grid { stroke: var(--line); stroke-width: 1; }
  svg .axis { stroke: var(--muted); stroke-width: 1; }
  svg .line { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round;
              stroke-linecap: round; }
  svg .last { fill: var(--accent); }
  svg .hit { fill: transparent; }
  svg .hit:hover { fill: var(--accent); fill-opacity: .35; }
  svg line.deploy { stroke: var(--warn); stroke-width: 1; stroke-dasharray: 2 4; opacity: .85; }
  svg .deploy-hit { stroke: transparent; stroke-width: 9; }
  svg .nodata { fill: var(--line); fill-opacity: .45; }
  svg .target { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 5 4; }
  svg text { font: 12px system-ui, sans-serif; }
  svg .tick-label, svg .target-label { fill: var(--muted); }
  svg .empty { fill: var(--muted); font-size: 13px; }
  ul.deploys { padding-left: 1.25rem; }
  li { margin: .2rem 0; }
  footer { border-top: 1px solid var(--line); margin-top: 3rem; padding: 1rem 0 2rem;
           color: var(--muted); font-size: .85rem; }
  @media (max-width: 40rem) {
    h1 { font-size: 1.5rem; }
    nav .brand { flex-basis: 100%; }
    .chart { padding: .6rem .6rem; }
  }
"""  # noqa: E501  (CSS, one rule per line)

_HEAD = (
    '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<meta name="color-scheme" content="light dark">\n'
    "<title>Platform history: demo.sandeeprn.in</title>\n"
    f"<style>{_CSS}</style>\n</head>\n<body>\n"
    '<header>\n  <nav class="wrap" aria-label="Main">\n'
    '    <a class="brand" href="/">demo.sandeeprn.in</a>\n'
    '    <a href="/">Link shortener</a>\n'
    '    <a href="/status" aria-current="page">History</a>\n'
    '    <a href="/api/docs">API docs</a>\n'
    '    <a href="https://ask.sandeeprn.in">On-call assistant</a>\n'
    '    <a href="https://github.com/Sandeep-RN/oci-k8s-gitops-demo">Source</a>\n'
    '  </nav>\n</header>\n<main class="wrap">\n'
)
_FOOT = (
    '</main>\n<footer><div class="wrap">All times are UTC. Drawn on the server from the '
    "platform's own Prometheus; no script runs on this page.</div></footer>\n</body>\n</html>\n"
)


def render_warming() -> str:
    """Before the first successful refresh: a plain 200 page, never a 5xx."""
    return (
        _HEAD
        + "<h1>Platform history</h1>\n"
        + '<p class="lead">The charts are warming up, try again in a minute.</p>\n'
        + '<p class="data-note">The page keeps a copy of the last day and week from the '
        "platform's own Prometheus. It fills a few minutes after the service starts.</p>\n" + _FOOT
    )


def _deploys_section(data: object) -> str:
    if not isinstance(data, DeploysData):
        return (
            "<h2>Deploys</h2>\n"
            '<p class="data-note">Deploy markers unavailable: the query for them has not '
            "answered yet.</p>\n"
        )
    window = _words(data.end - data.start) if data.end > data.start else "the window"
    marks = [m for m in _ticks(data.start, data.end, data.marks)]
    head = (
        "<h2>Deploys</h2>\n"
        f'<p class="data-note">The backend image changed at these times, in the last {_e(window)}. '
        "A commit is shown as its first 7 characters. The dashed amber lines on the charts "
        "mark the same times.</p>\n"
    )
    if not marks:
        return head + f'<p class="data-note">No deploys in the last {_e(window)}.</p>\n'
    items = "".join(
        f"<li>{_e(_stamp(m.ts))}: <code>{_e(m.short)}</code></li>\n" for m in reversed(marks)
    )
    return head + f'<ul class="deploys">\n{items}</ul>\n'


def _safe_chart(
    spec: _Spec,
    snapshot: HistorySnapshot,
    marks: tuple[Deploy, ...] | None,
    errors: list[str] | None,
) -> str:
    """One chart, isolated: a bug while drawing it blanks that chart only (review M1).
    Only the exception TYPE is logged: its message could carry the data."""
    try:
        return _chart(spec, snapshot.charts.get(spec.key), marks, snapshot.as_of)
    except Exception as exc:
        log.warning("history chart %s could not be drawn: %s", spec.key, type(exc).__name__)
        if errors is not None:
            errors.append(spec.key)
        return _chart(spec, None, None, snapshot.as_of)  # constants only: "no data"


def render_page(
    snapshot: HistorySnapshot,
    *,
    stale: bool = False,
    refresh_seconds: float = 300.0,
    errors: list[str] | None = None,
) -> str:
    """The page. A chart that cannot be drawn shows "no data" and the others are
    untouched; its key is appended to `errors` (if given) so the caller can count it."""
    deploys = snapshot.charts.get("deploys")
    marks = deploys.marks if isinstance(deploys, DeploysData) else None

    def group(specs: tuple[_Spec, ...]) -> str:
        cells = "".join(_safe_chart(s, snapshot, marks, errors) for s in specs)
        return f'<div class="charts">\n{cells}</div>\n'

    banner = (
        '<p class="banner" id="history-stale">Prometheus has not answered for a while, so the '
        "numbers below may be out of date.</p>\n"
        if stale
        else ""
    )
    refresh = _words(refresh_seconds) if math.isfinite(refresh_seconds) else "a few minutes"
    return (
        _HEAD
        + "<h1>Platform history</h1>\n"
        + '<p class="lead">How the platform behaved over the last day and week, drawn from '
        "its own Prometheus.</p>\n"
        + banner
        + f'<p class="data-note">Data as of {_e(_stamp(snapshot.as_of))}. This copy is refreshed '
        f"about every {_e(refresh)}, and each replica of the service refreshes on its own, "
        "so a reload can show data a few minutes apart. A gap in a line is missing data, "
        "never zero.</p>\n"
        + "<h2>Redirect SLO</h2>\n"
        + group(SLO_CHARTS)
        + "<h2>Node and cluster</h2>\n"
        + group(NODE_CHARTS)
        + _deploys_section(deploys)
        + _FOOT
    )


def headers(*, ready: bool) -> dict[str, str]:
    return {
        "Cache-Control": CACHE_CONTROL if ready else NO_STORE,
        "Content-Security-Policy": CSP,
    }
