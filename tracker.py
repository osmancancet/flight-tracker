"""Tarama döngüsü: tüm aktif rotaları periyodik olarak tarar.

Akış (her tetiklemede):
  1. Aktif rotaları DB'den çek.
  2. Her rotayı SIRAYLA tara (DDoS değil) — aralara rastgele jitter koy.
  3. Önceki fiyatı oku → fiyatı kaydet (price_history).
  4. price < threshold ve bu fiyat için daha önce bildirim atılmadıysa → eşik bildirimi.
  5. DROP_ALERT_PCT > 0 ise ve fiyat önceki ölçüme göre yeterince düştüyse → düşüş bildirimi
     (eşik üstü olsa bile), ayrı dedupe ('drop' türü) ile.

Rota bazında try-except: bir rota patlasa da diğerleri etkilenmez (rules.md §5).
Üst üste başarısız olan rotalar için sahibine tek seferlik uyarı gönderilir.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from database import Database
from logger import get_logger
from models import MAX_DATE_PAIRS, MAX_SCAN_TASKS, Route
from notifier import Notifier
from predictor import signal_line
from scraper import FlightScraper

log = get_logger("tracker")

_FAIL_ALERT_THRESHOLD = 5  # üst üste bu kadar başarısızlıktan sonra sahibine bildir
_FLEX_CAP = 7              # esnek pencere üst sınırı (±gün) — istek sayısını sınırlar


def _shift(iso: str, days: int) -> str:
    return (datetime.strptime(iso, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def _date_list(start: str, end: str, today: str) -> List[str]:
    """start..end (dahil) ISO günleri; bugünden önceki günler elenir."""
    out: List[str] = []
    d = start
    guard = 0
    while d <= end and guard < 366:
        if d >= today:
            out.append(d)
        d = _shift(d, 1)
        guard += 1
    return out


def candidate_dates(route: Route, today: Optional[str] = None) -> List[Tuple[str, Optional[str]]]:
    """Taranacak (gidiş, dönüş) tarih çiftleri. Geçmiş gidişler elenir.

    İki mod:
      * ARALIK modu (date_end veya return_date_end dolu): gidiş aralığı × dönüş
        aralığının kartezyen çarpımı; gidiş-dönüşte yalnızca dönüş ≥ gidiş çiftleri.
        MAX_DATE_PAIRS ile sınırlanır (anti-ban).
      * ESNEK modu (yalnızca flex_days): tek tarihin ±flex penceresi; gidiş-dönüşte
        dönüş aynı kaydırmayla taşınır → yolculuk süresi sabit kalır (eski davranış).
    """
    today = today or datetime.now().strftime("%Y-%m-%d")

    # SABİT SÜRE modu: gidiş (tek gün ya da aralık) × dönüş = gidiş + nights gece.
    if route.nights > 0:
        deps = _date_list(route.date, route.date_end or route.date, today)
        pairs = [(d, _shift(d, route.nights)) for d in deps][:MAX_DATE_PAIRS]
        return pairs or [(route.date, _shift(route.date, route.nights))]

    if route.has_date_range:
        deps = _date_list(route.date, route.date_end or route.date, today)
        if not route.return_date:
            pairs = [(d, None) for d in deps]
        else:
            rets = _date_list(route.return_date, route.return_date_end or route.return_date, today)
            pairs = [(d, r) for d in deps for r in rets if r >= d]
        pairs = pairs[:MAX_DATE_PAIRS]
        return pairs or [(route.date, route.return_date)]

    flex = max(0, min(_FLEX_CAP, route.flex_days))
    pairs = []
    for off in range(-flex, flex + 1):
        dep = _shift(route.date, off)
        if dep < today:
            continue
        ret = _shift(route.return_date, off) if route.return_date else None
        pairs.append((dep, ret))
    return pairs or [(route.date, route.return_date)]


class Tracker:
    def __init__(self, db: Database, scraper: FlightScraper, notifier: Notifier,
                 jitter_min: float, jitter_max: float, app_bot_data: dict,
                 drop_alert_pct: float = 0.0):
        self.db = db
        self.scraper = scraper
        self.notifier = notifier
        self.jitter_min = jitter_min
        self.jitter_max = jitter_max
        self.drop_alert_pct = drop_alert_pct
        self._bot_data = app_bot_data
        self._running = False
        self._fail_counts: Dict[int, int] = {}

    async def run_scan(self) -> None:
        """Tek bir tam tarama turu. APScheduler tarafından periyodik çağrılır."""
        if self._running:
            log.info("Önceki tarama hâlâ sürüyor, bu tur atlanıyor.")
            return
        self._running = True
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            routes = await self.db.get_active_routes()
            if not routes:
                log.info("Aktif rota yok, tarama atlanıyor.")
                return
            log.info("Tarama başladı — %d rota", len(routes))

            for i, route in enumerate(routes):
                try:
                    await self._scan_route(route)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Rota #%s taranamadı: %s", route.id, exc)

                if i < len(routes) - 1:
                    delay = random.uniform(self.jitter_min, self.jitter_max)
                    log.info("Jitter: %.1fs bekleniyor", delay)
                    await asyncio.sleep(delay)

            log.info("Tarama tamamlandı.")
        finally:
            self._bot_data["last_scan_at"] = started
            self._running = False

    async def _scan_route(self, route: Route) -> None:
        result = await self._fetch_best(route)
        if result is None:
            self._register_failure(route)
            log.info("#%s %s: fiyat alınamadı.", route.id, route.label())
            return

        # Başarı: hata sayacını sıfırla.
        self._fail_counts.pop(route.id, None)

        previous = await self.db.last_price(route.id)  # kayıttan ÖNCE oku
        await self.db.record_price(route.id, result)
        log.info("#%s %s → %.0f %s (hedef %.0f)",
                 route.id, route.label(), result.price, result.currency, route.threshold)

        await self._maybe_threshold_alert(route, result)
        await self._maybe_drop_alert(route, result, previous)

    async def _fetch_best(self, route: Route):
        """Rota için en ucuz uçuşu getirir. Çoklu havalimanı (origin/dest virgüllü)
        ve esnek tarih (±gün) kombinasyonlarının HEPSİNİ sırayla tarar; en ucuzu seçer.
        Sonucun .origin/.dest/.date alanlarına kazanan kombinasyonu yazar.

        İstek sayısı = (kalkış × varış × tarih); aralara anti-ban jitter konur."""
        candidates = candidate_dates(route)
        combos = [(o, d) for o in route.origin_list() for d in route.dest_list()]

        # Anti-ban: tek taramada toplam istek MAX_SCAN_TASKS'i aşmasın. Çok kombo varsa
        # kombo başına tarih sayısını kısarak HER havalimanı/hedefin en az bir kez
        # taranmasını garanti et (örn. tüm Balkan hedefleri primer tarihte kontrol edilir).
        per_combo = len(candidates)
        if combos and len(combos) * per_combo > MAX_SCAN_TASKS:
            per_combo = max(1, MAX_SCAN_TASKS // len(combos))
            log.info("#%s: %d kombo × %d tarih çok; kombo başına %d tarihe kısıldı.",
                     route.id, len(combos), len(candidates), per_combo)
        tasks = [(o, d, dep, ret) for (o, d) in combos for (dep, ret) in candidates[:per_combo]]

        best = None
        for j, (o, d, dep, ret) in enumerate(tasks):
            r = await self.scraper.fetch_cheapest(
                o, d, dep, ret, route.passengers, route.cabin,
                nonstop_only=route.direct_only,
            )
            if r is not None and (best is None or r.price < best.price):
                r.date, r.return_date, r.origin, r.dest = dep, ret, o, d
                best = r
            if len(tasks) > 1 and j < len(tasks) - 1:
                await asyncio.sleep(random.uniform(self.jitter_min, self.jitter_max))
        return best

    async def _windows(self, route_id: int) -> Dict[int, Optional[float]]:
        """1/6/24 saatlik pencerelerde kaydedilen en ucuz fiyatlar.
        Güncel fiyat kayıt edildikten sonra çağrıldığından o da dahildir."""
        return {
            1: await self.db.cheapest_since(route_id, 1),
            6: await self.db.cheapest_since(route_id, 6),
            24: await self.db.cheapest_since(route_id, 24),
        }

    async def _best_pos(self, route: Route, home_price: float):
        """Rotada pos kodları varsa POS karşılaştırması yapar; ev fiyatından (TR)
        daha ucuz bir satış noktası bulursa (etiket, fiyat) döndürür, yoksa None.

        Yalnızca bildirim anında çalışır → istek sayısı dedupe ile sınırlı kalır."""
        codes = route.pos_codes()
        if not codes:
            return None
        try:
            results = await self.scraper.compare_pos(route, codes)
        except Exception as exc:  # noqa: BLE001
            log.warning("#%s POS karşılaştırma hatası: %s", route.id, exc)
            return None
        priced = [(c, p) for c, p in results if p is not None]
        if not priced:
            return None
        code, price = min(priced, key=lambda t: t[1])
        if price < home_price:
            from models import POS_CATALOG
            log.info("#%s en ucuz POS %s: %.0f (<%.0f)", route.id, code, price, home_price)
            return (POS_CATALOG.get(code, code), price)
        return None

    async def _maybe_threshold_alert(self, route: Route, result) -> None:
        if not route.has_target or result.price >= route.threshold:
            return
        if await self.db.already_notified(route.id, result.price, "threshold"):
            log.info("#%s eşik zaten bildirildi (≤ %.0f).", route.id, result.price)
            return
        windows = await self._windows(route.id)
        pos_best = await self._best_pos(route, result.price)
        prices = await self.db.recent_prices(route.id, 30)
        signal = signal_line(prices) if len(prices) >= 5 else None
        self.notifier.notify(route.chat_id, route, result, windows, pos_best, signal)  # fire-and-forget
        await self.db.mark_notified(route.id, result.price, "threshold")
        log.info("#%s EŞİK bildirimi: %.0f < %.0f", route.id, result.price, route.threshold)

    async def _maybe_drop_alert(self, route: Route, result, previous) -> None:
        # Rota kendi yüzdesini taşıyorsa onu, yoksa global ayarı kullan.
        effective_pct = route.drop_pct if route.drop_pct and route.drop_pct > 0 else self.drop_alert_pct
        if effective_pct <= 0 or not previous or previous <= 0:
            return
        pct = (previous - result.price) / previous * 100.0
        if pct < effective_pct:
            return
        if await self.db.already_notified(route.id, result.price, "drop"):
            log.info("#%s düşüş zaten bildirildi (≤ %.0f).", route.id, result.price)
            return
        windows = await self._windows(route.id)
        self.notifier.notify_drop(route.chat_id, route, result, previous, pct, windows)
        await self.db.mark_notified(route.id, result.price, "drop")
        log.info("#%s DÜŞÜŞ bildirimi: %%%.0f (%.0f→%.0f)",
                 route.id, pct, previous, result.price)

    def _register_failure(self, route: Route) -> None:
        n = self._fail_counts.get(route.id, 0) + 1
        self._fail_counts[route.id] = n
        if n == _FAIL_ALERT_THRESHOLD:
            self.notifier.notify_text(
                route.chat_id,
                f"⚠️ <b>{route.label()}</b> için son {n} taramada fiyat alınamadı. "
                f"Site yapısı değişmiş veya geçici bir sorun olabilir; denemeye devam ediyorum.",
            )
            log.warning("#%s üst üste %d başarısızlık — sahibine bildirildi.", route.id, n)
