"""Giriş noktası: tüm bileşenleri tek asyncio event loop'unda birlikte çalıştırır.

  * Telegram bot (polling) ile komutları dinler.
  * APScheduler (AsyncIOScheduler) ile her CHECK_INTERVAL_MIN dakikada bir tarama yapar.
  * Playwright tarayıcı örneği tüm yaşam döngüsü boyunca paylaşılır.

Graceful shutdown: SIGINT/SIGTERM'de scheduler durur, bekleyen bildirimler
boşaltılır ve tarayıcı temiz kapanır.

Çalıştırma:  python main.py
"""
from __future__ import annotations

import asyncio
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

from bot import BotHandlers
from config import ConfigError, load_settings
from database import Database
from logger import get_logger
from notifier import Notifier
from scraper import FlightScraper
from tracker import Tracker

log = get_logger("main")


async def run() -> None:
    settings = load_settings()

    # --- Bileşenleri kur ---
    db = Database(settings.db_path)
    await db.init()

    scraper = FlightScraper(
        headless=settings.headless,
        proxy=settings.proxy,
        max_retries=settings.scraper_max_retries,
        debug_dump=settings.debug_dump,
    )
    await scraper.start()

    application: Application = (
        Application.builder().token(settings.bot_token).build()
    )

    notifier = Notifier(application.bot)
    handlers = BotHandlers(db, scraper)
    handlers.register(application)

    tracker = Tracker(
        db=db,
        scraper=scraper,
        notifier=notifier,
        jitter_min=settings.request_jitter_min,
        jitter_max=settings.request_jitter_max,
        app_bot_data=application.bot_data,
        drop_alert_pct=settings.drop_alert_pct,
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        tracker.run_scan,
        trigger="interval",
        minutes=settings.check_interval_min,
        id="scan",
        max_instances=1,
        coalesce=True,
    )
    # Tarayıcı sağlık kontrolü: çökerse otomatik yeniden başlatır.
    scheduler.add_job(
        scraper.health_check,
        trigger="interval",
        minutes=settings.health_check_min,
        id="health",
        max_instances=1,
        coalesce=True,
    )

    # --- Başlat ---
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    scheduler.start()
    log.info("Sistem çalışıyor. Tarama her %d dk. Ctrl+C ile durdur.",
             settings.check_interval_min)

    # İlk taramayı başlangıçta bir kez hemen yap.
    asyncio.create_task(tracker.run_scan())

    # --- Kapanışa kadar bekle ---
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # bazı platformlarda desteklenmez
    await stop_event.wait()

    # --- Graceful shutdown ---
    log.info("Kapatılıyor...")
    scheduler.shutdown(wait=False)
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    await notifier.drain()
    await scraper.close()
    log.info("Temiz çıkış tamamlandı.")


def main() -> None:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        log.error("Konfigürasyon hatası: %s", exc)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
