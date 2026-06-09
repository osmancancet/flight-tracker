"""Veritabanı katmanı testleri: CRUD, migration, istatistik, dedupe."""
import os
import tempfile

import pytest

from database import Database
from models import FlightResult, Route


@pytest.fixture
async def db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    database = Database(path)
    await database.init()
    return database


def _route(chat=1, ret=None):
    return Route(None, chat, "IST", "LON", "2026-08-15", 3000, "TRY", return_date=ret)


async def test_add_list_delete(db):
    rid = await db.add_route(_route())
    assert rid == 1
    routes = await db.list_routes(1)
    assert len(routes) == 1 and routes[0].origin == "IST"
    # Yanlış chat silemez, doğru chat siler.
    assert await db.delete_route(rid, 999) is False
    assert await db.delete_route(rid, 1) is True
    assert await db.list_routes(1) == []


async def test_round_trip_persisted(db):
    rid = await db.add_route(_route(ret="2026-08-22"))
    r = await db.get_route(rid, 1)
    assert r.is_round_trip and r.return_date == "2026-08-22"
    assert "⇄" in r.label()


async def test_pause_resume_and_active_count(db):
    rid = await db.add_route(_route())
    assert await db.count_active() == 1
    assert await db.set_active(rid, 1, False) is True
    assert await db.count_active() == 0
    assert len(await db.get_active_routes()) == 0
    # list_routes hepsini gösterir (duraklatılmış dahil)
    assert len(await db.list_routes(1)) == 1
    await db.set_active(rid, 1, True)
    assert await db.count_active() == 1


async def test_update_threshold(db):
    rid = await db.add_route(_route())
    assert await db.update_threshold(rid, 1, 2500) is True
    assert (await db.get_route(rid, 1)).threshold == 2500
    assert await db.update_threshold(rid, 999, 100) is False


async def test_price_history_and_stats(db):
    rid = await db.add_route(_route())
    assert await db.last_price(rid) is None
    assert await db.price_stats(rid) is None
    for p in (3000, 2500, 2800):
        await db.record_price(rid, FlightResult(price=p, link="x"))
    assert await db.last_price(rid) == 2800
    st = await db.price_stats(rid)
    assert st.count == 3 and st.minimum == 2500 and st.maximum == 3000
    assert st.first == 3000 and st.latest == 2800
    assert await db.recent_prices(rid, 2) == [2500, 2800]


async def test_notification_dedupe_by_kind(db):
    rid = await db.add_route(_route())
    # threshold ve drop türleri bağımsız dedupe edilir
    assert await db.already_notified(rid, 2800, "threshold") is False
    await db.mark_notified(rid, 2800, "threshold")
    assert await db.already_notified(rid, 2900, "threshold") is True   # daha yüksek -> bildirildi say
    assert await db.already_notified(rid, 2500, "threshold") is False  # daha ucuz -> yeni
    assert await db.already_notified(rid, 2800, "drop") is False       # farklı tür
