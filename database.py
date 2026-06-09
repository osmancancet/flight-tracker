"""Async SQLite katmanı (aiosqlite).

Üç tablo:
  - routes          : takip edilen aramalar (Telegram'dan dinamik CRUD)
  - price_history   : her tarama sonucu (analiz/geçmiş için)
  - notifications   : gönderilen bildirimler (dedupe). `kind` ayrımı ile eşik
                      bildirimi ('threshold') ve ani düşüş bildirimi ('drop')
                      birbirinden bağımsız dedupe edilir.

Şema hafif migration ile yönetilir: eksik kolonlar (return_date, kind) açılışta
otomatik eklenir; eski bir tracker.db sorunsuz yükseltilir.

Tüm fonksiyonlar asenkron ve try-except dışında bırakılmıştır; çağıran taraf
(tracker/bot) hatayı kendi bağlamında loglar. Bağlantı her çağrıda kısa ömürlü
açılıp kapanır — uzun süreli açık bağlantı kaynaklı kilit/sızıntı riski olmaz.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import aiosqlite

from logger import get_logger
from models import FlightResult, Route

log = get_logger("database")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    origin      TEXT    NOT NULL,
    dest        TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    threshold   REAL    NOT NULL,
    currency    TEXT    NOT NULL DEFAULT 'TRY',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id    INTEGER NOT NULL,
    price       REAL    NOT NULL,
    currency    TEXT,
    airline     TEXT,
    dep_time    TEXT,
    arr_time    TEXT,
    link        TEXT,
    fetched_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (route_id) REFERENCES routes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id    INTEGER NOT NULL,
    price       REAL    NOT NULL,
    sent_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (route_id) REFERENCES routes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_routes_active ON routes(active);
CREATE INDEX IF NOT EXISTS idx_hist_route   ON price_history(route_id);
CREATE INDEX IF NOT EXISTS idx_notif_route  ON notifications(route_id);
"""


_ROUTE_COLS = ("id, chat_id, origin, dest, date, threshold, currency, active, "
               "return_date, passengers, cabin, flex_days, pos, date_end, return_date_end, "
               "nights, direct_only, drop_pct")


@dataclass
class PriceStats:
    count: int
    minimum: float
    maximum: float
    average: float
    latest: float
    first: float


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            await db.executescript(_SCHEMA)
            await self._migrate(db)
            await db.commit()
        log.info("Veritabanı hazır: %s", self.path)

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        """Eski şemaları sessizce yükselt (eksik kolonları ekle)."""
        await self._add_column_if_missing(db, "routes", "return_date", "TEXT")
        await self._add_column_if_missing(db, "routes", "passengers", "INTEGER NOT NULL DEFAULT 1")
        await self._add_column_if_missing(db, "routes", "cabin", "TEXT NOT NULL DEFAULT 'economy'")
        await self._add_column_if_missing(db, "routes", "flex_days", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column_if_missing(db, "routes", "pos", "TEXT")
        await self._add_column_if_missing(db, "routes", "date_end", "TEXT")
        await self._add_column_if_missing(db, "routes", "return_date_end", "TEXT")
        await self._add_column_if_missing(db, "routes", "nights", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column_if_missing(db, "routes", "direct_only", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column_if_missing(db, "routes", "drop_pct", "REAL NOT NULL DEFAULT 0")
        await self._add_column_if_missing(
            db, "notifications", "kind", "TEXT NOT NULL DEFAULT 'threshold'"
        )

    @staticmethod
    async def _add_column_if_missing(db, table: str, column: str, decl: str) -> None:
        cur = await db.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in await cur.fetchall()}
        if column not in cols:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            log.info("Migration: %s.%s eklendi", table, column)

    # ---------------------------------------------------------------- routes
    async def add_route(self, route: Route) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO routes "
                "(chat_id, origin, dest, date, threshold, currency, active, return_date, "
                "passengers, cabin, flex_days, pos, date_end, return_date_end, nights, direct_only, drop_pct) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (route.chat_id, route.origin, route.dest, route.date,
                 route.threshold, route.currency, route.return_date,
                 route.passengers, route.cabin, route.flex_days, route.pos,
                 route.date_end, route.return_date_end, route.nights,
                 1 if route.direct_only else 0, route.drop_pct),
            )
            await db.commit()
            return cur.lastrowid

    async def list_routes(self, chat_id: int, only_active: bool = False) -> List[Route]:
        query = f"SELECT {_ROUTE_COLS} FROM routes WHERE chat_id = ?"
        params: list = [chat_id]
        if only_active:
            query += " AND active = 1"
        query += " ORDER BY id"
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(query, params)).fetchall()
        return [self._row_to_route(r) for r in rows]

    async def get_active_routes(self) -> List[Route]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                f"SELECT {_ROUTE_COLS} FROM routes WHERE active = 1 ORDER BY id"
            )).fetchall()
        return [self._row_to_route(r) for r in rows]

    async def get_route(self, route_id: int, chat_id: int) -> Optional[Route]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                f"SELECT {_ROUTE_COLS} FROM routes WHERE id = ? AND chat_id = ?",
                (route_id, chat_id),
            )).fetchone()
        return self._row_to_route(row) if row else None

    async def delete_route(self, route_id: int, chat_id: int) -> bool:
        """Yalnızca rotayı ekleyen chat silebilir. Silindiyse True döner."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            cur = await db.execute(
                "DELETE FROM routes WHERE id = ? AND chat_id = ?", (route_id, chat_id)
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_active(self, route_id: int, chat_id: int, active: bool) -> bool:
        """Rotayı duraklat/devam ettir. Değiştiyse True."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE routes SET active = ? WHERE id = ? AND chat_id = ?",
                (1 if active else 0, route_id, chat_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def update_threshold(self, route_id: int, chat_id: int, threshold: float) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE routes SET threshold = ? WHERE id = ? AND chat_id = ?",
                (threshold, route_id, chat_id),
            )
            await db.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------- history
    async def record_price(self, route_id: int, result: FlightResult) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO price_history (route_id, price, currency, airline, dep_time, arr_time, link) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (route_id, result.price, result.currency, result.airline,
                 result.departure_time, result.arrival_time, result.link),
            )
            await db.commit()

    async def last_price(self, route_id: int) -> Optional[float]:
        """Bu rotanın en son kaydedilen fiyatı (yoksa None)."""
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT price FROM price_history WHERE route_id = ? ORDER BY id DESC LIMIT 1",
                (route_id,),
            )).fetchone()
            return float(row[0]) if row else None

    async def price_stats(self, route_id: int) -> Optional[PriceStats]:
        """Rota için özet istatistik (kayıt yoksa None)."""
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT COUNT(*), MIN(price), MAX(price), AVG(price) FROM price_history WHERE route_id = ?",
                (route_id,),
            )).fetchone()
            if not row or not row[0]:
                return None
            count, mn, mx, avg = row
            latest = await (await db.execute(
                "SELECT price FROM price_history WHERE route_id = ? ORDER BY id DESC LIMIT 1",
                (route_id,),
            )).fetchone()
            first = await (await db.execute(
                "SELECT price FROM price_history WHERE route_id = ? ORDER BY id ASC LIMIT 1",
                (route_id,),
            )).fetchone()
        return PriceStats(
            count=int(count), minimum=float(mn), maximum=float(mx),
            average=float(avg), latest=float(latest[0]), first=float(first[0]),
        )

    async def recent_prices(self, route_id: int, limit: int = 12) -> List[float]:
        """En son N fiyat, kronolojik (eskiden yeniye) sırada — sparkline için."""
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT price FROM price_history WHERE route_id = ? ORDER BY id DESC LIMIT ?",
                (route_id, limit),
            )).fetchall()
        return [float(r[0]) for r in reversed(rows)]

    async def cheapest_since(self, route_id: int, hours: int) -> Optional[float]:
        """Son `hours` saat içinde kaydedilen en düşük fiyat (yoksa None).

        SQLite tarihleri UTC 'now' ile karşılaştırılır; fetched_at da datetime('now')
        (UTC) ile yazıldığından tutarlıdır."""
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT MIN(price) FROM price_history "
                "WHERE route_id = ? AND fetched_at >= datetime('now', ?)",
                (route_id, f"-{int(hours)} hours"),
            )).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def price_series(self, route_id: int, limit: int = 200) -> List[Tuple[str, float]]:
        """(fetched_at, price) çiftleri, kronolojik sırada — PNG grafik için."""
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT fetched_at, price FROM price_history WHERE route_id = ? ORDER BY id DESC LIMIT ?",
                (route_id, limit),
            )).fetchall()
        return [(str(r[0]), float(r[1])) for r in reversed(rows)]

    # --------------------------------------------------------- notifications
    async def already_notified(self, route_id: int, price: float, kind: str = "threshold") -> bool:
        """Bu rota+tür için, mevcut fiyata eşit veya daha düşük bir bildirim daha
        önce gönderildi mi? Gönderildiyse tekrar spam yapmayız."""
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT 1 FROM notifications WHERE route_id = ? AND kind = ? AND price <= ? LIMIT 1",
                (route_id, kind, price),
            )).fetchone()
            return row is not None

    async def mark_notified(self, route_id: int, price: float, kind: str = "threshold") -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO notifications (route_id, price, kind) VALUES (?, ?, ?)",
                (route_id, price, kind),
            )
            await db.commit()

    async def count_active(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT COUNT(*) FROM routes WHERE active = 1"
            )).fetchone()
            return int(row[0]) if row else 0

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _row_to_route(r: aiosqlite.Row) -> Route:
        return Route(
            id=r["id"],
            chat_id=r["chat_id"],
            origin=r["origin"],
            dest=r["dest"],
            date=r["date"],
            threshold=r["threshold"],
            currency=r["currency"],
            active=bool(r["active"]),
            return_date=r["return_date"],
            passengers=r["passengers"],
            cabin=r["cabin"],
            flex_days=r["flex_days"],
            pos=r["pos"],
            date_end=r["date_end"],
            return_date_end=r["return_date_end"],
            nights=r["nights"],
            direct_only=bool(r["direct_only"]),
            drop_pct=r["drop_pct"],
        )
