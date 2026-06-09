"""Telegram bildirim gönderimi (fire-and-forget + rate-limit saygısı).

Tarama döngüsü `notify(...)` çağrısında BEKLEMEZ: mesaj bir asyncio task'ına
atılır. Böylece bildirim gecikmesi/yavaşlığı scraping'i bloklamaz (rules.md §4).

Telegram'ın saniyelik mesaj sınırına saygı için gönderimler arası küçük bir
gecikme uygulanır; gönderim hatası yutulur ve loglanır (program çökmez).
"""
from __future__ import annotations

import asyncio
from typing import Dict, Optional, Set

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from logger import get_logger
from models import FlightResult, Route, _human

log = get_logger("notifier")


class Notifier:
    def __init__(self, bot: Bot, min_interval: float = 1.2):
        self._bot = bot
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._tasks: Set[asyncio.Task] = set()

    def notify(self, chat_id: int, route: Route, result: FlightResult,
               windows: Optional[Dict[int, Optional[float]]] = None,
               pos_best=None, signal: Optional[str] = None) -> None:
        """Eşik altı bildirimi. Fire-and-forget: çağıran beklemez."""
        text = self._format(route, result, windows, pos_best, signal)
        task = asyncio.create_task(self._send(chat_id, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def notify_drop(self, chat_id: int, route: Route, result: FlightResult,
                    previous: float, pct: float,
                    windows: Optional[Dict[int, Optional[float]]] = None) -> None:
        """Ani fiyat düşüşü bildirimi (eşik üstü olsa da). Fire-and-forget."""
        text = self._format_drop(route, result, previous, pct, windows)
        task = asyncio.create_task(self._send(chat_id, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def notify_text(self, chat_id: int, text: str) -> None:
        task = asyncio.create_task(self._send(chat_id, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, chat_id: int, text: str) -> None:
        # Rate-limit: gönderimleri serileştir ve aralarına minimum gecikme koy.
        async with self._lock:
            try:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
            except RetryAfter as exc:
                log.warning("Telegram rate-limit, %.1fs bekleniyor", exc.retry_after)
                await asyncio.sleep(float(exc.retry_after) + 0.5)
                try:
                    await self._bot.send_message(
                        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
                    )
                except TelegramError as exc2:
                    log.warning("Bildirim tekrar denemesi başarısız: %s", exc2)
            except TelegramError as exc:
                log.warning("Bildirim gönderilemedi (chat=%s): %s", chat_id, exc)
            finally:
                await asyncio.sleep(self._min_interval)

    @staticmethod
    def _windows_line(route: Route, windows: Optional[Dict[int, Optional[float]]]) -> Optional[str]:
        """1/6/24 saatlik en ucuz fiyat özeti satırı (veri yoksa None)."""
        if not windows:
            return None
        labels = {1: "1s", 6: "6s", 24: "24s"}
        parts = []
        for h in (1, 6, 24):
            val = windows.get(h)
            if val is not None:
                parts.append(f"{labels[h]}: {val:.0f}")
        if not parts:
            return None
        return "📊 En ucuz — " + " · ".join(parts) + f" {route.currency}"

    @staticmethod
    def _chosen_date_line(route: Route, r: FlightResult) -> Optional[str]:
        """Esnek/aralık taramada seçilen en uygun gidiş (ve dönüş) tarihi."""
        if not r.date:
            return None
        multi = route.has_date_range or route.flex_days or r.date != route.date
        if not multi:
            return None
        if route.is_round_trip and r.return_date:
            return f"📅 En uygun tarih: <b>{_human(r.date)} → {_human(r.return_date)}</b>"
        return f"📅 En uygun gidiş: <b>{_human(r.date)}</b>"

    @staticmethod
    def _best_route_line(route: Route, r: FlightResult) -> Optional[str]:
        """Çoklu havalimanında kazanan kalkış→varış (tek seçenek varsa gizli)."""
        if not (r.origin and r.dest):
            return None
        if len(route.origin_list()) <= 1 and len(route.dest_list()) <= 1:
            return None
        return f"🛫 En uygun rota: <b>{r.origin}→{r.dest}</b>"

    @classmethod
    def _format(cls, route: Route, r: FlightResult,
                windows: Optional[Dict[int, Optional[float]]] = None,
                pos_best=None, signal: Optional[str] = None) -> str:
        lines = [
            "🎯 <b>Ucuz bilet bulundu!</b>",
            f"✈️ <b>{route.label()}</b>",
            f"💰 <b>{r.price:.0f} {r.currency}</b>  (hedef: {route.threshold:.0f} {route.currency})",
        ]
        brl = cls._best_route_line(route, r)
        if brl:
            lines.append(brl)
        dl = cls._chosen_date_line(route, r)
        if dl:
            lines.append(dl)
        if r.airline:
            lines.append(f"🏷️ Havayolu: {r.airline}")
        if r.departure_time or r.arrival_time:
            seg = " - ".join(x for x in (r.departure_time, r.arrival_time) if x)
            lines.append(f"🕐 Saat: {seg}")
        if r.stops:
            lines.append(f"🔁 Aktarma: {r.stops}")
        if r.duration:
            lines.append(f"⏱️ Süre: {r.duration}")
        wl = cls._windows_line(route, windows)
        if wl:
            lines.append(wl)
        if pos_best:
            name, price = pos_best
            lines.append(f"💱 Daha ucuz satış noktası — <b>{name}</b>: {price:.0f} {r.currency} "
                         f"(kart döviz komisyonu olabilir)")
        if signal:
            lines.append(signal)
        if r.link:
            lines.append(f'\n🔗 <a href="{r.link}">Bileti görüntüle</a>')
        return "\n".join(lines)

    @classmethod
    def _format_drop(cls, route: Route, r: FlightResult, previous: float, pct: float,
                     windows: Optional[Dict[int, Optional[float]]] = None) -> str:
        lines = [
            "📉 <b>Ani fiyat düşüşü!</b>",
            f"✈️ <b>{route.label()}</b>",
            f"💰 <b>{r.price:.0f} {r.currency}</b>  "
            f"(önceki {previous:.0f} → <b>%{pct:.0f}</b> düşüş)",
        ]
        dl = cls._chosen_date_line(route, r)
        if dl:
            lines.append(dl)
        if r.price >= route.threshold:
            lines.append(f"ℹ️ Hedefin ({route.threshold:.0f}) hâlâ üstünde ama hızlı düşüyor.")
        if r.airline:
            lines.append(f"🏷️ Havayolu: {r.airline}")
        wl = cls._windows_line(route, windows)
        if wl:
            lines.append(wl)
        if r.link:
            lines.append(f'\n🔗 <a href="{r.link}">Bileti görüntüle</a>')
        return "\n".join(lines)

    async def drain(self) -> None:
        """Kapanışta bekleyen bildirim task'larının bitmesini bekle."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
