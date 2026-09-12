#!/usr/bin/env python3
"""competitors.py — общий слой мониторинга конкурентов: конфиг + БД + хелперы.

Импортируется матчером (playbooks/competitor-matching.md → claude -p) и
watcher'ом (watchers/competitor_prices.py). Сам ничего не сетевит — сеть в
ozon_scout.py. Здесь только состояние.

Схема БД (data/store.sqlite):
  competitor_cards  — какие SKU конкурентов трекаем (идентичность, к нашему SKU)
  competitor_prices — append-only история наблюдений цены/остатка (обе цены!)
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "store.sqlite"
CONFIG = ROOT / "config" / "competitors.yaml"


# ---------- конфиг ----------

def load_config() -> dict:
    if yaml is None:
        raise RuntimeError("нужен pyyaml: pip install pyyaml")
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    """Пишем конфиг обратно (матчер обновляет matched[]). Сохраняем читабельность."""
    if yaml is None:
        raise RuntimeError("нужен pyyaml")
    CONFIG.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


# ---------- БД ----------

def db() -> sqlite3.Connection:
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("""CREATE TABLE IF NOT EXISTS competitor_cards(
        competitor_sku TEXT PRIMARY KEY,
        our_sku        TEXT,          -- наш SKU, с которым конкурирует
        name           TEXT,
        config         TEXT,          -- RC | body | ? (комплектация, как её понял матчер)
        note           TEXT,          -- заметка матчера (почему это конкурент)
        url            TEXT,
        matched_by     TEXT,          -- 'claude-matcher' | 'manual'
        first_seen     INTEGER,
        last_seen      INTEGER,
        active         INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS competitor_prices(
        ts             INTEGER,
        competitor_sku TEXT,
        our_sku        TEXT,
        price          INTEGER,       -- розовая (Ozon-Карта, минимальная) — метрика сравнения
        orig_price     INTEGER,       -- зачёркнутая (контекст)
        stock_left     INTEGER,       -- если Ozon показал остаток
        rating         REAL,
        reviews        INTEGER,
        is_ad          INTEGER,
        found_via      TEXT)""")       # found_via: 'our_own' — наша плитка, иначе query
    c.execute("""CREATE INDEX IF NOT EXISTS ix_cp_sku_ts
        ON competitor_prices(competitor_sku, ts)""")
    # competitor_sales — append-only наблюдения ПРОДАЖ/ОСТАТКОВ из MPStats-плагина
    # (tools/mpstats.py). Отдельно от competitor_prices (та — витрина, только цена).
    # Тут то, чего витрина не даёт: заказы, выручка 30д, дни в наличии, реальная
    # цена продажи по дням (pricesGraph), реклама. graphs — сырые 30-дн. ряды (JSON).
    c.execute("""CREATE TABLE IF NOT EXISTS competitor_sales(
        ts             INTEGER,
        competitor_sku TEXT,
        model          TEXT,          -- наша модель, к которой отнесён (контекст, из dji_competitors.json)
        config         TEXT,          -- комплектация (body/rc/fmc/...)
        seller         TEXT,
        seller_id      INTEGER,
        brand          TEXT,
        stock          INTEGER,        -- Count — текущий остаток
        days_on_stocks INTEGER,        -- дней в наличии за период
        orders_per_day REAL,
        orders_30d     INTEGER,        -- Totals.orders — заказов за 30д
        revenue_30d    INTEGER,        -- Totals.sum — выручка ₽ за 30д
        revenue_prev   INTEGER,        -- Totals.sumPrev — пред. период (тренд)
        price_est      INTEGER,        -- revenue_30d/orders_30d — РЕАЛЬНАЯ ср. цена/шт (есть всегда из батча)
        price_last     INTEGER,        -- pricesGraph[-1] — цена посл. дня (только single-SKU, иначе NULL)
        price_avg      INTEGER,        -- средняя по pricesGraph (только single-SKU, иначе NULL)
        ext_advertising INTEGER,       -- продвижение в поиске Ozon
        ext_yandex     INTEGER,        -- реклама в Яндексе
        ext_google     INTEGER,
        graphs         TEXT)""")       # JSON: {orders,count,prices,visibility} по дням
    c.execute("""CREATE INDEX IF NOT EXISTS ix_cs_sku_ts
        ON competitor_sales(competitor_sku, ts)""")
    # мягкая миграция: price_est добавлен позже — дошиваем в уже созданную таблицу
    cols = {r[1] for r in c.execute("PRAGMA table_info(competitor_sales)").fetchall()}
    if "price_est" not in cols:
        c.execute("ALTER TABLE competitor_sales ADD COLUMN price_est INTEGER")
    c.commit()
    return c


def upsert_card(c: sqlite3.Connection, competitor_sku: str, our_sku: str, name: str,
                config: str, note: str, url: str | None, matched_by: str = "claude-matcher") -> bool:
    """Завести/освежить карточку конкурента. Возвращает True, если НОВАЯ."""
    now = int(time.time())
    row = c.execute("SELECT competitor_sku FROM competitor_cards WHERE competitor_sku=?",
                    (competitor_sku,)).fetchone()
    if row:
        c.execute("UPDATE competitor_cards SET last_seen=?, active=1, our_sku=?, name=?, "
                  "config=?, note=?, url=? WHERE competitor_sku=?",
                  (now, our_sku, name, config, note, url, competitor_sku))
        return False
    c.execute("INSERT INTO competitor_cards VALUES(?,?,?,?,?,?,?,?,?,1)",
              (competitor_sku, our_sku, name, config, note, url, matched_by, now, now))
    return True


def record_price(c: sqlite3.Connection, tile: dict, our_sku: str, found_via: str) -> None:
    """Записать наблюдение цены (одна строка истории). tile — из ozon_scout.parse_tile."""
    c.execute("INSERT INTO competitor_prices VALUES(?,?,?,?,?,?,?,?,?,?)",
              (int(time.time()), tile["sku"], our_sku, tile.get("price"),
               tile.get("orig_price"), tile.get("stock_left"), tile.get("rating"),
               tile.get("reviews"), 1 if tile.get("is_ad") else 0, found_via))


def last_price(c: sqlite3.Connection, competitor_sku: str) -> dict | None:
    """Последнее наблюдение по конкуренту (для diff между прогонами)."""
    r = c.execute("SELECT ts, price, orig_price, stock_left FROM competitor_prices "
                  "WHERE competitor_sku=? ORDER BY ts DESC LIMIT 1", (competitor_sku,)).fetchone()
    if not r:
        return None
    return {"ts": r[0], "price": r[1], "orig_price": r[2], "stock_left": r[3]}


def record_sales(c: sqlite3.Connection, item: dict) -> None:
    """Записать наблюдение продаж/остатка (одна строка). item — из mpstats.parse_item."""
    import json as _json
    c.execute(
        "INSERT INTO competitor_sales(ts,competitor_sku,model,config,seller,seller_id,brand,"
        "stock,days_on_stocks,orders_per_day,orders_30d,revenue_30d,revenue_prev,"
        "price_est,price_last,price_avg,ext_advertising,ext_yandex,ext_google,graphs) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), item["sku"], item.get("model"), item.get("config"),
         item.get("seller"), item.get("seller_id"), item.get("brand"),
         item.get("stock"), item.get("days_on_stocks"), item.get("orders_per_day"),
         item.get("orders_30d"), item.get("revenue_30d"), item.get("revenue_prev"),
         item.get("price_est"), item.get("price_last"), item.get("price_avg"),
         1 if item.get("ext_advertising") else 0,
         1 if item.get("ext_yandex") else 0,
         1 if item.get("ext_google") else 0,
         _json.dumps(item.get("graphs") or {}, ensure_ascii=False)))


def last_sales(c: sqlite3.Connection, competitor_sku: str) -> dict | None:
    """Последнее наблюдение продаж по конкуренту (для diff между прогонами)."""
    r = c.execute("SELECT ts, stock, orders_30d, revenue_30d, price_last FROM competitor_sales "
                  "WHERE competitor_sku=? ORDER BY ts DESC LIMIT 1", (competitor_sku,)).fetchone()
    if not r:
        return None
    return {"ts": r[0], "stock": r[1], "orders_30d": r[2], "revenue_30d": r[3], "price_last": r[4]}


def active_cards(c: sqlite3.Connection, our_sku: str | None = None) -> list[dict]:
    q = "SELECT competitor_sku, our_sku, name, config, note, url FROM competitor_cards WHERE active=1"
    args: tuple = ()
    if our_sku:
        q += " AND our_sku=?"
        args = (our_sku,)
    return [dict(competitor_sku=r[0], our_sku=r[1], name=r[2], config=r[3], note=r[4], url=r[5])
            for r in c.execute(q, args).fetchall()]
