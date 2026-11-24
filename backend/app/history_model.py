"""The numbers behind the /status page (demo #235, ADR 0027): plain, frozen data.

Kept apart from the refresher (history.py) and the renderer (history_page.py) so
that neither imports the other. Nothing in here is text from Prometheus: values are
finite numbers or None, and a deploy's commit is 7 hex characters that history.py
checked against a 40-hex pattern.
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SeriesData:
    """One chart line on a regular grid: values[i] is the value at start + i * step,
    or None for a gap. A gap is never a zero."""

    start: float
    end: float
    step: float
    values: tuple[float | None, ...]
    as_of: float  # wall clock of the refresh that produced it


@dataclass(frozen=True)
class Deploy:
    ts: float
    short: str  # 7 lowercase hex characters, from a 40-hex match


@dataclass(frozen=True)
class DeploysData:
    start: float
    end: float
    marks: tuple[Deploy, ...]
    as_of: float


@dataclass(frozen=True)
class HistorySnapshot:
    as_of: float
    # None: that chart has never had a good answer ("no data" on the page).
    charts: Mapping[str, SeriesData | DeploysData | None]
