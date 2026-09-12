#!/usr/bin/env python3
"""apply_matches.py — записать решения матчинга в конфиг и БД.

Вход — JSON решений матчера (stdin или файл):
  {
    "<наш sku>": [
      {"sku": "<sku конкурента>", "name": "...", "config": "RC", "note": "та же модель, тот же комплект"},
      ...
    ],
    ...
  }
Записывает matched[] в config/competitors.yaml и upsert в competitor_cards.
Печатает, какие конкуренты НОВЫЕ (не было в БД) — для дайджеста/TG.

LLM (playbooks/competitor-matching.md) только принимает решение и отдаёт этот JSON;
вся запись — здесь, детерминированно.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import competitors as C


def main() -> None:
    raw = Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else sys.stdin.read()
    decisions: dict = json.loads(raw)

    cfg = C.load_config()
    c = C.db()
    now = int(time.time())
    new_ones: list[dict] = []

    for our_sku, matches in decisions.items():
        if our_sku not in cfg["products"]:
            print(f"⚠ пропуск: {our_sku} нет в конфиге", file=sys.stderr)
            continue
        entry = cfg["products"][our_sku]
        matched_list: list[dict] = []
        for m in matches:
            csku = str(m["sku"])
            is_new = C.upsert_card(
                c, competitor_sku=csku, our_sku=our_sku, name=m.get("name", ""),
                config=m.get("config", "?"), note=m.get("note", ""), url=m.get("url"),
            )
            matched_list.append({
                "sku": csku,
                "name": m.get("name", "")[:80],
                "config": m.get("config", "?"),
                "note": m.get("note", ""),
                "first_seen": time.strftime("%Y-%m-%d", time.localtime(now)),
            })
            if is_new:
                new_ones.append({"our_sku": our_sku, **m})
        entry["matched"] = matched_list

    # деактивировать карточки, которых матчер больше не подтвердил (ушли из выдачи)
    confirmed = {str(m["sku"]) for ms in decisions.values() for m in ms}
    for (csku,) in c.execute("SELECT competitor_sku FROM competitor_cards WHERE active=1").fetchall():
        if csku not in confirmed:
            c.execute("UPDATE competitor_cards SET active=0 WHERE competitor_sku=?", (csku,))

    c.commit()
    c.close()
    C.save_config(cfg)

    total = sum(len(v) for v in decisions.values())
    print(f"Записано матчей: {total}. Новых конкурентов: {len(new_ones)}")
    for n in new_ones:
        print(f"  🆕 {n['sku']} {n.get('name','')[:50]} → к нашему {n['our_sku']}")


if __name__ == "__main__":
    main()
