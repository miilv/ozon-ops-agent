#!/usr/bin/env python3
"""Stop-hook: если сессия делала мутации (action_log за время сессии),
а state/docs не тронуты и незакоммичены — блокируем завершение один раз."""
import json, sqlite3, subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
data = json.load(sys.stdin)
if data.get("stop_hook_active"):  # уже блокировали — не зацикливаемся
    sys.exit(0)
# мутации за последние 2 часа этой сессией (грубая эвристика по времени)
try:
    c = sqlite3.connect(ROOT/"data"/"store.sqlite", timeout=10)
    n = c.execute("SELECT COUNT(*) FROM action_log WHERE source='ozon.py' AND kind='call' AND ts > ?",
                  (int(time.time())-7200,)).fetchone()[0]
    c.close()
except Exception:
    n = 0
if not n:
    sys.exit(0)
dirty = subprocess.run(["git","status","--porcelain","state/","docs/"],
                       cwd=ROOT, capture_output=True, text=True).stdout.strip()
# есть мутации и есть незакоммиченный след -> ок (сессия записала, коммит мог сделать)
# есть мутации и НЕТ ни диффа, ни свежего журнала -> блок
import glob, os
today = time.strftime("%Y-%m-%d")
fresh_journal = any(today in f for f in glob.glob(str(ROOT/"docs"/"journal"/"*.md")))
if dirty or fresh_journal:
    sys.exit(0)
print(json.dumps({"decision":"block",
  "reason":"Ты выполнял мутирующие вызовы Ozon API, но нет следа: обнови state/NOW.md "
           "(python3 tools/now.py add ...) и/или docs/journal/, закоммить state/ docs/ и завершай."}))
