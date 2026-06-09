"""Ortam değişkenlerini (.env) yükler ve tip güvenli bir Settings nesnesi sunar.

Tüm konfigürasyon tek noktadan okunur; rules.md gereği gizli bilgiler (token vb.)
koda gömülmez, yalnızca .env üzerinden gelir.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# .env dosyasını süreç başında bir kez yükle.
load_dotenv()


def _get_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on", "evet")


def _get_int(key: str, default: int) -> int:
    val = os.getenv(key)
    try:
        return int(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _get_float(key: str, default: float) -> float:
    val = os.getenv(key)
    try:
        return float(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _get_str(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val.strip() if val else default


@dataclass(frozen=True)
class Settings:
    bot_token: str
    default_chat_id: Optional[int]
    check_interval_min: int
    request_jitter_min: float
    request_jitter_max: float
    headless: bool
    proxy: Optional[str]
    db_path: str
    # Derinleştirme ayarları
    drop_alert_pct: float        # 0 = kapalı; >0 ise fiyat bu % düşünce eşik üstü olsa da uyar
    scraper_max_retries: int     # tek rota için tekrar deneme sayısı
    debug_dump: bool             # parse başarısızlığında ekran görüntüsü + HTML kaydet
    health_check_min: int        # tarayıcı sağlık kontrolü periyodu (dk)


class ConfigError(Exception):
    """Eksik/hatalı konfigürasyon durumunda fırlatılır."""


def load_settings() -> Settings:
    """Ortamdan Settings üretir. Zorunlu alan eksikse ConfigError fırlatır."""
    token = _get_str("TELEGRAM_BOT_TOKEN")
    if not token or token.startswith("123456:ABC"):
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN tanımlı değil. .env dosyasına BotFather'dan "
            "aldığın gerçek token'ı ekle (örnek için .env.example'a bak)."
        )

    chat_raw = _get_str("DEFAULT_CHAT_ID")
    default_chat_id: Optional[int] = None
    if chat_raw:
        try:
            default_chat_id = int(chat_raw)
        except ValueError:
            default_chat_id = None

    jitter_min = _get_float("REQUEST_JITTER_MIN", 4.0)
    jitter_max = _get_float("REQUEST_JITTER_MAX", 12.0)
    if jitter_max < jitter_min:
        jitter_min, jitter_max = jitter_max, jitter_min

    proxy = _get_str("PROXY") or None

    return Settings(
        bot_token=token,
        default_chat_id=default_chat_id,
        check_interval_min=max(1, _get_int("CHECK_INTERVAL_MIN", 10)),
        request_jitter_min=max(0.0, jitter_min),
        request_jitter_max=max(0.1, jitter_max),
        headless=_get_bool("HEADLESS", True),
        proxy=proxy,
        db_path=_get_str("DB_PATH", "tracker.db"),
        drop_alert_pct=max(0.0, _get_float("DROP_ALERT_PCT", 0.0)),
        scraper_max_retries=max(0, _get_int("SCRAPER_MAX_RETRIES", 2)),
        debug_dump=_get_bool("DEBUG_DUMP", False),
        health_check_min=max(1, _get_int("HEALTH_CHECK_MIN", 10)),
    )
