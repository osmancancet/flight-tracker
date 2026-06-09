"""Tracker döngüsü testleri: eşik bildirimi, dedupe, düşüş uyarısı, hata raporu.

Gerçek Playwright/Telegram olmadan; sahte scraper ve sahte bot ile."""
import asyncio
import os
import tempfile

import pytest

from database import Database
from models import FlightResult, Route
from notifier import Notifier
from tracker import Tracker, _FAIL_ALERT_THRESHOLD


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeScraper:
    """Sıralı fiyatlar döndürür; None = başarısızlık."""
    def __init__(self, prices):
        self.prices = list(prices)
        self.i = 0

    async def fetch_cheapest(self, o, d, date, return_date=None,
                             passengers=1, cabin="economy", nonstop_only=False):
        p = self.prices[self.i] if self.i < len(self.prices) else self.prices[-1]
        self.i += 1
        return None if p is None else FlightResult(price=p, airline="THY", link="x")


async def _fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db = Database(path)
    await db.init()
    return db


async def _make(prices, drop_pct=0.0):
    db = await _fresh_db()
    rid = await db.add_route(Route(None, 7, "IST", "LON", "2026-08-15", 3000, "TRY"))
    bot = FakeBot()
    notifier = Notifier(bot, min_interval=0)
    tracker = Tracker(db, FakeScraper(prices), notifier, 0, 0, {}, drop_alert_pct=drop_pct)
    return db, bot, notifier, tracker, rid


async def _run(tracker, notifier, n):
    for _ in range(n):
        await tracker.run_scan()
        await asyncio.sleep(0.02)
    await notifier.drain()


async def test_threshold_alert_and_dedupe():
    db, bot, notifier, tracker, _ = await _make([3500, 2800, 2900, 2500])
    await _run(tracker, notifier, 4)
    # 2800 ve 2500 bildirilir; 2900 (zaten <=2800 bildirildi) atlanır.
    assert len(bot.sent) == 2
    assert "2800" in bot.sent[0][1] and "2500" in bot.sent[1][1]


async def test_no_alert_above_threshold():
    db, bot, notifier, tracker, _ = await _make([3500, 3200, 3100])
    await _run(tracker, notifier, 3)
    assert bot.sent == []


async def test_drop_alert_above_threshold():
    # Hep eşik (3000) üstünde ama %20'lik ani düşüş var -> drop bildirimi
    db, bot, notifier, tracker, _ = await _make([5000, 5000, 3800], drop_pct=15)
    await _run(tracker, notifier, 3)
    assert len(bot.sent) == 1
    assert "düşüş" in bot.sent[0][1].lower()


async def test_drop_disabled_by_default():
    db, bot, notifier, tracker, _ = await _make([5000, 3800], drop_pct=0)
    await _run(tracker, notifier, 2)
    assert bot.sent == []  # eşik üstü + drop kapalı


async def test_failure_alert_after_threshold():
    db, bot, notifier, tracker, _ = await _make([None])  # hep başarısız
    await _run(tracker, notifier, _FAIL_ALERT_THRESHOLD)
    # Tam eşikte tek bir uyarı gönderilir.
    assert len(bot.sent) == 1 and "alınamadı" in bot.sent[0][1]
