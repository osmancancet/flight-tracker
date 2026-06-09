"""Saf fonksiyon testleri: fiyat/tarih parse, sparkline, URL kurma."""
import pytest

from scraper import _parse_price, FlightScraper
from bot import _parse_date, _parse_price as bot_price, _sparkline


@pytest.mark.parametrize("text,expected", [
    ("₺2.450", 2450), ("2.450 TL", 2450), ("TRY 2,450", 2450),
    ("$310", 310), ("1 250 TL", 1250), ("₺12.999", 12999),
    ("From ₺3.750 round trip", 3750), ("abc", None), ("", None),
])
def test_parse_price(text, expected):
    assert _parse_price(text) == (expected if expected is None else float(expected))


@pytest.mark.parametrize("raw,expected", [
    ("15-08-2026", "2026-08-15"),
    ("2026-08-15", "2026-08-15"),
    ("15.08.2026", "2026-08-15"),
    ("yarın", None),
    ("32-13-2026", None),
])
def test_parse_date(raw, expected):
    assert _parse_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("3000", 3000.0), ("2.500", 2500.0), ("2,500", 2500.0), ("2 500", 2500.0),
    ("0", None), ("-5", None), ("abc", None),
])
def test_bot_price(raw, expected):
    assert bot_price(raw) == expected


def test_sparkline():
    assert _sparkline([]) == ""
    assert _sparkline([5]) == ""
    assert _sparkline([1, 1, 1]) == "▁▁▁"           # düz
    line = _sparkline([1, 2, 3, 4])
    assert len(line) == 4 and line[0] == "▁" and line[-1] == "█"


def test_build_url_oneway_and_roundtrip():
    one = FlightScraper.build_url("IST", "LON", "2026-08-15")
    assert "IST" in one and "LON" in one and "2026-08-15" in one and "one+way" in one
    rt = FlightScraper.build_url("IST", "LON", "2026-08-15", "2026-08-22")
    assert "returning" in rt and "2026-08-22" in rt
