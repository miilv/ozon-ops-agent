#!/usr/bin/env python3
"""ozon_scout.py — сбор тайлов поисковой выдачи Ozon через публичный composer-api.

Единственный НАДЁЖНЫЙ бесплатный канал (проверено 19.07.2026):
  GET https://api.ozon.ru/composer-api.bx/page/json/v2?url=/search/?text=<q>&page=<n>
  → 200 без кук, стабильно, пагинация уходит на 11+ страниц (~8 тайлов/стр).
НЕ работают (403 Antibot): карточка /product/<sku>/, /category/..., /seller/...,
поиск по голому номеру SKU (/search/?text=<sku>). Поэтому трекинг конкретного
конкурента = найти его SKU в выдаче текстового запроса, а не дёргать его карточку.

Антибот Ozon режет по TLS-фингерпринту → ходим через curl_cffi impersonate
(имитация TLS реального Chrome). requests/urllib отсюда умирают сразу.

CLI:
  ozon_scout.py search "DJI Neo" --pages 5 [--json]
  ozon_scout.py find-sku <sku> --queries "DJI Neo,DJI Neo дрон" --pages 6

Как модуль:
  from ozon_scout import search
  tiles = search("DJI Neo", pages=5)   # -> list[dict]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    sys.stderr.write("НУЖЕН curl_cffi: pip install curl_cffi\n")
    raise

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "store.sqlite"
API = "https://api.ozon.ru/composer-api.bx/page/json/v2"

# Разные TLS-профили — ротируем при ретраях, чтобы не примелькаться одним отпечатком.
IMPERSONATE = ["chrome131", "chrome124", "chrome120", "chrome123", "edge101"]

# На проде (VPS) прямой доступ к api.ozon.ru таймаутит: transparent-iptables не ловит
# libcurl. Через явный HTTP-прокси (127.0.0.1:7890) — 200. На машине с прямым IP прокси не нужен
# (прямой резид. IP). Управляется env OZON_SCOUT_PROXY (в .env на сервере).
PROXY = os.environ.get("OZON_SCOUT_PROXY") or None
_ENVFILE = ROOT / ".env"
if PROXY is None and _ENVFILE.exists():
    for _l in _ENVFILE.read_text().splitlines():
        _l = _l.strip()
        if _l.startswith("OZON_SCOUT_PROXY=") and "=" in _l:
            PROXY = _l.split("=", 1)[1].strip() or None


def _session(imp: str):
    kw = {"impersonate": imp}
    if PROXY:
        kw["proxies"] = {"http": PROXY, "https": PROXY}
    return cffi_requests.Session(**kw)

_MONEY_RE = re.compile(r"\d")
_STOCK_RE = re.compile(r"(?:осталось\s*)?(\d+)\s*шт", re.IGNORECASE)


def _money(text: str | None) -> int | None:
    """'227 644 ₽' -> 227644 ; None/'' -> None."""
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def _num(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(m.group().replace(",", ".")) if m else None


def _int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", text.replace(" ", " ").replace(" ", ""))
    return int(m.group()) if m else None


def parse_tile(it: dict) -> dict:
    """Один тайл выдачи -> плоский словарь. Устойчив к перестановке полей."""
    sku = str(it.get("id") or it.get("sku") or "")
    name = price = orig = discount = rating = reviews = stock = None
    is_ad = False

    for st in it.get("mainState", []):
        typ = st.get("type")
        if typ == "priceV2":
            pv = st["priceV2"]
            for p in pv.get("price", []):
                if p.get("textStyle") == "PRICE":
                    price = _money(p.get("text"))
                elif p.get("textStyle") == "ORIGINAL_PRICE":
                    orig = _money(p.get("text"))
            discount = pv.get("discount")
        elif typ == "textDS":
            tds = st["textDS"]
            auto = tds.get("testInfo", {}).get("automatizationId", "")
            txt = tds.get("text", "")
            if st.get("id") == "name" or auto == "tile-name":
                name = txt
            elif "Stockbar" in auto or "шт" in txt:
                m = _STOCK_RE.search(txt)
                if m:
                    stock = int(m.group(1))
        elif typ == "labelListV2":
            texts = [x["text"]["text"] for x in st["labelListV2"].get("items", [])
                     if x.get("type") == "text"]
            if texts:
                rating = _num(texts[0])
                if len(texts) > 1:
                    reviews = _int(texts[1])

    # признак рекламной выдачи (проплаченное место) — тоже конкурент, но помечаем
    mb = it.get("multiButton", {}).get("ozonButton", {})
    tracking = json.dumps(mb, ensure_ascii=False)
    if "advertLite" in tracking or "advert" in tracking:
        is_ad = True
    premium_seller = "premium-seller-icon" in tracking

    link = it.get("action", {}).get("link", "")

    return {
        "sku": sku,
        "name": name,
        "price": price,
        "orig_price": orig,
        "discount": discount,
        "rating": rating,
        "reviews": reviews,
        "stock_left": stock,           # None если Ozon не показал (обычно показывает при малом остатке)
        "url": ("https://www.ozon.ru" + link.split("?")[0]) if link else None,
        "is_ad": is_ad,
        "premium_seller": premium_seller,
    }


def _fetch(url_param: str, session, timeout: int = 30):
    return session.get(API, params={"url": url_param}, timeout=timeout)


def search(query: str, pages: int = 3, sort: str | None = None,
           price_min: int | None = None, price_max: int | None = None,
           delay: tuple[float, float] = (1.5, 3.0), max_retries: int = 3) -> list[dict]:
    """Текстовый поиск -> уникальные тайлы (по sku, первое вхождение) с N страниц.

    ГЛАВНЫЙ приём против «мусорной» выдачи — ценовой коридор price_min/price_max.
    Ozon-фильтр цены передаётся как &currency_price=MIN.000-MAX.000 (разделитель —
    ДЕФИС, не «;»! с «;» фильтр молча игнорируется). В коридоре ~цены-нашего-товара
    выдача очищается от аксессуаров (лопасти/АКБ/моторы) и остаются только дроны.
    Разделять комплектации (RC / тушка / другая модель) фильтр НЕ умеет — это работа
    матчера. См. journal 2026-07-19.

    sort='price_desc' — вторичный приём: выдаёт ~36 тайлов/стр, но без коридора всё
    равно тонет в аксессуарах, поэтому основной рычаг — именно цена.

    delay — диапазон паузы между страницами (человеческий ритм). Одиночная сессия
    выдержала 6 быстрых запросов без бана, но в проде ходим медленно.
    """
    import random  # локально: Math.random недоступен в некоторых песочницах агента

    q = query.replace(" ", "+")
    sort_param = f"&sorting={sort}" if sort else ""
    price_param = ""
    if price_min is not None or price_max is not None:
        lo = f"{price_min or 0}.000"
        hi = f"{price_max or 99999999}.000"
        price_param = f"&currency_price={lo}-{hi}"  # ДЕФИС обязателен
    sort_param += price_param
    seen: dict[str, dict] = {}
    imp_idx = 0
    session = _session(IMPERSONATE[0])

    for page in range(1, pages + 1):
        url_param = f"/search/?text={q}&page={page}{sort_param}"
        tiles_here: list[dict] = []
        for attempt in range(max_retries):
            try:
                r = _fetch(url_param, session)
            except Exception as exc:  # сетевой сбой — ретрай с новой сессией
                imp_idx = (imp_idx + 1) % len(IMPERSONATE)
                session = _session(IMPERSONATE[imp_idx])
                time.sleep(1.5 * (attempt + 1))
                if attempt == max_retries - 1:
                    _health(query, page, "neterr", str(exc)[:120])
                continue
            if r.status_code == 200:
                data = r.json()
                for k, v in data.get("widgetStates", {}).items():
                    if "tileGrid" in k or "searchResultsV2" in k:
                        try:
                            for raw in json.loads(v).get("items", []):
                                tiles_here.append(parse_tile(raw))
                        except (json.JSONDecodeError, TypeError):
                            pass
                break
            else:
                # 403/anti-bot — сменить TLS-профиль и подождать
                imp_idx = (imp_idx + 1) % len(IMPERSONATE)
                session = _session(IMPERSONATE[imp_idx])
                time.sleep(2.0 * (attempt + 1))
                if attempt == max_retries - 1:
                    _health(query, page, f"http{r.status_code}", r.text[:120])
        # накопление уникальных
        new = 0
        for t in tiles_here:
            if t["sku"] and t["sku"] not in seen:
                seen[t["sku"]] = t
                new += 1
        if not tiles_here:
            break  # страница пустая/забанена — дальше нет смысла
        if page < pages:
            time.sleep(random.uniform(*delay))

    result = list(seen.values())
    _health(query, pages, "ok", f"{len(result)} tiles")
    return result


def _health(query: str, pages: int, status: str, note: str) -> None:
    """Лёгкая health-метка в БД — чтобы watcher видел деградацию канала."""
    try:
        c = sqlite3.connect(DB, timeout=30)
        c.execute("""CREATE TABLE IF NOT EXISTS scout_health(
            ts INTEGER, query TEXT, pages INTEGER, status TEXT, note TEXT)""")
        c.execute("INSERT INTO scout_health VALUES(?,?,?,?,?)",
                  (int(time.time()), query[:100], pages, status, note[:200]))
        c.commit()
        c.close()
    except Exception:
        pass  # health не должен ронять сбор


def find_sku(sku: str, queries: list[str], pages: int = 6) -> dict | None:
    """Найти конкретный SKU конкурента в выдаче нескольких запросов -> его тайл."""
    target = str(sku)
    for q in queries:
        for t in search(q, pages=pages):
            if t["sku"] == target:
                t["_found_via"] = q
                return t
    return None


def _main() -> None:
    ap = argparse.ArgumentParser(description="Ozon search collector (composer-api)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("search", help="текстовый поиск")
    ps.add_argument("query")
    ps.add_argument("--pages", type=int, default=3)
    ps.add_argument("--sort", default=None,
                    help="price_desc|price|new|rating|score")
    ps.add_argument("--price-min", type=int, default=None, help="ценовой коридор, низ (руб)")
    ps.add_argument("--price-max", type=int, default=None, help="ценовой коридор, верх (руб)")
    ps.add_argument("--json", action="store_true", help="сырой JSON вместо таблицы")

    pf = sub.add_parser("find-sku", help="найти SKU в выдаче запросов")
    pf.add_argument("sku")
    pf.add_argument("--queries", required=True, help="через запятую")
    pf.add_argument("--pages", type=int, default=6)

    args = ap.parse_args()

    if args.cmd == "search":
        tiles = search(args.query, pages=args.pages, sort=args.sort,
                       price_min=args.price_min, price_max=args.price_max)
        if args.json:
            print(json.dumps(tiles, ensure_ascii=False, indent=2))
        else:
            print(f"# '{args.query}': {len(tiles)} уникальных тайлов")
            for t in sorted(tiles, key=lambda x: x["price"] or 1 << 60):
                flags = ("📢" if t["is_ad"] else "  ") + ("⭐" if t["premium_seller"] else " ")
                stock = f" [{t['stock_left']}шт]" if t["stock_left"] else ""
                price = f"{t['price']:,}".replace(",", " ") if t["price"] else "—"
                print(f"{flags} {t['sku']:>11}  {price:>10} ₽{stock}  "
                      f"{(t['name'] or '')[:58]}  ({t['rating'] or '-'}★/{t['reviews'] or 0})")
    elif args.cmd == "find-sku":
        queries = [q.strip() for q in args.queries.split(",") if q.strip()]
        t = find_sku(args.sku, queries, pages=args.pages)
        print(json.dumps(t, ensure_ascii=False, indent=2) if t else "NOT FOUND")


if __name__ == "__main__":
    _main()
