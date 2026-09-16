"""Near-real-time acceptance throughput from articles-in-press.

Why this module exists
----------------------
The main estimator divides published articles by acceptance rate. But a paper
published this month was submitted 8-18 months ago, so that estimate describes
last year's traffic.

Articles-in-press ("online first", "just accepted") are papers that have been
accepted but not yet assigned to an issue. They appear in Crossref within days
of acceptance. Counting them gives you acceptance activity from the last few
weeks — the closest available proxy for what a journal is doing right now.

Two things this gives you that the annual estimate cannot:

1. Current acceptance throughput (papers accepted per week), which combined
   with the acceptance rate implies current submission inflow.
2. Trend detection. A journal whose in-press count is climbing month over month
   is either expanding or slowing its issue assembly; either way its queue is
   changing and last year's number is wrong.

Run this weekly and it accumulates a genuine time series in ./data/traffic.db.
The first run tells you a level; the fourth tells you a direction.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import requests

CROSSREF = "https://api.crossref.org"

# See DEFAULT_CONTACT in sources.py — courtesy header, sends no mail.
DEFAULT_CONTACT = "rasheedphd123@gmail.com"
DB_PATH = Path("data/traffic.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS inpress_snapshots (
    issn            TEXT NOT NULL,
    journal         TEXT,
    snapshot_date   TEXT NOT NULL,
    inpress_count   INTEGER,
    deposited_28d   INTEGER,
    deposited_91d   INTEGER,
    median_lag_days REAL,
    PRIMARY KEY (issn, snapshot_date)
);
CREATE TABLE IF NOT EXISTS estimates (
    issn          TEXT NOT NULL,
    run_date      TEXT NOT NULL,
    submissions   INTEGER,
    evidence_tier TEXT,
    PRIMARY KEY (issn, run_date)
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


@dataclass
class InPressSnapshot:
    issn: str
    journal: str | None
    snapshot_date: str
    inpress_count: int
    deposited_28d: int
    deposited_91d: int
    median_lag_days: float | None

    @property
    def accepted_per_week(self) -> float:
        """Recent acceptance throughput. 91-day window smooths issue-batching."""
        return self.deposited_91d / 13.0

    def implied_annual_output(self) -> float:
        return self.accepted_per_week * 52


class CrossrefTracker:
    def __init__(self, mailto: str | None = None, pause: float = 0.15):
        self.session = requests.Session()
        self.mailto = mailto or DEFAULT_CONTACT
        self.pause = pause

    def _get(self, path: str, **params) -> dict:
        if self.mailto:
            params["mailto"] = self.mailto
        for attempt in range(4):
            r = self.session.get(f"{CROSSREF}{path}", params=params, timeout=30)
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            time.sleep(self.pause)
            return r.json()
        return {}

    def _count(self, issn: str, **filters) -> int:
        parts = [f"{k.replace('_', '-')}:{v}" for k, v in filters.items()]
        data = self._get(
            f"/journals/{issn}/works",
            filter=",".join(parts),
            rows=0,
        )
        return data.get("message", {}).get("total-results", 0)

    def snapshot(self, issn: str, journal: str | None = None) -> InPressSnapshot | None:
        """Count recently deposited research items and estimate acceptance lag."""
        issn = issn.strip()
        today = date.today()
        d28 = (today - timedelta(days=28)).isoformat()
        d91 = (today - timedelta(days=91)).isoformat()

        try:
            # Items deposited (i.e. registered with Crossref) recently. For most
            # publishers deposit follows acceptance within days.
            n28 = self._count(issn, type="journal-article", from_deposit_date=d28)
            n91 = self._count(issn, type="journal-article", from_deposit_date=d91)

            # Articles carrying no issue number yet are in-press.
            data = self._get(
                f"/journals/{issn}/works",
                filter=f"type:journal-article,from-deposit-date:{d91}",
                rows=100,
                select="issue,volume,published-online,published-print,created",
                sort="deposited",
                order="desc",
            )
        except requests.HTTPError:
            return None

        items = data.get("message", {}).get("items", [])
        if not items:
            return None

        in_press = sum(1 for it in items if not it.get("issue"))
        # Scale the sampled in-press proportion up to the full 91-day count.
        inpress_est = int(round(in_press / len(items) * n91))

        lags = []
        for it in items:
            online = it.get("published-online", {}).get("date-parts", [[None]])[0]
            print_d = it.get("published-print", {}).get("date-parts", [[None]])[0]
            if online and print_d and online[0] and print_d[0]:
                try:
                    o = datetime(*(online + [1, 1])[:3])
                    p = datetime(*(print_d + [1, 1])[:3])
                    lag = (p - o).days
                    if 0 < lag < 1000:
                        lags.append(lag)
                except (TypeError, ValueError):
                    continue

        return InPressSnapshot(
            issn=issn,
            journal=journal,
            snapshot_date=today.isoformat(),
            inpress_count=inpress_est,
            deposited_28d=n28,
            deposited_91d=n91,
            median_lag_days=float(np.median(lags)) if lags else None,
        )

    def record(self, conn: sqlite3.Connection, snap: InPressSnapshot) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO inpress_snapshots VALUES (?,?,?,?,?,?,?)",
            (
                snap.issn, snap.journal, snap.snapshot_date, snap.inpress_count,
                snap.deposited_28d, snap.deposited_91d, snap.median_lag_days,
            ),
        )
        conn.commit()


def trend(conn: sqlite3.Connection, issn: str, min_points: int = 3) -> dict:
    """Direction of travel from accumulated snapshots.

    Returns an empty dict until enough weekly runs exist — a single snapshot
    is a level, not a trend, and reporting one as a trend would mislead.
    """
    rows = conn.execute(
        "SELECT snapshot_date, deposited_91d, inpress_count FROM inpress_snapshots "
        "WHERE issn=? ORDER BY snapshot_date",
        (issn,),
    ).fetchall()

    if len(rows) < min_points:
        return {
            "status": "insufficient_history",
            "snapshots": len(rows),
            "need": min_points,
        }

    dates = [datetime.fromisoformat(r[0]) for r in rows]
    weeks = np.array([(d - dates[0]).days / 7 for d in dates])
    counts = np.array([r[1] for r in rows], dtype=float)

    slope, intercept = np.polyfit(weeks, counts, 1)
    pct = slope / counts.mean() * 100 if counts.mean() else 0.0

    if pct > 1.5:
        label = "expanding"
    elif pct < -1.5:
        label = "contracting"
    else:
        label = "stable"

    return {
        "status": "ok",
        "snapshots": len(rows),
        "span_weeks": round(float(weeks[-1]), 1),
        "latest_91d_output": int(counts[-1]),
        "weekly_change_pct": round(float(pct), 2),
        "direction": label,
        "latest_inpress": rows[-1][2],
    }
