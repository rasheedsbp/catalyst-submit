"""Fast-acceptance: parsing stated figures, banding, filter and speed ranking.

Run:  python tests/test_speed.py
"""
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from journal_traffic import speed  # noqa: E402


def test_parse_stated_figures():
    t = ("Journal statistics. Time to First Decision: 18.2 days. "
         "Submission to acceptance 5 weeks. Acceptance to Publication: 3.5 days.")
    got = speed.parse_stated_speed(t)
    assert got["stated_days_to_first_decision"] == 18.2
    assert got["stated_days_to_acceptance"] == 35.0
    assert got["stated_days_accept_to_publication"] == 3.5
    assert speed.parse_stated_speed("Founded 1998. Volume 12 issue 4.") == {}


def test_measured_beats_stated_and_bands():
    r = speed.classify({"review_days_median": 62, "stated_days_to_acceptance": 20,
                        "production_days": 15})
    assert (r["acceptance_days"], r["acceptance_speed"], r["speed_source"]) == \
        (62, speed.BAND_FAST, "Measured (Crossref dates)")
    assert r["days_to_publication"] == 77
    r = speed.classify({"stated_days_to_acceptance": 30})
    assert (r["acceptance_speed"], r["speed_source"]) == (speed.BAND_VERY_FAST, "Stated by journal")
    assert speed.classify({"review_days_median": 8})["acceptance_speed"] == speed.BAND_SUSPICIOUS
    r = speed.classify({"stated_days_to_first_decision": 12})
    assert r["acceptance_days"] is None and r["speed_source"] == "Stated first decision only"
    assert speed.display(r) == "1st decision ~12 d"


def test_is_fast_excludes_unknown_and_suspicious():
    assert speed.is_fast({"acceptance_days": 60, "acceptance_speed": "Fast"}, 90)
    assert not speed.is_fast({"acceptance_days": 120, "acceptance_speed": "Moderate"}, 90)
    assert not speed.is_fast({"acceptance_days": None}, 90)
    assert not speed.is_fast({"acceptance_days": 9, "acceptance_speed": speed.BAND_SUSPICIOUS}, 90)


def test_recommend_sorts_by_speed():
    from journal_traffic.sources import JournalRecord
    from journal_traffic.model import build_features, estimate_traffic
    from journal_traffic.lag import apply_lag_correction
    from journal_traffic.recommend import recommend, format_shortlist
    rows = []
    for i, days in enumerate([150, 40, None, 70]):
        r = asdict(JournalRecord(openalex_id=f"S{i}", display_name=f"J{i}",
                                 issn_l=f"1111-000{i}", publisher=f"Pub{i}",
                                 research_works_by_year={2023: 120, 2024: 130},
                                 works_by_year={2024: 140}, cited_by_count=4000,
                                 h_index=30, two_yr_mean_citedness=2.0))
        r["quartile"] = 2
        r["review_days_median"] = days
        r.update(speed.classify(r))
        rows.append(r)
    res = apply_lag_correction(estimate_traffic(build_features(rows), None), 2025)
    rec = recommend(res, top_n=10, require_scopus=False, sort_by="speed")
    assert list(rec["display_name"]) == ["J1", "J3", "J0", "J2"]
    assert "Acceptance time: ~40 d · Very fast" in format_shortlist(rec)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
