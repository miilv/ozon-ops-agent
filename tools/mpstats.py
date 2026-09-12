#!/usr/bin/env python3
"""mpstats.py — клиент внутреннего API плагина MPStats (продажи/остатки конкурентов).

Зареверсен из расширения MPStats для Chrome (v4.254, 19.07.2026). Расширение —
это Vue-приложение, которое на карточке Ozon дёргает свой backend и рисует сайдбар
с продажами. Весь трафик идёт через ОДИН JSON-RPC эндпоинт:

    POST https://plugin.mpstats.io/pluginapi
    Content-Type: application/json
    Cookie: <сессия mpstats.io>            # httpOnly на домене .mpstats.io

Тело для Ozon (главный «on-page» запрос, БЕЗ поля Request — так и шлёт плагин):
    {"Place":"ozon", "Sku":[<id>, ...], "ozFBS":false, "pver":"4.254"}
→ {"code":200, "days":30, "items": { "<sku>": { ...метрики... } }}

КЛЮЧЕВЫЕ ФАКТЫ РЕВЕРСА (проверено живьём в браузере владельца):
  • Авторизация = сессионная кука mpstats.io. Аккаунт БЕСПЛАТНЫЙ (платный тариф
    истёк), но у плагина своя квота: userInfo → plugin.available=13000, use растёт.
  • БАТЧ: массив Sku в одном запросе. 205 SKU разом → 199 вернулось, 239 мс,
    расход квоты = 1 (не 205!). userInfo квоту НЕ тратит. → полный обход всех
    конкурентов = 1 единица/день, 13000 хватит на годы.
  • Кука httpOnly → из JS страницы не читается. Достаётся руками из DevTools
    (см. `mpstats.py whoami` / README-секцию ниже) и кладётся в .env как
    MPSTATS_COOKIE. Формат — целиком строка заголовка Cookie.

Что отдаёт по каждому SKU (30-дн. окно):
  Count           — текущий остаток (шт)
  DaysOnStocks    — дней в наличии за период
  OrdersPerDay    — заказов в день (сглаженное)
  Seller/SellerId — продавец
  Totals.orders   — заказов за 30д
  Totals.sum      — ВЫРУЧКА ₽ за 30д ; Totals.sumPrev — пред. период (тренд)
  pricesGraph[30] — РЕАЛЬНАЯ цена продажи по дням (то, чего витрина не даёт:
                    цена после СПП/Ozon-Карты — решает давнюю боль прайсинга)
  ordersGraph[30] / countGraph[30] / searchVisibilityGraph[30]
  Ext*Advertising — флаги внешней рекламы (Ozon-продвижение / Яндекс / Google)

getWarehouses — разбивка остатка по складам FBO/FBS.

CLI:
  mpstats.py whoami                         # аккаунт + остаток квоты плагина
  mpstats.py ozon <sku> <sku> ...           # метрики по SKU (таблица) [--json] [--fbs]
  mpstats.py warehouses <sku>               # остаток по складам
  mpstats.py pull [--store] [--limit N]     # обход всех отслеживаемых конкурентов

Как модуль:
  from mpstats import fetch_ozon, parse_item, user_info
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from curl_cffi import requests as _rq
except ImportError:
    sys.stderr.write("НУЖЕН curl_cffi: pip install curl_cffi\n")
    raise

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = "https://plugin.mpstats.io/pluginapi"
PVER = "4.254"                         # версия плагина; сервер её пишет в лимиты
# id расширения в Chrome Web Store. Реальный плагин шлёт запрос из service-worker'а,
# и браузер проставляет Origin: chrome-extension://<id>. Мимикрируем под него, чтобы
# сервер не придрался к Origin при headless-вызове (сама авторизация — по куке).
EXT_ID = "pjbepnginjokklnhdgladnmlghcchbeb"
TARGETS_JSON = ROOT / "state" / "dji_competitors.json"
SNAPSHOT = ROOT / "state" / "competitors_sales_pulse.md"   # читает дайджест (coo-pulse.md)


class AuthError(RuntimeError):
    """Кука протухла / не задана — сервер вернул 403 Unauthorized."""


# ---------- кука ----------

def _cookie() -> str:
    """MPSTATS_COOKIE из окружения или из .env (целиком строка заголовка Cookie)."""
    val = os.environ.get("MPSTATS_COOKIE")
    if not val:
        envf = ROOT / ".env"
        if envf.exists():
            for line in envf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("MPSTATS_COOKIE=") and "=" in line:
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not val:
        raise AuthError(
            "MPSTATS_COOKIE не задан. Достать: залогинься на mpstats.io → DevTools ▸ Network ▸ "
            "первый запрос 'mpstats.io' (document) ▸ Request Headers ▸ Cookie ▸ хватит куки "
            "mp_auth=... ▸ в .env:\n  MPSTATS_COOKIE='mp_auth=<jwt>'")
    return val


def cookie_expiry() -> tuple[int | None, float | None]:
    """(exp_unix, дней_осталось) из JWT mp_auth. Кука не продлевается сервером ⇒
    следим за сроком, чтобы дайджест не ослеп молча. (None, None) если не распарсить."""
    import base64
    import json as _json
    import re
    import time
    try:
        m = re.search(r"mp_auth=([^;]+)", _cookie())
        if not m:
            return None, None
        payload = m.group(1).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = _json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if not exp:
            return None, None
        return int(exp), (int(exp) - time.time()) / 86400.0
    except Exception:
        return None, None


# ---------- сеть ----------

def _post(payload: dict, timeout: int = 40) -> dict:
    """Один вызов pluginapi. Бросает AuthError на 403, RuntimeError на прочее."""
    body = {**payload, "pver": PVER}
    r = _rq.post(ENDPOINT, json=body, timeout=timeout, impersonate="chrome131",
                 headers={"Content-Type": "application/json", "Cookie": _cookie(),
                          "Origin": f"chrome-extension://{EXT_ID}"})
    if r.status_code != 200:
        raise RuntimeError(f"pluginapi HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if isinstance(data, dict) and data.get("code") == 403:
        raise AuthError("pluginapi: 403 Unauthorized — кука протухла, обнови MPSTATS_COOKIE")
    return data


def user_info() -> dict:
    """Аккаунт + квота плагина. Вызовы userInfo квоту НЕ расходуют."""
    d = _post({"Request": "userInfo"})
    res = (d.get("0") or {}).get("result", {})
    plugin = res.get("plugin", {}) or {}
    user = res.get("user", {}) or {}
    return {
        "email": user.get("email"),
        "tariff": user.get("tariff"),
        "expires": user.get("expires"),
        "quota_available": int(plugin.get("available") or 0),
        "quota_used": int(plugin.get("use") or 0),
    }


def fetch_ozon(skus: list[int | str], oz_fbs: bool = False, chunk: int = 400) -> dict[str, dict]:
    """Батч-запрос метрик по списку Ozon SKU. Возвращает {sku: сырой_item}.

    Один вызов = 1 единица квоты независимо от числа SKU. chunk — предохранитель
    на очень длинных списках (сервер держал 205 разом; режем по 400 с запасом).
    """
    ints = [int(s) for s in skus if str(s).isdigit()]
    out: dict[str, dict] = {}
    for i in range(0, len(ints), chunk):
        part = ints[i:i + chunk]
        d = _post({"Place": "ozon", "Sku": part, "ozFBS": bool(oz_fbs)})
        out.update(d.get("items", {}) or {})
    return out


def warehouses(skus: list[int | str]) -> dict[str, dict]:
    """getWarehouses — остаток по складам FBO/FBS. {sku: {stocks:{fbs,fbo[]}, last_update}}."""
    ints = [int(s) for s in skus]
    d = _post({"Request": "getWarehouses", "Place": "ozon", "Sku": ints})
    return d.get("data", {}) or {}


# ---------- разбор ----------

def _avg_nonzero(arr: list) -> int | None:
    vals = [x for x in (arr or []) if isinstance(x, (int, float)) and x > 0]
    return round(sum(vals) / len(vals)) if vals else None


def _last_nonzero(arr: list) -> int | None:
    for x in reversed(arr or []):
        if isinstance(x, (int, float)) and x > 0:
            return int(x)
    return None


def parse_item(raw: dict, model: str | None = None, config: str | None = None) -> dict:
    """Сырой item MPStats → плоский словарь под competitor_sales. Устойчив к пропускам.

    ВАЖНО про графики: pricesGraph/ordersGraph/... сервер отдаёт ТОЛЬКО на одиночный
    SKU. В батч-ответе (multi-SKU) их нет — только скаляры. Поэтому реальную цену/шт
    считаем из скаляров: price_est = revenue_30d / orders_30d (есть всегда при продажах).
    """
    totals = raw.get("Totals", {}) or {}
    prices = raw.get("pricesGraph") or []
    orders_30 = totals.get("orders")
    revenue_30 = totals.get("sum")
    price_est = round(revenue_30 / orders_30) if orders_30 and revenue_30 else None
    return {
        "sku": str(raw.get("Sku") or ""),
        "model": model,
        "config": config,
        "seller": raw.get("Seller"),
        "seller_id": raw.get("SellerId"),
        "brand": raw.get("Brand"),
        "stock": raw.get("Count"),
        "days_on_stocks": raw.get("DaysOnStocks"),
        "orders_per_day": raw.get("OrdersPerDay"),
        "orders_30d": orders_30,
        "revenue_30d": revenue_30,
        "revenue_prev": totals.get("sumPrev"),
        "price_est": price_est,                 # revenue/orders — ср. цена продажи/шт
        "price_last": _last_nonzero(prices),    # из графика (только single-SKU)
        "price_avg": _avg_nonzero(prices),      # из графика (только single-SKU)
        "ext_advertising": bool(raw.get("ExtAdvertising")),
        "ext_yandex": bool(raw.get("ExtYandexAdvertising")),
        "ext_google": bool(raw.get("ExtGoogleAdvertising")),
        "graphs": {
            "orders": raw.get("ordersGraph") or [],
            "count": raw.get("countGraph") or [],
            "prices": prices,
            "visibility": raw.get("searchVisibilityGraph") or [],
        },
    }


# ---------- цели (наши трекаемые конкуренты) ----------

def load_targets() -> list[dict]:
    """SKU конкурентов из state/dji_competitors.json → [{sku, model, config, name}].

    Тянем из matched[model][config][], market_only[...] и flagged[]. Это самая полная
    выверенная опись конкурентов (см. project-competitor-monitor memory).
    """
    if not TARGETS_JSON.exists():
        return []
    d = json.loads(TARGETS_JSON.read_text(encoding="utf-8"))
    seen: dict[str, dict] = {}

    def add(sku, model, config, name):
        sku = str(sku)
        if sku.isdigit() and sku not in seen:   # отсекаем плейсхолдеры вроде '?'
            seen[sku] = {"sku": sku, "model": model, "config": config, "name": name}

    for section in ("matched", "market_only"):
        for model, cfgs in (d.get(section) or {}).items():
            if not isinstance(cfgs, dict):
                continue
            for config, lst in cfgs.items():
                if isinstance(lst, list):
                    for it in lst:
                        if isinstance(it, dict) and it.get("sku"):
                            add(it["sku"], model, config, it.get("name"))
    for it in d.get("flagged", []) or []:
        if isinstance(it, dict) and it.get("sku"):
            add(it["sku"], it.get("model"), it.get("config", "?"), it.get("name"))
    return list(seen.values())


# ---------- CLI ----------

def _fmt_money(n) -> str:
    return f"{int(n):,}".replace(",", " ") if n else "—"


def _cmd_whoami() -> None:
    u = user_info()
    left = u["quota_available"] - u["quota_used"]
    print(f"аккаунт : {u['email']}  (тариф: {u['tariff'] or '—'}, {u['expires'] or '—'})")
    print(f"квота   : использовано {u['quota_used']} из {u['quota_available']}  "
          f"→ осталось {left}")
    import time
    exp, days = cookie_expiry()
    if exp:
        mark = "  ⚠️ ОБНОВИ КУКУ" if days is not None and days < 2 else ""
        print(f"кука    : mp_auth до {time.strftime('%d.%m %H:%M', time.localtime(exp))} "
              f"(~{days:.1f} дн.){mark}")


def _cmd_ozon(args) -> None:
    raw = fetch_ozon(args.skus, oz_fbs=args.fbs)
    if args.json:
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        return
    items = [parse_item(raw[s]) for s in raw]
    items.sort(key=lambda x: x["revenue_30d"] or 0, reverse=True)
    print(f"# {len(items)} SKU (30 дней; ✎ = внешняя реклама; цена/шт = выручка/заказы)")
    print(f"{'sku':>11}  {'ост':>4} {'зак':>4} {'выручка30д':>11} {'тренд':>6}  "
          f"{'цена/шт':>9}  продавец")
    for it in items:
        trend = ""
        if it["revenue_prev"]:
            dp = (it["revenue_30d"] or 0) - it["revenue_prev"]
            trend = f"{'+' if dp >= 0 else ''}{round(100*dp/it['revenue_prev'])}%"
        ad = "✎" if (it["ext_advertising"] or it["ext_yandex"] or it["ext_google"]) else " "
        print(f"{it['sku']:>11} {ad} {it['stock'] or 0:>4} {it['orders_30d'] or 0:>4} "
              f"{_fmt_money(it['revenue_30d']):>11} {trend:>6}  "
              f"{_fmt_money(it['price_est']):>9}  {(it['seller'] or '')[:24]}")


def _cmd_warehouses(args) -> None:
    data = warehouses(args.skus)
    for sku, w in data.items():
        st = w.get("stocks", {})
        print(f"{sku}  обновлено {w.get('last_update')}")
        print(f"  FBS: {st.get('fbs', 0)}")
        for wh in st.get("fbo", []) or []:
            print(f"  FBO {wh.get('name'):<24} {wh.get('count')}")


def write_snapshot(rows: list[dict], got: int, total: int) -> None:
    """Снапшот продаж конкурентов для дайджеста (state/competitors_sales_pulse.md).

    Только продающие (orders>0), топ по выручке 30д. Плоский текст — читает COO-пульс.
    Без времени в коде (Date недоступен в песочнице агента) → метку ставит вызывающий.
    """
    import time
    sellers = sorted({r["seller"] for r in rows if (r["orders_30d"] or 0) > 0 and r["seller"]})
    selling = [r for r in rows if (r["orders_30d"] or 0) > 0]
    selling.sort(key=lambda x: x["revenue_30d"] or 0, reverse=True)
    total_rev = sum(r["revenue_30d"] or 0 for r in selling)
    exp, days = cookie_expiry()
    cookie_line = ""
    if exp:
        warn = "  ⚠️ ПОРА ОБНОВИТЬ (пингни владельца)" if days is not None and days < 2 else ""
        cookie_line = (f"Кука MPStats (mp_auth) истекает {time.strftime('%d.%m', time.localtime(exp))} "
                       f"— ~{days:.0f} дн.{warn}")
    lines = [
        f"# Пульс продаж конкурентов (MPStats) — {time.strftime('%d.%m %H:%M')}",
        f"Источник: `tools/mpstats.py pull` (батч plugin.mpstats.io, 1 квота/прогон).",
        *( [cookie_line] if cookie_line else [] ),
        f"Охват: {got}/{total} карточек; продают за 30д: {len(selling)} у {len(sellers)} продавцов; "
        f"суммарная выручка конкурентов за 30д: {_fmt_money(total_rev)} ₽.",
        "",
        "## Продают за 30д (топ по выручке; ✎ = внешняя реклама)",
        "```",
        f"{'выручка30д':>11} {'зак':>4} {'цена/шт':>9} {'ост':>4}  {'модель/компл':<18} продавец",
    ]
    for r in selling[:30]:
        trend = ""
        if r["revenue_prev"]:
            dp = (r["revenue_30d"] or 0) - r["revenue_prev"]
            trend = f" {'+' if dp >= 0 else ''}{round(100*dp/r['revenue_prev'])}%"
        ad = "✎" if (r["ext_advertising"] or r["ext_yandex"] or r["ext_google"]) else " "
        mc = f"{r['model'] or '?'}/{r['config'] or '?'}"
        lines.append(f"{_fmt_money(r['revenue_30d']):>11} {r['orders_30d'] or 0:>4} "
                     f"{_fmt_money(r['price_est']):>9} {r['stock'] or 0:>4} {ad} {mc:<18} "
                     f"{(r['seller'] or '')[:22]}{trend}")
    lines += ["```", ""]
    SNAPSHOT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cmd_pull(args) -> None:
    import competitors as C  # локальный слой состояния (та же папка tools/)
    targets = load_targets()
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        print("нет целей — пуст state/dji_competitors.json"); return
    by_sku = {t["sku"]: t for t in targets}
    print(f"обход {len(targets)} конкурентов…", file=sys.stderr)
    raw = fetch_ozon([t["sku"] for t in targets], oz_fbs=args.fbs)

    conn = C.db() if args.store else None
    rows = []
    for sku, item in raw.items():
        t = by_sku.get(sku, {})
        parsed = parse_item(item, model=t.get("model"), config=t.get("config"))
        rows.append(parsed)
        if conn is not None:
            C.record_sales(conn, parsed)
    if conn is not None:
        conn.commit(); conn.close()

    rows.sort(key=lambda x: x["revenue_30d"] or 0, reverse=True)
    got, missing = len(raw), len(targets) - len(raw)
    with_sales = sum(1 for r in rows if (r["orders_30d"] or 0) > 0)
    write_snapshot(rows, got, len(targets))
    print(f"# получено {got}/{len(targets)} (нет данных: {missing}); "
          f"с продажами за 30д: {with_sales}; снапшот → {SNAPSHOT.name}"
          + ("  [записано в competitor_sales]" if args.store else "  [--dry, не записано]"))
    print(f"{'sku':>11}  {'модель/компл':<18} {'ост':>4} {'зак':>4} {'выручка30д':>11} "
          f"{'цена/шт':>9}  продавец")
    for r in rows[:args.top]:
        mc = f"{r['model'] or '?'}/{r['config'] or '?'}"
        print(f"{r['sku']:>11}  {mc:<18} {r['stock'] or 0:>4} {r['orders_30d'] or 0:>4} "
              f"{_fmt_money(r['revenue_30d']):>11} {_fmt_money(r['price_est']):>9}  "
              f"{(r['seller'] or '')[:22]}")


def _main() -> None:
    ap = argparse.ArgumentParser(description="MPStats plugin API client (продажи/остатки конкурентов)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="аккаунт + остаток квоты плагина")

    po = sub.add_parser("ozon", help="метрики по SKU")
    po.add_argument("skus", nargs="+")
    po.add_argument("--fbs", action="store_true", help="учитывать FBS-остаток")
    po.add_argument("--json", action="store_true", help="сырой JSON")

    pw = sub.add_parser("warehouses", help="остаток по складам FBO/FBS")
    pw.add_argument("skus", nargs="+")

    pp = sub.add_parser("pull", help="обход всех конкурентов из dji_competitors.json")
    pp.add_argument("--store", action="store_true", help="записать в competitor_sales (иначе только показать)")
    pp.add_argument("--fbs", action="store_true")
    pp.add_argument("--limit", type=int, default=0, help="ограничить число целей (тест)")
    pp.add_argument("--top", type=int, default=40, help="сколько строк показать")

    args = ap.parse_args()
    try:
        if args.cmd == "whoami":
            _cmd_whoami()
        elif args.cmd == "ozon":
            _cmd_ozon(args)
        elif args.cmd == "warehouses":
            _cmd_warehouses(args)
        elif args.cmd == "pull":
            _cmd_pull(args)
    except AuthError as e:
        sys.stderr.write(f"\n[auth] {e}\n")
        sys.exit(2)


if __name__ == "__main__":
    _main()
