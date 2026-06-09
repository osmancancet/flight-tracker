"""Yeni özellik testleri: flag parse, URL (pax/cabin), esnek tarih, PNG grafik, DB kalıcılık."""
import os
import tempfile

import pytest

from bot import parse_flags, _parse_airports
from charts import render_price_chart
from database import Database
from models import Route, expand_airport
from predictor import predict, BUY, WAIT, UNKNOWN
from scraper import FlightScraper
from tracker import candidate_dates


# ------------------------------------------------------------- flag parse
def test_parse_flags_defaults():
    pos, flags = parse_flags(["IST", "LON", "15-08-2026", "3000"])
    assert pos == ["IST", "LON", "15-08-2026", "3000"]
    assert flags == {"pax": 1, "cabin": "economy", "flex": 0, "nights": 0, "pos": None,
                     "near": False, "direct": False, "error": None}


def test_parse_flags_pos():
    _, flags = parse_flags(["x", "pos=gb,de"])
    assert flags["pos"] == "GB,DE" and flags["error"] is None
    _, bad = parse_flags(["x", "pos=GB,XX"])
    assert bad["error"] is not None


def test_parse_flags_full():
    pos, flags = parse_flags(["IST", "LON", "15-08-2026", "3000",
                              "pax=2", "cabin=business", "flex=3"])
    assert pos == ["IST", "LON", "15-08-2026", "3000"]
    assert flags["pax"] == 2 and flags["cabin"] == "business" and flags["flex"] == 3
    assert flags["error"] is None


def test_parse_flags_turkish_aliases():
    _, flags = parse_flags(["x", "yolcu=3", "kabin=ekonomi", "esnek=2"])
    assert flags["pax"] == 3 and flags["cabin"] == "economy" and flags["flex"] == 2


@pytest.mark.parametrize("bad", ["pax=0", "pax=10", "cabin=lux", "flex=9", "pax=abc"])
def test_parse_flags_errors(bad):
    _, flags = parse_flags(["IST", "LON", "15-08-2026", "3000", bad])
    assert flags["error"] is not None


# --------------------------------------------------------------- build_url
def test_build_url_passengers_and_cabin():
    url = FlightScraper.build_url("IST", "LON", "2026-08-15", None, 3, "business")
    assert "3+adults" in url and "business+class" in url


def test_build_url_economy_single_no_extras():
    url = FlightScraper.build_url("IST", "LON", "2026-08-15", None, 1, "economy")
    assert "adults" not in url and "class" not in url


def test_build_url_pos_gl_curr():
    url = FlightScraper.build_url("IST", "LON", "2026-08-15", gl="GB", curr="TRY")
    assert "gl=GB" in url and "curr=TRY" in url


async def test_compare_pos_sorts_and_handles_missing():
    # Sahte scraper: ülkeye göre fiyat; biri None
    class Scr(FlightScraper):
        def __init__(self): pass
        async def fetch_cheapest(self, o, d, date, return_date=None, passengers=1,
                                 cabin="economy", gl=None, curr="TRY"):
            from models import FlightResult
            price = {"TR": 9000, "GB": 8200, "DE": None}.get(gl)
            return FlightResult(price=price) if price else None
    r = Route(1, 1, "IST", "LON", "2026-08-15", 3000)
    out = await Scr().compare_pos(r, ["TR", "GB", "DE"], jitter=(0, 0))
    assert out[0] == ("GB", 8200) and out[1] == ("TR", 9000)
    assert out[2][0] == "DE" and out[2][1] is None  # fiyatsız sona


# ------------------------------------------------------------- candidate_dates
def test_candidate_dates_no_flex():
    r = Route(1, 1, "IST", "LON", "2026-08-15", 3000, flex_days=0)
    assert candidate_dates(r, today="2026-01-01") == [("2026-08-15", None)]


def test_candidate_dates_flex_window():
    r = Route(1, 1, "IST", "LON", "2026-08-15", 3000, flex_days=2)
    pairs = candidate_dates(r, today="2026-01-01")
    deps = [d for d, _ in pairs]
    assert deps == ["2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17"]


def test_candidate_dates_roundtrip_shift_keeps_duration():
    r = Route(1, 1, "IST", "LON", "2026-08-15", 5000, return_date="2026-08-22", flex_days=1)
    pairs = candidate_dates(r, today="2026-01-01")
    # her çiftte süre 7 gün korunmalı
    for dep, ret in pairs:
        assert ret is not None
    assert pairs[0] == ("2026-08-14", "2026-08-21")
    assert pairs[-1] == ("2026-08-16", "2026-08-23")


def test_candidate_dates_filters_past():
    r = Route(1, 1, "IST", "LON", "2026-08-15", 3000, flex_days=3)
    pairs = candidate_dates(r, today="2026-08-15")  # bugün = gidiş; geçmiş günler elenir
    assert all(d >= "2026-08-15" for d, _ in pairs)
    assert pairs[0][0] == "2026-08-15"


# ----------------------------------------------------------------- chart
def test_render_price_chart_returns_png():
    points = [("2026-06-01 10:00:00", 3200), ("2026-06-01 16:00:00", 2900),
              ("2026-06-02 10:00:00", 3050)]
    png = render_price_chart("IST→LON 15-08-2026", points, threshold=3000)
    assert isinstance(png, (bytes, bytearray)) and len(png) > 1000
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG imzası


# -------------------------------------------------------------- db persist
@pytest.fixture
async def db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    d = Database(path)
    await d.init()
    return d


async def test_route_extras_persisted(db):
    rid = await db.add_route(Route(None, 1, "IST", "LON", "2026-08-15", 3000,
                                   passengers=2, cabin="business", flex_days=3))
    r = await db.get_route(rid, 1)
    assert r.passengers == 2 and r.cabin == "business" and r.flex_days == 3
    assert "business" in r.label() and "±3g" in r.label()


async def test_price_series(db):
    rid = await db.add_route(Route(None, 1, "IST", "LON", "2026-08-15", 3000))
    from models import FlightResult
    for p in (3000, 2800):
        await db.record_price(rid, FlightResult(price=p))
    series = await db.price_series(rid)
    assert len(series) == 2 and series[0][1] == 3000 and series[1][1] == 2800


async def test_cheapest_since(db):
    rid = await db.add_route(Route(None, 1, "IST", "LON", "2026-08-15", 3000))
    from models import FlightResult
    assert await db.cheapest_since(rid, 24) is None  # kayıt yok
    for p in (3000, 2500, 2800):
        await db.record_price(rid, FlightResult(price=p))
    # Hepsi az önce kaydedildi → 1/6/24 saat penceresinde en ucuz 2500
    assert await db.cheapest_since(rid, 1) == 2500
    assert await db.cheapest_since(rid, 6) == 2500
    assert await db.cheapest_since(rid, 24) == 2500


def test_notifier_windows_line():
    from notifier import Notifier
    from models import FlightResult
    r = Route(3, 1, "IST", "LON", "2026-08-15", 3000)
    text = Notifier._format(r, FlightResult(price=2500, airline="THY", link="x"),
                            windows={1: 2500, 6: 2400, 24: 2300})
    assert "1s: 2500" in text and "6s: 2400" in text and "24s: 2300" in text
    # windows None ise satır görünmez
    assert "En ucuz —" not in Notifier._format(r, FlightResult(price=2500))


def test_notifier_pos_best_line():
    from notifier import Notifier
    from models import FlightResult
    r = Route(3, 1, "IST", "LON", "2026-08-15", 3000)
    text = Notifier._format(r, FlightResult(price=9000, link="x"),
                            pos_best=("İngiltere", 8200))
    assert "İngiltere" in text and "8200" in text and "satış noktası" in text


async def test_pos_persisted(db):
    rid = await db.add_route(Route(None, 1, "IST", "LON", "2026-08-15", 3000, pos="GB,DE"))
    r = await db.get_route(rid, 1)
    assert r.pos == "GB,DE" and r.pos_codes() == ["GB", "DE"]


# --------------------------------------------------------- multi-airport
def test_parse_airports_list_and_validation():
    codes, err = _parse_airports("IST,SAW,ADB,ESB")
    assert codes == ["IST", "SAW", "ADB", "ESB"] and err is None
    _, bad = _parse_airports("IST,XX1")
    assert bad is not None


def test_parse_airports_near_expands_metro():
    codes, err = _parse_airports("IST", near=True)
    assert "IST" in codes and "SAW" in codes and err is None


def test_expand_airport():
    assert set(expand_airport("IST")) == {"IST", "SAW"}
    assert expand_airport("BEG") == ["BEG"]


def test_route_multi_airport_label_and_lists():
    r = Route(1, 1, "IST,SAW", "BEG", "2026-08-15", 3000)
    assert r.origin_list() == ["IST", "SAW"] and r.dest_list() == ["BEG"]
    assert r.primary_origin() == "IST"
    assert "IST/SAW→BEG" in r.label()


async def test_multi_airport_persisted(db):
    rid = await db.add_route(Route(None, 1, "IST,SAW,ADB", "BEG", "2026-08-15", 3000))
    r = await db.get_route(rid, 1)
    assert r.origin_list() == ["IST", "SAW", "ADB"]


# ------------------------------------------------------------- predictor
def test_predict_unknown_with_few_points():
    assert predict([3000, 2900])[0] == UNKNOWN


def test_predict_buy_at_low():
    # son fiyat kayıtlı en düşüğe yakın
    assert predict([4000, 3800, 3500, 3200, 3010, 3000])[0] == BUY


def test_predict_wait_at_high():
    # son fiyat geçmişe göre tepe bölgede ve yükseliş
    assert predict([2000, 2200, 2500, 2800, 3000, 3200])[0] == WAIT


# ----------------------------------------------------------- tarih aralığı
def test_parse_date_range_single_and_range():
    from bot import _parse_date_range
    assert _parse_date_range("15-08-2026") == ("2026-08-15", None, None)
    s, e, err = _parse_date_range("10-07-2026..15-07-2026")
    assert s == "2026-07-10" and e == "2026-07-15" and err is None
    # ters aralık hata verir
    assert _parse_date_range("15-07-2026..10-07-2026")[2] is not None


def test_candidate_dates_range_oneway():
    r = Route(1, 1, "IST", "BEG", "2026-07-10", 3000, date_end="2026-07-12")
    pairs = candidate_dates(r, today="2026-01-01")
    assert [d for d, _ in pairs] == ["2026-07-10", "2026-07-11", "2026-07-12"]
    assert all(ret is None for _, ret in pairs)


def test_candidate_dates_range_roundtrip_cartesian():
    r = Route(1, 1, "IST", "BEG", "2026-07-10", 5000,
              return_date="2026-07-20", date_end="2026-07-11", return_date_end="2026-07-21")
    pairs = candidate_dates(r, today="2026-01-01")
    # 2 gidiş × 2 dönüş = 4 çift, hepsinde dönüş ≥ gidiş
    assert len(pairs) == 4
    assert all(ret >= dep for dep, ret in pairs)
    assert ("2026-07-10", "2026-07-20") in pairs and ("2026-07-11", "2026-07-21") in pairs


def test_candidate_dates_range_filters_return_before_departure():
    # gidiş aralığı dönüş aralığıyla çakışıyor → dönüş<gidiş çiftleri elenir
    r = Route(1, 1, "IST", "BEG", "2026-07-10", 5000,
              return_date="2026-07-10", date_end="2026-07-12", return_date_end="2026-07-12")
    pairs = candidate_dates(r, today="2026-01-01")
    assert all(ret >= dep for dep, ret in pairs)
    assert ("2026-07-12", "2026-07-10") not in pairs


def test_candidate_dates_range_capped():
    from models import MAX_DATE_PAIRS
    r = Route(1, 1, "IST", "BEG", "2026-07-01", 5000,
              return_date="2026-07-15", date_end="2026-07-31", return_date_end="2026-07-31")
    pairs = candidate_dates(r, today="2026-01-01")
    assert len(pairs) <= MAX_DATE_PAIRS


async def test_date_range_persisted(db):
    rid = await db.add_route(Route(None, 1, "IST", "BEG", "2026-07-10", 5000,
                                   return_date="2026-07-20", date_end="2026-07-12",
                                   return_date_end="2026-07-22"))
    r = await db.get_route(rid, 1)
    assert r.date_end == "2026-07-12" and r.return_date_end == "2026-07-22"
    assert r.has_date_range and "…" in r.label()


# ---------------------------------------------------- sabit dönüş / nights
def test_candidate_dates_fixed_return_with_departure_range():
    # gidiş aralık + sabit dönüş tarihi (tek gün)
    r = Route(1, 1, "IST", "BEG", "2026-07-10", 5000,
              return_date="2026-07-25", date_end="2026-07-12")
    pairs = candidate_dates(r, today="2026-01-01")
    assert [p for p in pairs] == [("2026-07-10", "2026-07-25"),
                                  ("2026-07-11", "2026-07-25"),
                                  ("2026-07-12", "2026-07-25")]


def test_candidate_dates_nights_fixed_duration():
    # gidiş aralık + sabit 7 gece → dönüş her zaman gidiş+7
    r = Route(1, 1, "IST", "BEG", "2026-07-01", 5000, date_end="2026-07-03", nights=7)
    pairs = candidate_dates(r, today="2026-01-01")
    assert pairs == [("2026-07-01", "2026-07-08"),
                     ("2026-07-02", "2026-07-09"),
                     ("2026-07-03", "2026-07-10")]


def test_route_nights_is_round_trip_and_label():
    r = Route(1, 1, "IST", "BEG", "2026-07-01", 5000, date_end="2026-07-31", nights=7)
    assert r.is_round_trip and "7 gece" in r.label() and "⇄" in r.label()


async def test_nights_persisted(db):
    rid = await db.add_route(Route(None, 1, "IST", "BEG", "2026-07-01", 5000,
                                   date_end="2026-07-31", nights=10))
    r = await db.get_route(rid, 1)
    assert r.nights == 10 and r.is_round_trip


# --------------------------------------------------- Balkan grubu + aktarma
def test_parse_airports_balkan_group():
    codes, err = _parse_airports("BALKAN")
    assert err is None
    assert set(codes) == {"BEG", "SJJ", "TGD", "TIV", "TIA", "SKP", "PRN"}


def test_guess_stops():
    assert FlightScraper._guess_stops("13:50 Wizz Air Aktarmasız ₺8.543") == "Aktarmasız"
    assert FlightScraper._guess_stops("THY 1 aktarma 6 sa ₺5.000") == "1 aktarma"
    assert FlightScraper._guess_stops("2 durak ₺4.000") == "2 aktarma"
    assert FlightScraper._guess_stops("sadece fiyat ₺3.000") is None


async def test_direct_only_persisted(db):
    rid = await db.add_route(Route(None, 1, "IST", "BEG", "2026-08-15", 3000, direct_only=True))
    r = await db.get_route(rid, 1)
    assert r.direct_only is True


def test_notifier_shows_stops():
    from notifier import Notifier
    from models import FlightResult
    r = Route(3, 1, "IST", "BEG", "2026-08-15", 3000)
    text = Notifier._format(r, FlightResult(price=2500, stops="1 aktarma", link="x"))
    assert "Aktarma: 1 aktarma" in text
