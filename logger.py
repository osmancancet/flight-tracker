"""Merkezi logging kurulumu: konsol + döner dosya.

Tüm modüller `from logger import get_logger` ile aynı yapılandırmayı paylaşır.
Döner dosya, bot günlerce açık kalsa bile log dosyasının şişmesini önler.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_CONFIGURED = False
_LOG_DIR = "logs"
_LOG_FILE = os.path.join(_LOG_DIR, "tracker.log")

_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    os.makedirs(_LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(_FMT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Gürültülü 3. parti logger'ları kıs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    return logging.getLogger(name)
