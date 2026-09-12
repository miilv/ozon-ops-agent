#!/usr/bin/env bash
# Раннер матчинга конкурентов через claude -p (headless). Крон: раз в N дней.
# Собирает кандидатов → Claude сопоставляет по playbooks/competitor-matching.md →
# apply_matches пишет matched[] в config/competitors.yaml → раннер синкает в git.
#
# Самосинхронизация (config — code-like, едет через git; у команды была потеря через
# git → аккуратно): pull ДО, commit+push ПОСЛЕ и только если yaml реально изменился.
# Требует: claude в PATH, .env (ключи Ozon + OZON_SCOUT_PROXY на сервере), curl_cffi, pyyaml.
set -uo pipefail
cd "$(dirname "$0")/.."

# свежий конфиг перед матчингом (autostash — на случай gitignored-шума в дереве)
git pull --rebase --autostash origin main 2>&1 | tail -2 || echo "pull warn (продолжаю)"

PROMPT='Ты матчер конкурентов. Выполни строго playbooks/competitor-matching.md:
1) запусти gather_candidates.py, 2) прочитай state/competitor_candidates.json,
3) реши по правилам, кто настоящий конкурент каждому нашему SKU (та же модель И
комплектация; Fly More Combo, тушки при RC, другие модели, отдельные пульты — НЕ
матч), 4) сформируй JSON решения и примени scripts/apply_matches.py, 5) если есть
новые конкуренты — краткий итог в TG (tools/tg.py send).
ВАЖНО: сам git commit НЕ делай — коммит и push сделает раннер. Отвечай результатом.'

# паттерн как у остальных кранов: bare claude -p, stdin из /dev/null
claude -p "$PROMPT" < /dev/null || echo "claude -p завершился с ошибкой (см. лог)"

# вернуть изменения конфига в git, только если matched реально поменялся
if ! git diff --quiet config/competitors.yaml 2>/dev/null; then
  git add config/competitors.yaml
  git commit -q -m "матчер: обновлён список конкурентов $(date +%F)"
  git pull --rebase --autostash origin main 2>&1 | tail -2 || true
  git push origin main 2>&1 | tail -2 || echo "push warn — конфликт, разрулить вручную"
  echo "конфиг конкурентов обновлён и запушен"
else
  echo "изменений в matched нет — git чист"
fi
