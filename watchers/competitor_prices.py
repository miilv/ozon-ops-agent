#!/usr/bin/env python3
"""Датчик цен конкурентов. Детерминированный, без LLM. Cron: раз в день.

Для каждого нашего SKU с подтверждёнными конкурентами (config.matched):
  1. свежий прогон ozon_scout по queries+коридору;
  2. из ОДНОЙ выдачи берём нашу плитку и плитки конкурентов — один слой цены
     (розовая = Ozon-Карта, минимальная видимая), сравнение min-to-min;
  3. пишем наблюдения в competitor_prices (обе цены + остаток);
  4. сигналы: кто дешевле нас (undercut), кто подвинул цену (>move_pct), новые лоу;
  5. при сигнале — дайджест в TG. Всегда — снапшот в state/competitors_pulse.md.

Наш якорь цены — НАША ЖЕ плитка (тот же слой), а не Seller API (там нет СПП/Карты).
Если нашей плитки в выдаче нет — добираем find_sku, иначе помечаем пропуск.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import json
import urllib.request

import competitors as C
from ozon_scout import search, find_sku


def env() -> dict:
    e = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            e[k.strip()] = v.strip()
    return e


ENV = env()


DRY = "--dry" in sys.argv


def tg_send(text: str) -> None:
    if DRY:
        print("[DRY] TG-сообщение:\n" + text + "\n")
        return
    token, chat = ENV.get("TG_BOT_TOKEN", ""), ENV.get("TG_TEAM_CHAT", "")
    if not token or not chat:
        print("TG creds missing; would send:\n" + text)
        return
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": int(chat), "text": text, "parse_mode": "HTML"}).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def rub(n) -> str:
    return f"{int(n):,}".replace(",", " ") + " ₽" if n is not None else "—"


def pct_move(cur: int | None, prev: int | None) -> float | None:
    if not cur or not prev:
        return None
    return (cur - prev) / prev * 100.0


def main() -> None:
    cfg = C.load_config()
    defaults = cfg.get("defaults", {})
    move_pct = float(defaults.get("alert", {}).get("move_pct", 4))
    pages = int(defaults.get("pages", 3))
    c = C.db()

    signals: list[str] = []       # строки для TG
    snapshot: list[str] = []      # строки для state-файла
    now_str = time.strftime("%d.%m %H:%M")

    for our_sku, p in cfg["products"].items():
        matched = {str(m["sku"]): m for m in p.get("matched", []) if isinstance(m, dict)}
        if not matched:
            continue

        lo, hi = p.get("price_band", [None, None])
        # одна выдача на SKU: и мы, и конкуренты в ней
        pool: dict[str, dict] = {}
        for q in p["queries"]:
            for t in search(q, pages=pages, price_min=lo, price_max=hi):
                pool.setdefault(t["sku"], t)

        # наша цена — из нашей же плитки
        our_tile = pool.get(our_sku) or find_sku(our_sku, p["queries"], pages=pages)
        our_price = our_tile["price"] if our_tile else None
        if our_tile:
            C.record_price(c, our_tile, our_sku, found_via="our_own")

        rows = []
        for csku, m in matched.items():
            tile = pool.get(csku)
            if not tile:
                # конкурент не всплыл в этом прогоне — не наказываем, просто пропуск
                rows.append((csku, m, None, None, None))
                continue
            prev = C.last_price(c, csku)
            prev_price = prev["price"] if prev else None
            C.record_price(c, tile, our_sku, found_via=tile.get("found_via", "watch"))
            mv = pct_move(tile["price"], prev_price)
            rows.append((csku, m, tile, mv, prev_price))

        c.commit()  # инкрементально: партиал переживёт обрыв
        print(f"  {our_sku} {p['name'][:32]:<32} our={our_price} "
              f"конкурентов_в_выдаче={sum(1 for r in rows if r[2])}/{len(matched)}",
              flush=True)

        present = [(csku, m, t, mv, pp) for csku, m, t, mv, pp in rows if t]
        cur_cheapest = min((t["price"] for _, _, t, _, _ in present), default=None)
        prev_prices = [pp for _, _, _, _, pp in present if pp]
        prev_cheapest = min(prev_prices) if prev_prices else None
        undercutters = [(csku, m, t) for csku, m, t, _, _ in present
                        if our_price and t["price"] and t["price"] < our_price]

        # снапшот-строка (стоячее состояние — всегда сюда, не в TG)
        head = f"<b>{p['name'][:38]}</b> — наша {rub(our_price)}"
        if cur_cheapest is not None:
            head += f", мин. у конкурентов {rub(cur_cheapest)}"
        snapshot.append(head)
        for csku, m, t, mv, pp in sorted(present, key=lambda z: z[2]["price"]):
            tag = " 🔻дешевле нас" if our_price and t["price"] < our_price else ""
            stock = f" · {t['stock_left']}шт" if t.get("stock_left") else ""
            snapshot.append(f"   {rub(t['price'])} — {m.get('name','')[:40]}{stock}{tag}")

        # TG-сигнал по подрезу — только на ИЗМЕНЕНИЕ, не стоячее состояние (правило 7):
        # первый прогон по SKU (базлайн) ИЛИ конкуренты обновили лоу ниже нас.
        first_run = prev_cheapest is None
        worsened = prev_cheapest is not None and cur_cheapest is not None and cur_cheapest < prev_cheapest
        if undercutters and (first_run or worsened):
            why = "стартовый срез" if first_run else f"новый минимум {rub(cur_cheapest)} (было {rub(prev_cheapest)})"
            lines = "\n".join(
                f"   • {rub(t['price'])} ({(our_price - t['price']) / our_price * 100:.0f}% ниже нас) "
                f"— {m.get('name','')[:38]}"
                for csku, m, t in sorted(undercutters, key=lambda z: z[2]["price"]))
            signals.append(f"🔻 <b>{p['name'][:38]}</b> — {why} (наша {rub(our_price)}):\n{lines}")
        # движения цены конкретных конкурентов (change-based по своей природе)
        for csku, m, t, mv, pp in present:
            if mv is not None and abs(mv) >= move_pct:
                arrow = "↓" if mv < 0 else "↑"
                signals.append(f"{arrow} {m.get('name','')[:38]}: {rub(t['price'])} "
                               f"({mv:+.0f}% с прошлого прогона)")

    c.commit()
    c.close()

    # снапшот всегда
    pulse = ROOT / "state" / "competitors_pulse.md"
    pulse.write_text(f"# Пульс конкурентов — {now_str}\n\n" + "\n".join(snapshot) + "\n",
                     encoding="utf-8")

    # TG только при сигнале (правило 7)
    if signals:
        tg_send(f"🏷 <b>Цены конкурентов</b> ({now_str})\n\n" + "\n\n".join(signals))
        print(f"{'[DRY] НЕ отправлено' if DRY else 'Отправлен алерт'}: {len(signals)} сигналов")
    else:
        print("Сигналов нет — снапшот обновлён, TG молчит")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            tg_send(f"⚠️ competitor_prices упал: {type(exc).__name__}: {exc}")
        finally:
            raise
