"""Telegram komut handler'ları — çift yönlü kontrol (rules.md §2).

Komutlar:
  /start, /yardim                                   → tanıtım + komut listesi
  /rota_ekle IST LON 15-08-2026 3000                → tek yön rota
  /rota_ekle IST LON 15-08-2026 22-08-2026 5000     → gidiş-dönüş rota
  /rotalar                                          → rotaları listele (+ inline butonlar)
  /sil <id>                                         → rota sil
  /duraklat <id> | /devam <id>                      → rotayı duraklat / devam ettir
  /esik <id> <fiyat>                                → hedef fiyatı güncelle
  /gecmis <id>                                      → fiyat geçmişi & istatistik
  /durum                                            → sistem durumu

Inline butonlar: /rotalar her rota için Sil / Duraklat-Devam butonları üretir;
CallbackQueryHandler bunları işler. Tüm handler'lar geniş try-except ile sarılıdır
(rules.md §5); geçersiz girdi kullanıcıya net hata olarak döner, bot çökmez.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import asyncio

from charts import render_price_chart
from database import Database
from logger import get_logger
from predictor import signal_line
from models import (
    CABINS,
    DEFAULT_POS,
    DEST_GROUPS,
    MAX_AIRPORT_COMBOS,
    POS_CATALOG,
    Route,
    expand_airport,
)

log = get_logger("bot")

_IATA_LEN = 3
_SPARK = "▁▂▃▄▅▆▇█"

HELP_TEXT = (
    "🛫 <b>Ucuz Uçak Bileti Avcısı</b>\n\n"
    "Hedef fiyatın altına düşen biletleri 7/24 takip eder.\n\n"
    "<b>Rota ekleme</b>\n"
    "• <code>/rota_ekle IST BEG 15-08-2026 3000</code>  (tek yön)\n"
    "• <code>/rota_ekle IST,SAW,ADB,ESB BEG 15-08-2026 3000</code>  (çoklu kalkış)\n"
    "• <code>/rota_ekle IST BALKAN 01-07-2026..28-07-2026 4000 nights=7</code>\n"
    "   (BALKAN = vizesiz Balkan ülkeleri: BEG,SJJ,TGD,TIV,TIA,SKP,PRN — hepsi taranır)\n"
    "• <code>/rota_ekle IST BEG 15-08-2026 22-08-2026 5000</code>  (gidiş-dönüş)\n"
    "• <code>/rota_ekle IST BEG 10-07-2026..15-07-2026 20-07-2026..25-07-2026 5000</code>\n"
    "   (gidiş-dönüş + tarih ARALIĞI: aralıktaki tüm günleri tarar, en ucuzu bulur)\n"
    "• <code>/rota_ekle IST BEG 10-07-2026..15-07-2026 25-07-2026 5000</code>\n"
    "   (gidiş aralık + <b>sabit dönüş tarihi</b>)\n"
    "• <code>/rota_ekle IST BEG 01-07-2026..31-07-2026 5000 nights=7</code>\n"
    "   (gidiş aralık + <b>sabit süre</b>: dönüş = gidiş + 7 gece, en ucuz haftayı bulur)\n"
    "   kalkış varış gidiş [dönüş] hedef_fiyat — kalkış/varış virgülle birden çok olabilir\n"
    "   <i>İsteğe bağlı:</i> <code>pax=2 cabin=business flex=3 nights=7 pos=GB,DE near=1 direct=1</code>\n"
    "   (direct=1: yalnızca aktarmasız; varsayılan aktarmalı uçuşlar da dahil)\n"
    "   (yolcu 1-9 · kabin · esnek ±0-7 gün · pos: ülke satış noktası ·\n"
    "    near=1: havalimanını metro grubuna genişlet, örn. IST→IST,SAW)\n\n"
    "<b>Yönetim</b>\n"
    "• <code>/rotalar</code> — rotaların (butonlarla yönet)\n"
    "• <code>/sil &lt;id&gt;</code> — sil\n"
    "• <code>/duraklat &lt;id&gt;</code> / <code>/devam &lt;id&gt;</code>\n"
    "• <code>/esik &lt;id&gt; &lt;fiyat&gt;</code> — hedef fiyatı güncelle\n"
    "• <code>/gecmis &lt;id&gt;</code> — fiyat geçmişi & istatistik\n"
    "• <code>/grafik &lt;id&gt;</code> — fiyat geçmişi grafiği (PNG)\n"
    "• <code>/karsilastir &lt;id&gt; [TR,GB,DE]</code> — ülke satış noktası fiyatları\n"
    "• <code>/durum</code> — sistem durumu\n"
    "• <code>/yardim</code> — bu mesaj"
)


def _parse_date(raw: str) -> Optional[str]:
    """'15-08-2026' (GG-AA-YYYY) → '2026-08-15' (ISO). Geçersiz → None."""
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_date_range(raw: str):
    """'15-08-2026' → (iso, None) | '10-07-2026..15-07-2026' → (iso_start, iso_end).
    Hatalı veya başlangıç>bitiş ise (None, None, hata_mesajı)."""
    parts = raw.split("..")
    if len(parts) == 1:
        d = _parse_date(parts[0])
        return (d, None, None) if d else (None, None, f"Geçersiz tarih: {parts[0]}")
    if len(parts) == 2:
        start, end = _parse_date(parts[0]), _parse_date(parts[1])
        if not start or not end:
            return (None, None, f"Geçersiz tarih aralığı: {raw}")
        if end < start:
            return (None, None, "Aralıkta bitiş, başlangıçtan önce olamaz.")
        return (start, end, None)
    return (None, None, f"Geçersiz tarih biçimi: {raw}")


def _parse_price(raw: str) -> Optional[float]:
    """Hedef fiyatı (tam sayı TRY) ayrıştırır. Binlik ayraçlarını (./,/boşluk)
    temizler: '3000', '2.500', '2,500', '2 500' → 3000/2500/2500/2500."""
    cleaned = raw.replace(".", "").replace(",", "").replace(" ", "").strip()
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


_CABIN_ALIASES = {
    "economy": "economy", "ekonomi": "economy", "eco": "economy",
    "premium": "premium", "premiumeconomy": "premium",
    "business": "business", "is": "business", "iş": "business",
    "first": "first", "birinci": "first",
}


def parse_flags(args: List[str]):
    """args'ı pozisyonel ve key=value bayraklarına ayırır.

    Döner: (positional_list, {pax, cabin, flex}) — geçersiz bayrak değeri None'a
    çevrilir ve çağıran tarafça hata olarak ele alınır."""
    positional = [a for a in args if "=" not in a]
    raw = {}
    for a in args:
        if "=" in a:
            k, _, v = a.partition("=")
            raw[k.strip().lower()] = v.strip()

    flags = {"pax": 1, "cabin": "economy", "flex": 0, "nights": 0, "pos": None,
             "near": False, "direct": False, "error": None}

    if "nights" in raw or "gece" in raw:
        val = raw.get("nights", raw.get("gece"))
        if val.isdigit() and 1 <= int(val) <= 30:
            flags["nights"] = int(val)
        else:
            flags["error"] = "Gece sayısı 1-30 olmalı (nights=7)."

    if "direct" in raw or "aktarmasiz" in raw or "aktarmasız" in raw:
        val = (raw.get("direct") or raw.get("aktarmasiz") or raw.get("aktarmasız") or "1").lower()
        flags["direct"] = val in ("1", "true", "evet", "yes", "on")

    if "near" in raw or "yakin" in raw or "yakın" in raw:
        val = (raw.get("near") or raw.get("yakin") or raw.get("yakın") or "1").lower()
        flags["near"] = val in ("1", "true", "evet", "yes", "on")

    if "pax" in raw or "yolcu" in raw:
        val = raw.get("pax", raw.get("yolcu"))
        if val.isdigit() and 1 <= int(val) <= 9:
            flags["pax"] = int(val)
        else:
            flags["error"] = "Yolcu sayısı 1-9 arası olmalı (pax=2)."

    if "cabin" in raw or "kabin" in raw:
        val = (raw.get("cabin", raw.get("kabin")) or "").lower()
        mapped = _CABIN_ALIASES.get(val)
        if mapped:
            flags["cabin"] = mapped
        else:
            flags["error"] = f"Kabin: {', '.join(sorted(set(CABINS)))} (cabin=business)."

    if "flex" in raw or "esnek" in raw:
        val = raw.get("flex", raw.get("esnek"))
        if val.isdigit() and 0 <= int(val) <= 7:
            flags["flex"] = int(val)
        else:
            flags["error"] = "Esnek pencere 0-7 gün olmalı (flex=3)."

    if "pos" in raw:
        codes, bad = _normalize_pos(raw["pos"])
        if bad:
            flags["error"] = f"Geçersiz POS kodu: {bad}. Geçerli: {', '.join(POS_CATALOG)}."
        else:
            flags["pos"] = ",".join(codes) if codes else None

    return positional, flags


def _parse_airports(raw: str, near: bool = False):
    """'IST,SAW' / 'IST' / 'BALKAN' → (kod_listesi, None) | hata: ([], mesaj).
    - Grup anahtarı (örn. BALKAN) tüm grup havalimanlarına genişler.
    - near=True ise her havalimanı metro grubuna genişler (IST → IST,SAW)."""
    codes = []
    for part in raw.split(","):
        c = part.strip().upper()
        if not c:
            continue
        if c in DEST_GROUPS:               # grup anahtarı (BALKAN gibi)
            expanded = DEST_GROUPS[c]
        elif len(c) == 3 and c.isalpha():  # tek IATA
            expanded = expand_airport(c) if near else [c]
        else:
            return [], f"'{c}' geçerli 3 harfli IATA kodu ya da grup adı değil."
        for x in expanded:
            if x not in codes:
                codes.append(x)
    if not codes:
        return [], "En az bir havalimanı kodu gerekli."
    return codes, None


def _normalize_pos(raw: str):
    """'gb,de' -> (['GB','DE'], None) | hatalıysa (geçerli olanlar, ilk_hatalı)."""
    codes, bad = [], None
    for part in raw.split(","):
        c = part.strip().upper()
        if not c:
            continue
        if c not in POS_CATALOG:
            bad = c
            break
        if c not in codes:
            codes.append(c)
    return codes, bad


def _sparkline(prices: List[float]) -> str:
    """Fiyat listesini blok karakterli mini grafiğe çevirir."""
    if len(prices) < 2:
        return ""
    lo, hi = min(prices), max(prices)
    if hi == lo:
        return _SPARK[0] * len(prices)
    span = hi - lo
    return "".join(_SPARK[min(len(_SPARK) - 1, int((p - lo) / span * (len(_SPARK) - 1)))]
                    for p in prices)


class BotHandlers:
    def __init__(self, db: Database, scraper=None):
        self.db = db
        self.scraper = scraper  # /karsilastir için (opsiyonel; yoksa komut bilgi verir)

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler(["start", "yardim", "help"], self.cmd_help))
        app.add_handler(CommandHandler("rota_ekle", self.cmd_add))
        app.add_handler(CommandHandler("rotalar", self.cmd_list))
        app.add_handler(CommandHandler("sil", self.cmd_delete))
        app.add_handler(CommandHandler("duraklat", self.cmd_pause))
        app.add_handler(CommandHandler("devam", self.cmd_resume))
        app.add_handler(CommandHandler("esik", self.cmd_threshold))
        app.add_handler(CommandHandler("gecmis", self.cmd_history))
        app.add_handler(CommandHandler("grafik", self.cmd_chart))
        app.add_handler(CommandHandler(["karsilastir", "pos"], self.cmd_compare))
        app.add_handler(CommandHandler("durum", self.cmd_status))
        app.add_handler(CallbackQueryHandler(self.on_callback))

    # ------------------------------------------------------------- helpers
    async def _reply(self, update: Update, text: str,
                     markup: Optional[InlineKeyboardMarkup] = None) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                reply_markup=markup,
            )

    async def _render_routes(self, chat_id: int) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
        """/rotalar ve callback sonrası tazeleme için ortak gösterim üretir."""
        routes = await self.db.list_routes(chat_id)  # aktif + duraklatılmış hepsi
        if not routes:
            return ("📭 Henüz rotan yok. <code>/rota_ekle</code> ile ekle.", None)
        lines = ["📋 <b>Rotaların:</b>"]
        rows: List[List[InlineKeyboardButton]] = []
        for r in routes:
            badge = "🟢" if r.active else "⏸️"
            lines.append(f"{badge} #{r.id} — <b>{r.label()}</b>  ≤ {r.threshold:.0f} {r.currency}")
            toggle = (InlineKeyboardButton("⏸️ Duraklat", callback_data=f"pause:{r.id}")
                      if r.active else
                      InlineKeyboardButton("▶️ Devam", callback_data=f"resume:{r.id}"))
            rows.append([
                toggle,
                InlineKeyboardButton("🗑️ Sil", callback_data=f"del:{r.id}"),
            ])
        return ("\n".join(lines), InlineKeyboardMarkup(rows))

    # -------------------------------------------------------------- commands
    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, HELP_TEXT)

    async def cmd_add(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            positional, flags = parse_flags(ctx.args or [])
            if flags["error"]:
                await self._reply(update, f"❌ {flags['error']}")
                return
            if len(positional) not in (4, 5):
                await self._reply(
                    update,
                    "❌ Kullanım:\n"
                    "<code>/rota_ekle IST LON 15-08-2026 3000</code>  (tek yön)\n"
                    "<code>/rota_ekle IST LON 15-08-2026 22-08-2026 5000</code>  (gidiş-dönüş)\n"
                    "İsteğe bağlı bayraklar: <code>pax=2 cabin=business flex=3</code>",
                )
                return

            args = positional
            origins, err1 = _parse_airports(args[0], flags["near"])
            dests, err2 = _parse_airports(args[1], flags["near"])
            if err1 or err2:
                await self._reply(update, f"❌ {err1 or err2}")
                return
            if len(origins) * len(dests) > MAX_AIRPORT_COMBOS:
                await self._reply(
                    update,
                    f"❌ Çok fazla havalimanı kombinasyonu ({len(origins)}×{len(dests)}). "
                    f"En fazla {MAX_AIRPORT_COMBOS} olmalı (anti-ban).",
                )
                return
            origin, dest = ",".join(origins), ",".join(dests)

            dep_start, dep_end, derr = _parse_date_range(args[2])
            if derr:
                await self._reply(update, f"❌ {derr}")
                return

            ret_start = ret_end = None
            price_raw = args[3]
            if len(args) == 5:
                if flags["nights"]:
                    await self._reply(
                        update,
                        "❌ Aynı anda hem dönüş tarihi hem <code>nights=</code> verilemez. "
                        "Ya sabit dönüş tarihi yaz, ya da yalnızca nights kullan.",
                    )
                    return
                ret_start, ret_end, rerr = _parse_date_range(args[3])
                price_raw = args[4]
                if rerr:
                    await self._reply(update, f"❌ Dönüş: {rerr}")
                    return

            today = datetime.now().strftime("%Y-%m-%d")
            if dep_start < today:
                await self._reply(update, "❌ Gidiş tarihi geçmişte olamaz.")
                return
            if ret_start and (ret_end or ret_start) < dep_start:
                await self._reply(update, "❌ Dönüş tarihi gidişten önce olamaz.")
                return

            threshold = _parse_price(price_raw)
            if threshold is None:
                await self._reply(update, "❌ Hedef fiyat pozitif bir sayı olmalı (örn. 3000).")
                return

            chat_id = update.effective_chat.id
            route = Route(None, chat_id, origin, dest, dep_start, threshold, "TRY",
                          return_date=ret_start, date_end=dep_end, return_date_end=ret_end,
                          passengers=flags["pax"], cabin=flags["cabin"],
                          flex_days=flags["flex"], nights=flags["nights"], pos=flags["pos"],
                          direct_only=flags["direct"])
            rid = await self.db.add_route(route)
            route.id = rid
            await self._reply(
                update,
                f"✅ Rota eklendi (#{rid}):\n<b>{route.label()}</b>\n"
                f"Hedef: <b>{threshold:.0f} TRY</b> altına düşünce haber vereceğim.",
            )
            log.info("Rota eklendi #%s chat=%s %s", rid, chat_id, route.label())
        except Exception as exc:  # noqa: BLE001
            log.warning("cmd_add hatası: %s", exc)
            await self._reply(update, "⚠️ Rota eklenirken bir hata oldu, tekrar dener misin?")

    async def cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            text, markup = await self._render_routes(update.effective_chat.id)
            await self._reply(update, text, markup)
        except Exception as exc:  # noqa: BLE001
            log.warning("cmd_list hatası: %s", exc)
            await self._reply(update, "⚠️ Rotalar listelenirken hata oldu.")

    async def cmd_delete(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._simple_id_action(update, ctx, self._do_delete)

    async def cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._simple_id_action(update, ctx, self._do_pause)

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._simple_id_action(update, ctx, self._do_resume)

    async def _simple_id_action(self, update, ctx, action) -> None:
        try:
            args = ctx.args or []
            if len(args) != 1 or not args[0].isdigit():
                await self._reply(update, "❌ Kullanım: komuttan sonra rota id ver (örn. 3).")
                return
            msg = await action(update.effective_chat.id, int(args[0]))
            await self._reply(update, msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("id-action hatası: %s", exc)
            await self._reply(update, "⚠️ İşlem sırasında hata oldu.")

    async def cmd_threshold(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            args = ctx.args or []
            if len(args) != 2 or not args[0].isdigit():
                await self._reply(update, "❌ Kullanım: <code>/esik &lt;id&gt; &lt;fiyat&gt;</code> (örn. /esik 3 2500)")
                return
            price = _parse_price(args[1])
            if price is None:
                await self._reply(update, "❌ Fiyat pozitif bir sayı olmalı.")
                return
            ok = await self.db.update_threshold(int(args[0]), update.effective_chat.id, price)
            await self._reply(
                update,
                f"✅ #{args[0]} hedefi {price:.0f} TRY olarak güncellendi." if ok
                else f"❓ #{args[0]} bulunamadı (sana ait olmayabilir).",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("cmd_threshold hatası: %s", exc)
            await self._reply(update, "⚠️ Eşik güncellenirken hata oldu.")

    async def cmd_history(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            args = ctx.args or []
            if len(args) != 1 or not args[0].isdigit():
                await self._reply(update, "❌ Kullanım: <code>/gecmis &lt;id&gt;</code> (örn. /gecmis 3)")
                return
            route = await self.db.get_route(int(args[0]), update.effective_chat.id)
            if not route:
                await self._reply(update, f"❓ #{args[0]} bulunamadı (sana ait olmayabilir).")
                return
            stats = await self.db.price_stats(route.id)
            if not stats:
                await self._reply(update, f"📭 #{route.id} <b>{route.label()}</b> için henüz fiyat kaydı yok.")
                return
            recent = await self.db.recent_prices(route.id, 30)
            trend = "↘️ düşüyor" if stats.latest < stats.first else (
                "↗️ yükseliyor" if stats.latest > stats.first else "➡️ sabit")
            spark = _sparkline(recent[-16:])
            sig = signal_line(recent) if len(recent) >= 5 else None
            await self._reply(
                update,
                f"📈 <b>{route.label()}</b>  (#{route.id})\n"
                f"Hedef: {route.threshold:.0f} {route.currency}\n\n"
                f"Güncel : <b>{stats.latest:.0f}</b> {route.currency}  ({trend})\n"
                f"En ucuz: {stats.minimum:.0f}   En pahalı: {stats.maximum:.0f}\n"
                f"Ortalama: {stats.average:.0f}   Ölçüm: {stats.count}\n"
                + (f"\n<code>{spark}</code>" if spark else "")
                + (f"\n\n{sig}" if sig else ""),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("cmd_history hatası: %s", exc)
            await self._reply(update, "⚠️ Geçmiş alınırken hata oldu.")

    async def cmd_chart(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            args = ctx.args or []
            if len(args) != 1 or not args[0].isdigit():
                await self._reply(update, "❌ Kullanım: <code>/grafik &lt;id&gt;</code> (örn. /grafik 3)")
                return
            route = await self.db.get_route(int(args[0]), update.effective_chat.id)
            if not route:
                await self._reply(update, f"❓ #{args[0]} bulunamadı (sana ait olmayabilir).")
                return
            points = await self.db.price_series(route.id, 200)
            if len(points) < 2:
                await self._reply(
                    update,
                    f"📭 #{route.id} için grafik çizecek kadar veri yok (en az 2 ölçüm gerekir).",
                )
                return
            # Bloklayıcı render'ı event loop dışında çalıştır.
            png = await asyncio.to_thread(
                render_price_chart, route.label(), points, route.threshold, route.currency
            )
            await update.effective_message.reply_photo(
                photo=png, caption=f"📈 {route.label()} — son {len(points)} ölçüm",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("cmd_chart hatası: %s", exc)
            await self._reply(update, "⚠️ Grafik oluşturulurken hata oldu.")

    async def cmd_compare(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Aynı uçuşu farklı ülke satış noktalarında karşılaştırır (en ucuz POS)."""
        try:
            args = ctx.args or []
            if not args or not args[0].isdigit():
                await self._reply(
                    update,
                    "❌ Kullanım: <code>/karsilastir &lt;id&gt; [TR,GB,DE]</code>\n"
                    f"Geçerli ülkeler: {', '.join(POS_CATALOG)}",
                )
                return
            route = await self.db.get_route(int(args[0]), update.effective_chat.id)
            if not route:
                await self._reply(update, f"❓ #{args[0]} bulunamadı (sana ait olmayabilir).")
                return
            if self.scraper is None:
                await self._reply(update, "⚠️ Karşılaştırma motoru şu an kullanılamıyor.")
                return

            # Kodlar: argüman > rotanın pos'u > varsayılan küme
            if len(args) >= 2:
                codes, bad = _normalize_pos(args[1])
                if bad:
                    await self._reply(update, f"❌ Geçersiz POS kodu: {bad}.")
                    return
            else:
                codes = route.pos_codes() or list(DEFAULT_POS)

            await self._reply(
                update,
                f"🔎 <b>{route.label()}</b> için {len(codes)} satış noktası taranıyor "
                f"({', '.join(codes)})… bu birkaç dakika sürebilir.",
            )
            results = await self.scraper.compare_pos(route, codes)
            lines = [f"💱 <b>{route.label()}</b> — satış noktası karşılaştırması"]
            best_code, best_price = None, None
            for code, price in results:
                name = POS_CATALOG.get(code, code)
                if price is None:
                    lines.append(f"• {name} ({code}): —")
                else:
                    if best_price is None:
                        best_code, best_price = code, price
                    star = " ⭐" if code == best_code else ""
                    lines.append(f"• {name} ({code}): <b>{price:.0f} TRY</b>{star}")
            if best_price is not None and len(results) > 1:
                home = next((p for c, p in results if c == "TR"), None)
                if home and best_price < home:
                    lines.append(f"\n✅ En ucuz <b>{POS_CATALOG.get(best_code)}</b>: "
                                 f"TR'ye göre {home - best_price:.0f} TRY tasarruf.")
            lines.append("\n⚠️ Fiyat Google'ın kuruyla TL'ye çevrilidir; gerçek ödemede "
                         "kart döviz komisyonu olabilir ve bazı havayolları farklı satış "
                         "noktasında alınan bileti reddedebilir.")
            await self._reply(update, "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            log.warning("cmd_compare hatası: %s", exc)
            await self._reply(update, "⚠️ Karşılaştırma sırasında hata oldu.")

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            total = await self.db.count_active()
            mine = len(await self.db.list_routes(update.effective_chat.id))
            last = ctx.application.bot_data.get("last_scan_at") or "henüz tarama yapılmadı"
            await self._reply(
                update,
                f"📊 <b>Durum</b>\n"
                f"• Senin rotaların: <b>{mine}</b>\n"
                f"• Sistemdeki aktif rota: <b>{total}</b>\n"
                f"• Son tarama: {last}",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("cmd_status hatası: %s", exc)
            await self._reply(update, "⚠️ Durum alınırken hata oldu.")

    # ------------------------------------------------------------- callbacks
    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        try:
            await query.answer()
            data = query.data or ""
            action, _, raw_id = data.partition(":")
            if not raw_id.isdigit():
                return
            route_id = int(raw_id)
            chat_id = query.message.chat.id

            if action == "del":
                msg = await self._do_delete(chat_id, route_id)
            elif action == "pause":
                msg = await self._do_pause(chat_id, route_id)
            elif action == "resume":
                msg = await self._do_resume(chat_id, route_id)
            else:
                return

            # Listeyi yerinde tazele.
            text, markup = await self._render_routes(chat_id)
            await query.edit_message_text(
                f"{text}\n\n<i>{msg}</i>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("on_callback hatası: %s", exc)

    # ---------------------------------------------------------- action verbs
    async def _do_delete(self, chat_id: int, route_id: int) -> str:
        ok = await self.db.delete_route(route_id, chat_id)
        return f"🗑️ #{route_id} silindi." if ok else f"❓ #{route_id} bulunamadı."

    async def _do_pause(self, chat_id: int, route_id: int) -> str:
        ok = await self.db.set_active(route_id, chat_id, False)
        return f"⏸️ #{route_id} duraklatıldı." if ok else f"❓ #{route_id} bulunamadı."

    async def _do_resume(self, chat_id: int, route_id: int) -> str:
        ok = await self.db.set_active(route_id, chat_id, True)
        return f"▶️ #{route_id} tekrar aktif." if ok else f"❓ #{route_id} bulunamadı."
