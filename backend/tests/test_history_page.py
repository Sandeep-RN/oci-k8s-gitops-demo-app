# The /status page: server-rendered SVG built from numbers (demo #235, ADR 0027).
# Only numbers we format ourselves and constants may enter the markup. Nothing
# from Prometheus (no label, no string) may appear, and there is no script at all.
import math
import random
import re
from html.parser import HTMLParser

import pytest
from prometheus_client import CollectorRegistry

from app import history, history_page
from app.history import Deploy, DeploysData, HistoryService, HistorySnapshot, SeriesData
from app.status import RangeSeries
from tests.history_helpers import (
    NOW,
    SHA_A,
    SHA_B,
    SHA_C,
    FakeRange,
    default_range_answers,
    grid,
    image,
    spec,
)

DAY = 86400


class Parsed(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.texts: list[str] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        if data.strip():
            self.texts.append(data)


def snapshot_from(answers=None, now=NOW + 123.0):
    service = HistoryService(
        FakeRange(answers if answers is not None else default_range_answers(now)),
        registry=CollectorRegistry(),
        wall=lambda: now,
    )
    assert service.refresh_once()
    return service.read()


def render(snap=None, **kw):
    return history_page.render_page(snap if snap is not None else snapshot_from(), **kw)


def series_data(key, values, as_of=NOW + 123.0, now=NOW):
    start, end, step = grid(key, now)
    n = int((end - start) // step) + 1
    padded = tuple(values) + (None,) * (n - len(values))
    return SeriesData(start=start, end=end, step=step, values=padded[:n], as_of=as_of)


def full_snapshot_of(**overrides):
    base = snapshot_from()
    charts = dict(base.charts)
    charts.update(overrides)
    return HistorySnapshot(as_of=base.as_of, charts=charts)


# ---- the page as a whole --------------------------------------------------------------


def test_the_page_has_one_chart_per_series_and_no_script_at_all():
    page = render()
    assert page.count("<svg") == 8
    assert "<script" not in page.lower()
    assert not re.search(r"\son[a-z]+\s*=", page, re.IGNORECASE)
    assert "javascript:" not in page.lower()
    assert page.isascii()


def test_the_page_loads_nothing_from_elsewhere():
    page = render()
    for attr in ("src=", "<link", "@import", "<iframe", "<img", "<object", "<embed"):
        assert attr not in page.lower()
    assert not re.search(r"url\((?!#)", page.lower())
    hosts = set(re.findall(r"https?://([^/\s\"'<>)]+)", page))
    assert hosts <= {"github.com", "ask.sandeeprn.in"}


def test_the_page_has_the_nav_header_dark_mode_and_a_narrow_layout_like_the_landing_page():
    page = render()
    assert "<nav" in page and 'href="/"' in page and 'href="/api/docs"' in page
    assert "prefers-color-scheme: dark" in page and "#0a0c12" in page
    assert 'name="viewport"' in page and "@media (max-width" in page
    assert 'name="color-scheme"' in page
    assert 'lang="en"' in page


def test_every_chart_is_an_image_with_a_title_and_a_description():
    parsed = Parsed(render())
    svgs = [a for t, a in parsed.tags if t == "svg"]
    assert len(svgs) == 8
    for attrs in svgs:
        assert attrs["role"] == "img"
        ids = attrs["aria-labelledby"].split()
        assert len(ids) == 2
    titles = [a.get("id") for t, a in parsed.tags if t == "title" and a.get("id")]
    descs = [a.get("id") for t, a in parsed.tags if t == "desc"]
    assert len(titles) == 8 and len(descs) == 8
    for attrs in svgs:
        t_id, d_id = attrs["aria-labelledby"].split()
        assert t_id in titles and d_id in descs


def test_each_chart_says_its_window_and_step_in_words():
    page = render()
    assert page.count('<p class="meta">7 days, one point every 30 minutes.</p>') == 3
    assert page.count('<p class="meta">24 hours, one point every 5 minutes.</p>') == 5


def test_the_request_rate_chart_says_it_includes_the_probe():
    page = render()
    assert "includes the in-cluster probe" in page
    # and says so in the chart's own description, which a screen reader reads
    assert re.search(r"<desc[^>]*>[^<]*includes the in-cluster probe", page)


def test_each_chart_prints_min_max_and_last():
    page = render()
    assert page.count("min ") >= 8 and page.count("max ") >= 8 and page.count("last ") >= 8
    # cpu in the fake runs 10 to 21 percent
    assert "min 10" in page and "max 21" in page


def test_availability_is_one_minus_the_error_ratio():
    # errors 0.0012 -> availability 99.88 percent
    page = render()
    section = re.search(r'id="chart-errors".*?</section>', page, re.S).group(0)
    assert "99.88" in section
    assert "0.12%" not in section and "0.120%" not in section


def test_the_page_prints_the_data_time_and_the_refresh_note_without_fixed_numbers():
    snap = snapshot_from()
    five = render(snap, refresh_seconds=300.0)
    assert "2026-" in five and "UTC" in five  # the as-of time
    assert "about every 5 minutes" in five
    assert "about every 90 seconds" in render(snap, refresh_seconds=90.0)
    assert "about every 15 minutes" in render(snap, refresh_seconds=900.0)
    assert "about every 1 minute" in render(snap, refresh_seconds=60.0)


def test_a_stale_snapshot_says_so_and_a_fresh_one_does_not():
    snap = snapshot_from()
    assert "history-stale" not in render(snap, stale=False)
    stale = render(snap, stale=True)
    assert "history-stale" in stale and "has not answered" in stale


def test_the_warming_up_page_is_a_plain_page_with_no_chart_and_no_script():
    page = history_page.render_warming()
    assert "warming up" in page and "try again in a minute" in page
    assert "<svg" not in page and "<script" not in page.lower()
    assert "<nav" in page and page.isascii()


def test_the_csp_is_exactly_the_strictest_of_the_site_and_has_no_script_src():
    assert history_page.CSP == (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )
    assert "script-src" not in history_page.CSP


def test_the_page_is_small_enough_to_be_a_cached_string():
    page = render()
    assert len(page.encode()) < 150 * 1024


# ---- gaps, no data, partial data ------------------------------------------------------


def path_data(page, chart):
    section = re.search(rf'<section class="chart" id="chart-{chart}".*?</section>', page, re.S)
    assert section, chart
    return re.findall(r'<path class="line" d="([^"]*)"', section.group(0))


def test_a_gap_breaks_the_line_and_is_never_drawn_as_zero():
    values = [0.5, 0.6, None, None, 0.4, 0.5, None, 0.7]
    snap = full_snapshot_of(cpu=series_data("cpu", values))
    (d,) = path_data(render(snap), "cpu")
    assert d.count("M") == 3  # three runs: [0.5 0.6] [0.4 0.5] [0.7]
    # the y axis starts at 0: a gap drawn as zero would reach the bottom of the plot
    ys = [float(y) for y in re.findall(r"[ML]\s*[-\d.]+[ ,]([-\d.]+)", d)]
    assert len(ys) == 5 and max(ys) < history_page.PLOT_BOTTOM - 1


def test_a_series_that_is_all_gaps_says_no_data_and_has_no_line():
    snap = full_snapshot_of(cpu=series_data("cpu", []))
    page = render(snap)
    section = re.search(r'id="chart-cpu".*?</section>', page, re.S).group(0)
    assert "no data" in section
    assert path_data(page, "cpu") == []


def test_a_chart_whose_query_never_worked_says_no_data_and_still_has_its_frame():
    snap = full_snapshot_of(mem=None)
    page = render(snap)
    section = re.search(r'id="chart-mem".*?</section>', page, re.S).group(0)
    assert "no data" in section and "<svg" in section
    assert page.count("<svg") == 8  # the rest are untouched


def test_one_isolated_point_still_shows():
    snap = full_snapshot_of(pods=series_data("pods", [None, 41.0, None]))
    (d,) = path_data(render(snap), "pods")
    assert d.count("M") == 1 and "h" in d  # a zero-length run is drawn as a dot


def test_a_chart_with_less_data_than_its_window_says_how_much_it_has():
    start, end, step = grid("budget")
    n = int((end - start) // step) + 1
    have = round(n * 3.9 / 7)
    snap = full_snapshot_of(budget=series_data("budget", [0.9] * have))
    page = render(snap)
    section = re.search(r'id="chart-budget".*?</section>', page, re.S).group(0)
    assert re.search(r"has 3\.9 of 7 days of data", section)
    # a complete chart says nothing of the kind
    full = re.search(r'id="chart-cpu".*?</section>', page, re.S).group(0)
    assert "of data" not in full


def test_a_24_hour_chart_that_is_short_counts_in_hours():
    n_have = 12 * 6  # 6 hours at 5 minutes
    snap = full_snapshot_of(rps=series_data("rps", [None] * (289 - n_have) + [0.03] * n_have))
    section = re.search(r'id="chart-rps".*?</section>', render(snap), re.S).group(0)
    assert re.search(r"has 6(\.0)? of 24 hours of data", section)


def test_a_chart_that_kept_old_data_after_a_failed_refresh_says_how_old():
    old = series_data("cpu", [0.2] * 100, as_of=NOW - 1800)
    snap = HistorySnapshot(as_of=NOW + 123.0, charts={**snapshot_from().charts, "cpu": old})
    section = re.search(r'id="chart-cpu".*?</section>', render(snap), re.S).group(0)
    assert "last updated" in section.lower()
    assert (
        "last updated"
        not in re.search(r'id="chart-pods".*?</section>', render(snap), re.S).group(0).lower()
    )


# ---- tooltips and deploy ticks --------------------------------------------------------


def test_points_carry_native_hover_text_with_the_time_and_value():
    page = render()
    section = re.search(r'id="chart-cpu".*?</section>', page, re.S).group(0)
    hovers = re.findall(r'<circle class="hit"[^>]*><title>([^<]*)</title>', section)
    assert 10 <= len(hovers) <= history_page.MAX_HOVER_POINTS
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC: \d+(\.\d+)?%", hovers[0])


def test_hover_targets_are_capped_so_the_page_stays_small():
    section = re.search(r'id="chart-budget".*?</section>', render(), re.S).group(0)
    assert section.count('class="hit"') <= history_page.MAX_HOVER_POINTS


def ticks_in(page, chart):
    section = re.search(rf'<section class="chart" id="chart-{chart}".*?</section>', page, re.S)
    return re.findall(r'<g class="deploy"><title>([^<]*)</title>', section.group(0))


def test_deploy_ticks_appear_on_every_chart_where_they_are_in_range():
    page = render()
    # SHA_B and SHA_C (SHA_A was already running): both are older than 24 hours
    assert len(ticks_in(page, "budget")) == 2
    start, end, _ = grid("deploys")
    marks = (Deploy(ts=end - 5 * DAY, short="aaaaaaa"), Deploy(ts=end - 3600, short="bbbbbbb"))
    page = render(full_snapshot_of(deploys=DeploysData(start, end, marks, NOW + 123.0)))
    for chart in ("budget", "errors", "latency"):
        assert len(ticks_in(page, chart)) == 2, chart  # 7 days: both
    for chart in ("rps", "cpu", "mem", "net", "pods"):
        assert len(ticks_in(page, chart)) == 1, chart  # 24 hours: the recent one


def test_a_tick_outside_a_charts_window_is_left_off_that_chart():
    start, end, step = grid("deploys")
    marks = (Deploy(ts=end - 3 * DAY, short="aaaaaaa"), Deploy(ts=end - 2 * 3600, short="bbbbbbb"))
    snap = full_snapshot_of(deploys=DeploysData(start=start, end=end, marks=marks, as_of=NOW))
    page = render(snap)
    assert len(ticks_in(page, "budget")) == 2  # 7 days
    assert len(ticks_in(page, "cpu")) == 1  # 24 hours: only the recent one
    assert "bbbbbbb" in ticks_in(page, "cpu")[0]


def test_a_tick_has_hover_text_with_seven_hex_characters_and_a_time():
    page = render()
    for text in ticks_in(page, "pods"):
        assert re.fullmatch(r"deploy [0-9a-f]{7} at \d{4}-\d\d-\d\d \d\d:\d\d UTC", text)


def test_the_deploy_list_below_the_charts_has_the_same_markers():
    page = render()
    assert SHA_B[:7] in page and SHA_C[:7] in page
    assert SHA_B not in page and SHA_C not in page  # never the full tag
    assert "no deploys" not in page.lower()


def test_no_deploy_markers_says_so():
    start, end, _ = grid("deploys")
    snap = full_snapshot_of(deploys=DeploysData(start=start, end=end, marks=(), as_of=NOW))
    assert "no deploys" in render(snap).lower()


def test_a_failed_deploy_query_says_so_and_the_charts_still_render():
    page = render(full_snapshot_of(deploys=None))
    assert page.count("<svg") == 8
    assert "deploy markers unavailable" in page.lower()
    assert not ticks_in(page, "cpu")


# ---- nothing from Prometheus gets in ---------------------------------------------------

HOSTILE = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "10.1.2.3:9100",
    "backend-6d9f7c5b8-abcde",
    "node-1.internal.invalid",
    "CANARY-LABEL",
    "&lt;b&gt;",
    "javascript:alert(1)",
]


def test_no_label_pod_node_address_or_image_text_reaches_the_page():
    answers = default_range_answers(NOW + 123.0)
    start, _, step = grid("deploys")
    hostile = {f"k{i}": text for i, text in enumerate(HOSTILE)}
    hostile["image"] = "registry.example.invalid/CANARY-LABEL/backend:" + SHA_B
    answers[spec("deploys")[1]] = [
        RangeSeries(labels=hostile, points=((start + 10 * step, 1.0),)),
        *answers[spec("deploys")[1]],
    ]
    answers[spec("cpu")[1]] = [RangeSeries(labels=hostile, points=((start, 0.3),))]
    page = render(snapshot_from(answers))
    for text in (*HOSTILE, "registry.example.invalid"):
        assert text not in page, text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", page)  # no IPv4 address anywhere
    assert "<script" not in page.lower() and "onerror" not in page


def test_a_hostile_image_label_cannot_become_a_marker_even_with_html_in_it():
    answers = default_range_answers(NOW + 123.0)
    start, _, step = grid("deploys")
    evil = RangeSeries(
        labels={"image": "x/backend:<script>alert(1)</script>"},
        points=((start + 20 * step, 1.0),),
    )
    answers[spec("deploys")[1]] = [evil, *answers[spec("deploys")[1]]]
    page = render(snapshot_from(answers))
    assert "alert" not in page and "<script" not in page.lower()


def test_only_the_seven_hex_form_of_a_commit_is_ever_printed():
    page = render()
    assert not re.search(r"[0-9a-f]{8,}", re.sub(r"#[0-9a-fA-F]{3,8}\b", "", page))


# ---- property: the SVG contains only expected tags and attributes ----------------------

ALLOWED_TAGS = {
    "svg", "title", "desc", "g", "path", "line", "rect", "circle", "text",
}  # fmt: skip
ALLOWED_ATTRS = {
    "svg": {"viewbox", "role", "aria-labelledby", "class", "preserveaspectratio"},
    "title": {"id"},
    "desc": {"id"},
    "g": {"class"},
    "path": {"class", "d"},
    "line": {"class", "x1", "y1", "x2", "y2"},
    "rect": {"class", "x", "y", "width", "height"},
    "circle": {"class", "cx", "cy", "r"},
    "text": {"class", "x", "y", "text-anchor"},
}
NUMERIC = {"x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "width", "height"}
CLASSES = {
    "line", "hit", "deploy", "deploy-hit", "grid", "axis", "tick-label", "nodata", "now",
    "target", "target-label", "empty", "dot", "last", "area", "chart-svg",
}  # fmt: skip
NUM = re.compile(r"-?\d+(\.\d+)?")
PATH = re.compile(r"[MLh0-9., \-]*")
TEXT_OK = re.compile(r"[A-Za-z0-9 .,:;%/()+\-]*")


def svg_parts(page):
    return re.findall(r"<svg.*?</svg>", page, re.S)


def check_svg(svg):
    parsed = Parsed(svg)
    for tag, attrs in parsed.tags:
        assert tag in ALLOWED_TAGS, tag
        assert set(attrs) <= ALLOWED_ATTRS[tag], (tag, set(attrs) - ALLOWED_ATTRS[tag])
        for name, value in attrs.items():
            if name in NUMERIC:
                assert NUM.fullmatch(value), (name, value)
            elif name == "d":
                assert PATH.fullmatch(value), value
                assert "nan" not in value.lower() and "inf" not in value.lower()
            elif name == "class":
                assert set(value.split()) <= CLASSES, value
            elif name == "viewbox":
                assert re.fullmatch(r"0 0 \d+ \d+", value)
            elif name == "text-anchor":
                assert value in {"start", "middle", "end"}
    for text in parsed.texts:
        assert TEXT_OK.fullmatch(text), text


def random_values(rng, n):
    kinds = [None, None, math.nan, math.inf, -math.inf, 0.0, -1.0, 1e300, -1e300, 1e-300]
    out = []
    for _ in range(n):
        r = rng.random()
        if r < 0.15:
            out.append(rng.choice(kinds))
        else:
            out.append(rng.uniform(-5, 5) * 10 ** rng.randint(-6, 12))
    return tuple(out)


@pytest.mark.parametrize("seed", range(40))
def test_for_random_numbers_the_svg_has_only_expected_tags_attributes_and_text(seed):
    rng = random.Random(seed)
    charts = {}
    base = snapshot_from()
    for key in ("budget", "errors", "latency", "rps", "cpu", "mem", "net", "pods"):
        start, end, step = grid(key)
        n = int((end - start) // step) + 1
        roll = rng.random()
        if roll < 0.1:
            charts[key] = None
        else:
            charts[key] = SeriesData(
                start=start,
                end=end,
                step=step,
                values=random_values(rng, n),
                as_of=NOW - rng.randint(0, 99999),
            )
    start, end, _ = grid("deploys")
    charts["deploys"] = DeploysData(
        start=start,
        end=end,
        marks=tuple(
            Deploy(
                ts=rng.choice([math.nan, math.inf, start - 1e9, rng.uniform(start, end)]),
                short="abcdef0",
            )
            for _ in range(rng.randint(0, 12))
        ),
        as_of=NOW,
    )
    page = render(HistorySnapshot(as_of=base.as_of, charts=charts), stale=bool(seed % 2))
    svgs = svg_parts(page)
    assert len(svgs) == 8
    for svg in svgs:
        check_svg(svg)
    assert "<script" not in page.lower()


def test_the_checker_itself_notices_a_forbidden_tag_or_attribute():
    with pytest.raises(AssertionError):
        check_svg('<svg viewBox="0 0 1 1"><script>x</script></svg>')
    with pytest.raises(AssertionError):
        check_svg('<svg viewBox="0 0 1 1" onload="x"></svg>')
    with pytest.raises(AssertionError):
        check_svg('<svg viewBox="0 0 1 1"><path d="M nan 1"/></svg>')
    with pytest.raises(AssertionError):
        check_svg('<svg viewBox="0 0 1 1"><text x="1" y="1">&lt;b&gt;</text></svg>')


def test_a_flat_series_and_a_single_value_do_not_divide_by_zero():
    for key in ("cpu", "pods", "budget"):
        snap = full_snapshot_of(**{key: series_data(key, [0.5] * 50)})
        check_svg(svg_parts(render(snap))[0])
    snap = full_snapshot_of(cpu=series_data("cpu", [0.0] * 50))
    render(snap)


def test_the_page_is_the_same_for_the_same_snapshot():
    snap = snapshot_from()
    assert render(snap) == render(snap)
    assert history.HistorySnapshot is HistorySnapshot
    assert image(SHA_A) and SHA_C


def test_deploys_minutes_apart_do_not_paint_a_solid_bar_but_the_list_keeps_them_all():
    start, end, _ = grid("deploys")
    marks = tuple(Deploy(ts=end - 3600 + i * 60, short=f"{i:07x}") for i in range(30))
    page = render(full_snapshot_of(deploys=DeploysData(start, end, marks, NOW + 123.0)))
    assert 1 <= len(ticks_in(page, "budget")) < 30
    assert page.count("<li>") == 30
