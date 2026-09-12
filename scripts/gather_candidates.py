#!/usr/bin/env python3
"""gather_candidates.py — детерминированная часть матчинга: собрать кандидатов.

Для каждого нашего SKU из config/competitors.yaml прогоняет ozon_scout по его
queries в ценовом коридоре, склеивает выдачу, выкидывает наши же карточки и явные
аксессуары, и пишет кандидатов в state/competitor_candidates.json.

Дальше judgment делает LLM (playbooks/competitor-matching.md → claude -p): решает,
какие кандидаты — настоящие конкуренты той же модели/комплектации.

Запуск: python3 scripts/gather_candidates.py [--pages N]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import competitors as C
from ozon_scout import search

# Явные аксессуары/расходка — режем по ключевым словам (коридор ловит не всё,
# бывает дорогой аккумулятор/кейс). Матчер их всё равно не должен видеть.
ACCESSORY_KW = [
    "лопаст", "пропеллер", "винт", "аккумулятор", "акб", "батаре", "зарядн",
    "чехол", "кейс", "сумк", "рюкзак", "защит", "фильтр", "бленда", "шлейф",
    "демпфер", "амортизатор", "мотор", "двигател", "корпус", "луч", "подвес",
    "ремень", "стик", "наклейк", "плёнк", "пленк", "переходник", "хаб", "кабел",
    "адаптер", "крышк", "заглушк", "комплект лопаст", "запчаст", "sd", "карта памяти",
]


def looks_accessory(name: str) -> bool:
    n = (name or "").lower()
    return any(kw in n for kw in ACCESSORY_KW)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=None, help="переопределить pages из конфига")
    args = ap.parse_args()

    cfg = C.load_config()
    our_skus = set(cfg["products"].keys())
    default_pages = cfg.get("defaults", {}).get("pages", 3)

    out: dict = {}
    for our_sku, p in cfg["products"].items():
        pages = args.pages or default_pages
        lo, hi = p.get("price_band", [None, None])
        merged: dict[str, dict] = {}
        for q in p["queries"]:
            for tile in search(q, pages=pages, price_min=lo, price_max=hi):
                if not tile["sku"] or tile["price"] is None:
                    continue
                if tile["sku"] in our_skus:          # наши карточки — не конкуренты
                    continue
                if looks_accessory(tile["name"]):    # явная расходка
                    continue
                # первое вхождение; помним, каким запросом нашли
                if tile["sku"] not in merged:
                    tile = {**tile, "found_via": q}
                    merged[tile["sku"]] = tile

        cands = sorted(merged.values(), key=lambda t: t["price"])
        out[our_sku] = {
            "our_name": p["name"],
            "model": p.get("model"),
            "config": p.get("config"),
            "price_band": p.get("price_band"),
            "already_matched": [m["sku"] for m in p.get("matched", []) if isinstance(m, dict)],
            "candidates": cands,
        }
        print(f"{our_sku} {p['name'][:40]:<40} кандидатов: {len(cands)}", file=sys.stderr)

    dst = ROOT / "state" / "competitor_candidates.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {dst}", file=sys.stderr)


if __name__ == "__main__":
    main()
