"""Service catalogue and enquiry storage.

Every enquiry is a row with a status the admin can move along, so this doubles
as a lightweight CRM: nothing is lost between someone clicking "Request this
service" and the work being invoiced.

Visitor details are collected once, on the request form. There is deliberately
no tracking beyond what a person types in themselves — researchers hand this
tool unpublished manuscripts, and quiet data collection would be both wrong and,
once noticed, fatal to the tool's usefulness as an introduction.
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("JF_DB", "data/leads.db")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")

STATUSES = ["New", "Contacted", "Quoted", "In progress", "Won", "Closed"]

CATALOG = [
    {
        "id": "stats",
        "icon": "📊",
        "title": "Statistical Analysis",
        "short": "SPSS · AMOS · STATA · SmartPLS",
        "blurb": "Model specification, assumption testing, SEM and mediation "
                 "analysis, with results written up in publishable form.",
        "details": ["Descriptive and inferential analysis",
                    "SEM, CFA and path analysis in AMOS or SmartPLS",
                    "Reliability, validity and common-method bias testing",
                    "Interpretation written into your results section"],
    },
    {
        "id": "code",
        "icon": "💻",
        "title": "Python & MATLAB Implementation",
        "short": "Algorithms · simulation · figures",
        "blurb": "Implementation of the method in your paper, from a "
                 "description or a published algorithm, with reproducible "
                 "results and publication-quality figures.",
        "details": ["Machine learning and deep learning pipelines",
                    "MATLAB / Simulink models for power and control systems",
                    "Comparison against baselines, with tables and plots",
                    "Commented code you can defend in a viva"],
    },
    {
        "id": "manuscript",
        "icon": "📝",
        "title": "Manuscript Preparation",
        "short": "Drafting · restructuring · language editing",
        "blurb": "Full drafting or restructuring of a paper for IEEE, "
                 "Springer, Elsevier, Scopus and SCI/SCIE venues, including "
                 "abstract, structure and academic register.",
        "details": ["Drafting from results, notes or a rough draft",
                    "Restructuring to the target journal's expected sections",
                    "Structured abstracts and keyword selection",
                    "Plagiarism and AI-detection checks before submission"],
    },
    {
        "id": "journal",
        "icon": "🎯",
        "title": "Journal Selection & Submission",
        "short": "Finding the right venue, and getting it there",
        "blurb": "Shortlisting realistic journals by scope, indexing and "
                 "turnaround, then handling formatting and the submission "
                 "portal itself.",
        "details": ["Scope and indexing check (Scopus, SCI/SCIE, UGC-CARE)",
                    "Realistic assessment of acceptance odds",
                    "Cover letter and submission portal handling",
                    "Formatting to the journal's exact template"],
    },
    {
        "id": "response",
        "icon": "🔄",
        "title": "Reviewer Response Support",
        "short": "Major and minor revisions",
        "blurb": "Point-by-point responses to reviewers and the revisions "
                 "they ask for, including additional analysis where a "
                 "reviewer demands it.",
        "details": ["Point-by-point response document",
                    "Revisions tracked so reviewers can see the changes",
                    "Additional experiments or analysis if required",
                    "Resubmission handling"],
    },
    {
        "id": "thesis",
        "icon": "🎓",
        "title": "Thesis & Synopsis",
        "short": "PhD synopsis, chapters and full thesis",
        "blurb": "Synopsis preparation for registration, chapter development, "
                 "and full thesis compilation with formatting to university "
                 "guidelines.",
        "details": ["Research proposal and synopsis for registration",
                    "Chapter-by-chapter development",
                    "Literature review and research gap identification",
                    "University-specific formatting and plagiarism compliance"],
    },
    {
        "id": "figures",
        "icon": "📈",
        "title": "Figures & Tables",
        "short": "Redrawn to journal specification",
        "blurb": "Publication-quality figures redrawn at the required "
                 "resolution, and complex tables restructured to fit the "
                 "journal's column format.",
        "details": ["Vector figures at 300+ DPI",
                    "Architecture and flow diagrams",
                    "Wide tables restructured or split to fit",
                    "Consistent styling across the whole paper"],
    },
    {
        "id": "book",
        "icon": "📚",
        "title": "Book Publishing — Tri-Crest Press",
        "short": "Thesis to book · ISBN · Amazon KDP",
        "blurb": "Conversion of a completed thesis into a published book, "
                 "with ISBN registration and distribution through Amazon KDP.",
        "details": ["Thesis-to-book restructuring and editing",
                    "Cover design and interior typesetting",
                    "ISBN registration",
                    "Amazon KDP listing and distribution"],
    },
]

BY_ID = {s["id"]: s for s in CATALOG}

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    service     TEXT NOT NULL,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    phone       TEXT,
    institution TEXT,
    country     TEXT,
    role        TEXT,
    domain      TEXT,
    journal     TEXT,
    deadline    TEXT,
    message     TEXT,
    source      TEXT,
    status      TEXT NOT NULL DEFAULT 'New',
    notes       TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    journal  TEXT,
    figures  INTEGER,
    tables   INTEGER,
    words    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_req_status ON requests(status);
"""


@contextmanager
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def submit(service: str, form: dict, source: str = "services") -> tuple[bool, str]:
    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip().lower()
    if len(name) < 2:
        return False, "Please enter your name."
    if not EMAIL_RE.match(email):
        return False, "Please enter a valid email address."
    if service not in BY_ID:
        return False, "Please choose a service."
    with db() as conn:
        conn.execute(
            "INSERT INTO requests (at, service, name, email, phone, institution,"
            " country, role, domain, journal, deadline, message, source, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'New')",
            (_now(), service, name[:120], email[:160],
             (form.get("phone") or "")[:40],
             (form.get("institution") or "")[:160],
             (form.get("country") or "")[:80],
             (form.get("role") or "")[:60],
             (form.get("domain") or "")[:120],
             (form.get("journal") or "")[:160],
             (form.get("deadline") or "")[:60],
             (form.get("message") or "")[:4000], source))
    # Notify immediately. A researcher who has just asked for help is warm;
    # a day later they have emailed three other consultancies. Never let a
    # mail failure lose the request itself.
    try:
        from . import notify
        if notify.configured():
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM requests ORDER BY id DESC LIMIT 1").fetchone()
            notify.alert_new_request(
                dict(row) if row else {"name": name, "email": email},
                BY_ID[service]["title"])
    except Exception:
        pass

    return True, ("Thank you — your request has been received. "
                  "We usually reply within one working day.")


def log_run(journal: str, figures: int, tables: int, words: int) -> None:
    """Anonymous counts only: no filenames, no content, nothing identifying."""
    try:
        with db() as conn:
            conn.execute("INSERT INTO runs (at, journal, figures, tables, words)"
                         " VALUES (?,?,?,?,?)",
                         (_now(), journal, figures, tables, words))
    except Exception:
        pass


def all_requests(status: str | None = None) -> list[dict]:
    q = "SELECT * FROM requests"
    args: tuple = ()
    if status and status != "All":
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY at DESC"
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def update_request(req_id: int, status: str, notes: str) -> None:
    with db() as conn:
        conn.execute("UPDATE requests SET status=?, notes=? WHERE id=?",
                     (status, notes, req_id))


def stats() -> dict:
    with db() as conn:
        runs = conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
        reqs = conn.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"]
        new = conn.execute("SELECT COUNT(*) c FROM requests WHERE status='New'"
                           ).fetchone()["c"]
        won = conn.execute("SELECT COUNT(*) c FROM requests WHERE status='Won'"
                           ).fetchone()["c"]
        by_service = conn.execute(
            "SELECT service, COUNT(*) c FROM requests GROUP BY service "
            "ORDER BY c DESC").fetchall()
        by_journal = conn.execute(
            "SELECT journal, COUNT(*) c FROM runs GROUP BY journal "
            "ORDER BY c DESC LIMIT 8").fetchall()
    return {"runs": runs, "requests": reqs, "new": new, "won": won,
            "by_service": [(BY_ID.get(r["service"], {}).get("title", r["service"]),
                            r["c"]) for r in by_service],
            "by_journal": [(r["journal"], r["c"]) for r in by_journal]}


def daily_activity(days: int = 30) -> list[dict]:
    """Per-day counts, newest first — the "what happened when" view."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db() as conn:
        reqs = conn.execute(
            "SELECT substr(at,1,10) d, COUNT(*) c FROM requests "
            "WHERE at >= ? GROUP BY d", (since,)).fetchall()
        runs = conn.execute(
            "SELECT substr(at,1,10) d, COUNT(*) c FROM runs "
            "WHERE at >= ? GROUP BY d", (since,)).fetchall()
    r = {x["d"]: x["c"] for x in reqs}
    u = {x["d"]: x["c"] for x in runs}
    days_set = sorted(set(r) | set(u), reverse=True)
    return [{"date": d, "requests": r.get(d, 0), "formatted": u.get(d, 0)}
            for d in days_set]


def requests_on(day: str) -> list[dict]:
    with db() as conn:
        return [dict(x) for x in conn.execute(
            "SELECT * FROM requests WHERE substr(at,1,10)=? ORDER BY at DESC",
            (day,)).fetchall()]


def to_csv() -> str:
    import csv
    import io
    rows = all_requests()
    cols = ["at", "status", "service", "name", "email", "phone", "institution",
            "country", "role", "domain", "journal", "deadline", "message",
            "source", "notes"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
