"""Conference finder — offline tests (OpenAlex, WikiCFP and CORE are mocked).

Run:  python tests/test_conferences.py
"""
import os
import sys
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from journal_traffic import conferences as cf  # noqa: E402

FUTURE = date.today() + timedelta(days=60)
PAST = date.today() - timedelta(days=30)
WIKICFP_HTML = f"""
<table>
<tr bgcolor="#f6f6f6"><td rowspan="2"><a href="/cfp/servlet/event.showcfp?eventid=1&amp;copyownerid=2">WCNC 2027</a></td>
<td colspan="3">IEEE Wireless Communications and Networking Conference</td></tr>
<tr bgcolor="#f6f6f6"><td>Apr 5, 2027 - Apr 8, 2027</td><td>Lisbon, Portugal</td><td>{FUTURE.strftime('%b %d, %Y')}</td></tr>
<tr bgcolor="#e6e6e6"><td rowspan="2"><a href="/cfp/servlet/event.showcfp?eventid=9">NEWCONF 2027</a></td>
<td colspan="3">International Symposium on Sensor Swarms</td></tr>
<tr bgcolor="#e6e6e6"><td>N/A</td><td>Online</td><td>{FUTURE.strftime('%b %d, %Y')}</td></tr>
<tr bgcolor="#f6f6f6"><td rowspan="2"><a href="/cfp/servlet/event.showcfp?eventid=5">SmartGrid 2026</a></td>
<td colspan="3">International Conference on Smart Grid</td></tr>
<tr bgcolor="#f6f6f6"><td>Jan 3, 2026 - Jan 5, 2026</td><td>Paris</td><td>{PAST.strftime('%b %d, %Y')}</td></tr>
</table>"""

EVENT_HTML = """<table>
<tr><th>When</th><td>Apr 5, 2027 - Apr 8, 2027</td></tr>
<tr><th>Abstract Registration Due</th><td>Nov 1, 2026</td></tr>
<tr><th>Submission Deadline</th><td>%s</td></tr>
<tr><th>Notification Due</th><td>Jan 20, 2027</td></tr>
<tr><th>Final Version Due</th><td>Feb 10, 2027</td></tr>
</table>
Link: <a href="https://wcnc2027.ieee-wcnc.org/" target="_newtab">https://wcnc2027.ieee-wcnc.org/</a>
""" % (FUTURE + timedelta(days=7)).strftime('%b %d, %Y')


def _event(url):
    return cf.parse_wikicfp_event(EVENT_HTML) if "eventid=1" in url else {}


class FakeClient:
    def _get(self, path, **params):
        if path == "/works":
            return {"group_by": [
                {"key": "https://openalex.org/S1", "count": 40},
                {"key": "https://openalex.org/S2", "count": 12},
                {"key": "https://openalex.org/S3", "count": 9},
                {"key": "unknown", "count": 99}]}
        yr = date.today().year
        return {"results": [
            {"id": "https://openalex.org/S1",
             "display_name": "IEEE Wireless Communications and Networking Conference",
             "host_organization_name": "IEEE", "issn": [],
             "counts_by_year": [{"year": yr - 1, "works_count": 900}],
             "summary_stats": {"h_index": 120}},
            {"id": "https://openalex.org/S2",
             "display_name": "International Conference on Smart Grid",
             "host_organization_name": "IEEE",
             "counts_by_year": [{"year": yr - 1, "works_count": 150}]},
            {"id": "https://openalex.org/S3",
             "display_name": "WASET International Conference on Sensor Networks",
             "host_organization_name": "World Academy of Science, Engineering and Technology",
             "counts_by_year": [{"year": yr - 1, "works_count": 300}]},
        ]}


class FakeScopus:
    available = True

    def lookup(self, name, issns=None):
        return ("Yes", 2026) if "wireless" in name.lower() else ("Not found", None)

    def summary(self):
        return "fake scopus"


def _core(text):
    c = cf.CoreRanking.__new__(cf.CoreRanking)
    c.by_acronym, c.by_name = {}, {}
    c._parse(text)
    return c


CORE_CSV = ('1,IEEE Wireless Communications and Networking Conference,WCNC,ICORE2026,B,No,4602\n'
            '2,International Conference on Machine Learning,ICML,ICORE2026,A*,No,4611\n'
            '3,Some Local Workshop,SLW,ICORE2026,Unranked,No,4602\n')


def test_norm_and_acronym():
    assert cf.norm_name("2025 IEEE 12th International Conference on Smart Grid (SmartGrid 2025)") \
        == cf.norm_name("IEEE International Conference on Smart Grid")
    assert cf.acronym_of("2025 IEEE International Conference on Smart Grid (SmartGrid 2025)") == "SMARTGRID"
    assert cf.acronym_of("ICASSP 2026 - IEEE International Conference on Acoustics") == "ICASSP"


def test_wikicfp_parser():
    calls = cf.parse_wikicfp(WIKICFP_HTML)
    assert [c.acronym for c in calls] == ["WCNC 2027", "NEWCONF 2027", "SmartGrid 2026"]
    assert calls[0].where == "Lisbon, Portugal"
    assert calls[0].deadline_date == FUTURE.isoformat()
    assert calls[0].url.endswith("eventid=1&copyownerid=2")


def test_core_parser_ignores_unranked():
    core = _core(CORE_CSV)
    assert core.lookup("IEEE Wireless Communications and Networking Conference", "WCNC") == ("B", "ICORE2026")
    assert core.lookup("Some Local Workshop", "SLW") == (None, None)


def test_pipeline_ranks_merges_and_flags():
    with mock.patch.object(cf, "ScopusProceedings", return_value=FakeScopus()), \
         mock.patch.object(cf, "CoreRanking", return_value=_core(CORE_CSV)), \
         mock.patch.object(cf, "wikicfp_search", return_value=cf.parse_wikicfp(WIKICFP_HTML)), \
         mock.patch.object(cf, "wikicfp_event", side_effect=_event):
        confs = cf.find_conferences("t", terms=["wireless sensor networks"],
                                    client=FakeClient(), progress=lambda *_: None)
    top = confs[0]
    assert top.name.startswith("IEEE Wireless")
    assert (top.scopus_indexed, top.core_rank, top.next_where) == ("Yes", "B", "Lisbon, Portugal")
    # event page: extended deadline, notification and the official website
    assert top.days_to_deadline == 67 and top.acronym == "WCNC"
    assert top.website == "https://wcnc2027.ieee-wcnc.org/"
    assert top.notification == "Jan 20, 2027" and top.abstract_deadline == "Nov 1, 2026"
    assert top.status == "Open"
    # Smart Grid's only call is past its deadline: omitted entirely
    assert not any("Smart Grid" in c.name for c in confs)
    waset = next(c for c in confs if "WASET" in c.name)
    assert any("predatory" in f for f in waset.flags)
    assert waset.score < top.score


def test_filters():
    with mock.patch.object(cf, "ScopusProceedings", return_value=FakeScopus()), \
         mock.patch.object(cf, "CoreRanking", return_value=_core(CORE_CSV)), \
         mock.patch.object(cf, "wikicfp_search", return_value=cf.parse_wikicfp(WIKICFP_HTML)), \
         mock.patch.object(cf, "wikicfp_event", side_effect=_event):
        kw = dict(terms=["x"], client=FakeClient(), progress=lambda *_: None)
        assert len(cf.find_conferences("t", require_scopus=True, **kw)) == 1
        waset_kept = cf.find_conferences("t", **kw)
        assert any(c.status == "No call announced" for c in waset_kept)
        assert len(cf.find_conferences("t", upcoming_only=True, **kw)) == 1
        assert cf.find_conferences("t", min_rank="A", **kw) == []
        calls = cf.upcoming_calls(["x"])
    assert {c.acronym for c in calls} == {"WCNC 2027", "NEWCONF 2027"}   # past one dropped
    assert next(c for c in calls if c.acronym == "WCNC 2027").website.startswith("https://wcnc")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
