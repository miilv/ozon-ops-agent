#!/usr/bin/env python3
"""competitor_sales_estimate.py — грубая оценка продаж конкурентов по динамике остатков.

Бесплатный прокси к продажам, пока нет MPStats API. Ozon показывает в плитке остаток
(«N шт»); падение остатка между прогонами ≈ продажи. КАВЕАТ: приблизительно — продавец
докладывает сток (тогда рост, продажи не видны), Ozon показывает остаток не всегда и
режет большие числа. Это оценка порядка, не факт. Реальные продажи — только MPStats.

Считает по истории competitor_prices за окно (дней): сумму снижений stock_left.
Запуск: python3 scripts/competitor_sales_estimate.py [--days 14]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import competitors as C


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    since = int(time.time()) - args.days * 86400

    c = C.db()
    cfg = C.load_config()
    names = {}
    for csku, name in c.execute("SELECT competitor_sku, name FROM competitor_cards").fetchall():
        names[csku] = name

    print(f"# Оценка продаж по остаткам за {args.days} дн (приближённо!)\n")
    for our_sku, p in cfg["products"].items():
        rows = c.execute(
            "SELECT competitor_sku, ts, stock_left FROM competitor_prices "
            "WHERE our_sku=? AND found_via!='our_own' AND stock_left IS NOT NULL AND ts>=? "
            "ORDER BY competitor_sku, ts", (our_sku, since)).fetchall()
        by: dict[str, list] = {}
        for csku, ts, st in rows:
            by.setdefault(csku, []).append(st)
        est = []
        for csku, series in by.items():
            sold = 0
            for a, b in zip(series, series[1:]):
                if b < a:                    # остаток упал — считаем продажами
                    sold += a - b
            if len(series) >= 2:
                est.append((csku, sold, len(series)))
        if not est:
            continue
        print(f"## {p['name'][:42]}")
        for csku, sold, n in sorted(est, key=lambda x: -x[1]):
            print(f"   ~{sold:>4} шт  ({n} набл.)  {names.get(csku,'')[:46]}")
        print()


if __name__ == "__main__":
    main()
