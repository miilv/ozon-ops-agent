#!/usr/bin/env python3
"""Датчик заказов FBS: новые отправления и дедлайны отгрузки → алерт в TG.
Детерминированный, без LLM. Cron: */15 мин. Идемпотентен (SQLite-статусы)."""
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "store.sqlite"
MSK = timezone(timedelta(hours=3))
DEADLINE_SOON = timedelta(hours=3)

def env() -> dict:
    e = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            e[k.strip()] = v.strip()
    return e

E = env()
TG_TOKEN = E.get("TG_BOT_TOKEN", "")
TG_CHAT = E.get("TG_TEAM_CHAT", "")

def ozon(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        "https://api-seller.ozon.ru" + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Client-Id": E["OZON_CLIENT_ID"], "Api-Key": E["OZON_API_KEY"],
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        print("TG creds missing; would send:\n" + text)
        return
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data=json.dumps({"chat_id": int(TG_CHAT), "text": text, "parse_mode": "HTML"}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        json.loads(r.read())

def db() -> sqlite3.Connection:
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS postings(
        posting_number TEXT PRIMARY KEY, status TEXT, price REAL,
        items TEXT, shipment_date TEXT,
        first_seen TEXT, alerted_new INTEGER DEFAULT 0, alerted_deadline INTEGER DEFAULT 0)""")
    return c

def fetch_postings() -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, offset = [], 0
    while True:
        r = ozon("/v3/posting/fbs/list", {
            "dir": "DESC",
            "filter": {"since": since, "to": to},
            "limit": 100, "offset": offset, "with": {}})
        posts = r.get("result", {}).get("postings", [])
        out += posts
        if not r.get("result", {}).get("has_next"):
            break
        offset += 100
    return out

def fmt_items(p: dict) -> str:
    return "; ".join(f"{x.get('offer_id')}×{x.get('quantity')}" for x in p.get("products", []))

def total(p: dict) -> float:
    return sum(float(x.get("price", 0)) * x.get("quantity", 1) for x in p.get("products", []))

def main() -> None:
    con = db()
    now = datetime.now(timezone.utc)
    active = {"awaiting_packaging", "awaiting_registration", "awaiting_deliver", "awaiting_approve"}
    for p in fetch_postings():
        pn, st = p["posting_number"], p["status"]
        ship = p.get("shipment_date") or ""
        row = con.execute("SELECT status, alerted_new, alerted_deadline FROM postings WHERE posting_number=?", (pn,)).fetchone()
        if row is None:
            con.execute("INSERT INTO postings VALUES(?,?,?,?,?,?,0,0)",
                        (pn, st, total(p), fmt_items(p), ship, now.isoformat()))
            if st in active:
                dl = ""
                if ship:
                    dt = datetime.fromisoformat(ship.replace("Z", "+00:00")).astimezone(MSK)
                    dl = f"\n⏰ Отгрузить до: <b>{dt:%d.%m %H:%M} МСК</b>"
                tg_send(f"🛒 <b>Новый заказ</b> {pn}\n{fmt_items(p)}\n"
                        f"Сумма: {total(p):,.0f} ₽{dl}".replace(",", " "))
                con.execute("UPDATE postings SET alerted_new=1 WHERE posting_number=?", (pn,))
        else:
            if row[0] != st:
                con.execute("UPDATE postings SET status=? WHERE posting_number=?", (st, pn))
                if st == "cancelled":
                    tg_send(f"❌ Заказ {pn} отменён ({fmt_items(p)})")
        # эскалация дедлайна
        if st == "awaiting_packaging" and ship:
            dt = datetime.fromisoformat(ship.replace("Z", "+00:00"))
            already = con.execute("SELECT alerted_deadline FROM postings WHERE posting_number=?", (pn,)).fetchone()
            if dt - now < DEADLINE_SOON and already and not already[0]:
                left = max(dt - now, timedelta(0))
                h, m = divmod(int(left.total_seconds() // 60), 60)
                tg_send(f"🚨 <b>ДЕДЛАЙН</b>: заказ {pn} не собран, до отгрузки {h}ч {m:02d}м!\n"
                        f"{fmt_items(p)} — {total(p):,.0f} ₽".replace(",", " "))
                con.execute("UPDATE postings SET alerted_deadline=1 WHERE posting_number=?", (pn,))
    con.commit()
    con.close()

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # алерт о падении самого датчика
        try:
            tg_send(f"⚠️ orders_watcher упал: {type(exc).__name__}: {exc}")
        finally:
            sys.exit(1)
