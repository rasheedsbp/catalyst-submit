"""APC lookup: source priority, statuses, currency, page parsing, display.

Network is mocked, so this runs offline:  python tests/test_apc.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from journal_traffic import apc  # noqa: E402

apc._LIVE_RATES = dict(apc.FALLBACK_USD_RATES)          # no live FX in tests


def _doaj(has_apc, prices=None, url="https://j.example/apc"):
    return {"bibjson": {"apc": {"has_apc": has_apc, "url": url,
                                "max": prices or []}}}


def test_doaj_price_wins_and_prefers_usd():
    with mock.patch.object(apc, "_doaj_record", return_value=_doaj(
            True, [{"price": 1800, "currency": "EUR"}, {"price": 2000, "currency": "USD"}])):
        info = apc.lookup({"issn_l": "1234-5678", "apc_usd": 999}, scrape=False)
    assert (info.apc_status, info.apc_amount, info.apc_currency, info.apc_source) == \
        (apc.STATUS_CHARGES, 2000.0, "USD", "DOAJ")
    assert "1800 EUR" in info.apc_note


def test_doaj_diamond_is_no_apc():
    with mock.patch.object(apc, "_doaj_record", return_value=_doaj(False)):
        info = apc.lookup({"issn_l": "1234-5678"}, scrape=False)
    assert info.apc_status == apc.STATUS_NO_APC and info.apc_usd_est == 0
    assert apc.display(info.as_row()) == "No APC (free)"
    assert (info.publishing_model, info.free_option) == (apc.MODEL_DIAMOND, apc.FREE_YES)


def test_openalex_fills_when_not_in_doaj_and_converts():
    with mock.patch.object(apc, "_doaj_record", return_value=None):
        info = apc.lookup({"issn_l": "1234-5678", "is_oa": True,
                           "apc_prices": [{"price": 2000, "currency": "GBP"}]},
                          scrape=False)
    assert info.apc_source == "OpenAlex" and info.apc_currency == "GBP"
    assert info.apc_usd_est == 2540
    assert apc.display(info.as_row()) == "GBP 2,000 (~$2,540)"
    assert (info.publishing_model, info.free_option) == (apc.MODEL_GOLD, apc.FREE_NO)


def test_doaj_yes_without_amount_takes_openalex_amount():
    with mock.patch.object(apc, "_doaj_record", return_value=_doaj(True)):
        info = apc.lookup({"issn_l": "1234-5678", "apc_usd": 1500}, scrape=False)
    assert info.apc_amount == 1500 and info.apc_source == "DOAJ + OpenAlex"
    assert info.apc_url == "https://j.example/apc"


def test_publisher_page_last_resort():
    page = apc.APCInfo(apc_status=apc.STATUS_CHARGES, apc_amount=2600,
                       apc_currency="CHF", apc_usd_est=2912, apc_source="Publisher page")
    with mock.patch.object(apc, "_doaj_record", return_value=None), \
         mock.patch.object(apc, "from_publisher_page", return_value=page):
        info = apc.lookup({"issn_l": "1234-5678", "homepage": "https://mdpi.com/j"})
    assert info.apc_source == "Publisher page" and info.apc_amount == 2600


def test_subscription_vs_unknown_when_nothing_found():
    with mock.patch.object(apc, "_doaj_record", return_value=None), \
         mock.patch.object(apc, "from_publisher_page", return_value=None):
        sub = apc.lookup({"issn_l": "1234-5678", "is_oa": False})
        oa = apc.lookup({"issn_l": "1234-5678", "is_oa": True})
    assert sub.apc_status == apc.STATUS_SUBSCRIPTION
    assert apc.display(sub.as_row()) == "Free (subscription route)"
    assert sub.free_option == apc.FREE_YES
    assert oa.apc_status == apc.STATUS_UNKNOWN


def test_hybrid_price_is_optional_with_free_route():
    # OpenAlex lists a price, but the journal is a subscription title
    with mock.patch.object(apc, "_doaj_record", return_value=None):
        info = apc.lookup({"issn_l": "1234-5678", "is_oa": False, "apc_usd": 3290,
                           "apc_prices": [{"price": 3290, "currency": "USD"}]},
                          scrape=False)
    assert info.publishing_model == apc.MODEL_HYBRID
    assert info.free_option == apc.FREE_YES
    assert apc.display(info.as_row()) == "USD 3,290 optional · free route"
    assert "subscription route" in info.free_option_note


def test_gold_oa_with_waiver():
    rec = _doaj(True, [{"price": 1200, "currency": "USD"}])
    rec["bibjson"]["waiver"] = {"has_waiver": True}
    with mock.patch.object(apc, "_doaj_record", return_value=rec):
        info = apc.lookup({"issn_l": "1234-5678", "is_oa": True}, scrape=False)
    assert (info.publishing_model, info.free_option) == (apc.MODEL_GOLD, apc.FREE_WAIVER)


def test_publisher_page_hybrid_wording():
    assert apc.HYBRID_PATTERN.search("Authors can choose to publish open access for a fee.")
    assert apc.HYBRID_PATTERN.search("This is a hybrid journal.")
    assert not apc.HYBRID_PATTERN.search("All articles are published open access.")


def test_parse_publisher_text():
    cases = {
        "The article processing charge (APC) is USD 2,390 for accepted papers.": (2390, "USD"),
        "APC: CHF 2600. Discounts are available.": (2600, "CHF"),
        "Authors pay an open access fee of €1.800 upon acceptance.": (1800, "EUR"),
        "£2,000 article processing charge applies": (2000, "GBP"),
    }
    for text, (amt, cur) in cases.items():
        a, c, _ = apc.parse_apc_text(text)
        assert (a, c) == (amt, cur), (text, a, c)
    a, c, free = apc.parse_apc_text("This journal does not charge any article processing charges.")
    assert a is None and free
    # a year or a volume number is not a price
    assert apc.parse_apc_text("Founded in 1998, Volume 12, APC policy under review")[0] is None


def test_apc_subpages_for_publisher():
    urls = apc.apc_urls("https://www.mdpi.com/journal/sensors")
    assert urls[0].endswith("/sensors/apc") and urls[-1].endswith("/sensors")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
