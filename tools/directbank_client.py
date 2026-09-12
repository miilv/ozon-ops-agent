#!/usr/bin/env python3
"""DirectBank-клиент (чтение выписок). Только stdlib.

Транспорт: HTTPS + XML по стандарту 1С:DirectBank
  https://github.com/1C-Company/DirectBank

Все секреты и URL — из .env, в чат/git не попадают:
  DIRECTBANK_URL       базовый URL шлюза банка (из файла настроек обмена), напр. https://bank.example/directbank/
  DIRECTBANK_LOGIN     логин банк-клиента
  DIRECTBANK_PASSWORD  пароль
  DIRECTBANK_CUSTOMER  CustomerID (обычно = логин; если банк выдал иной — сюда)
  DIRECTBANK_APIVER    версия API стандарта, по умолчанию 2.3

Использование:
  directbank_client.py logon                 — проверить вход, получить SID
  directbank_client.py list [YYYY-MM-DD]     — список входящих пакетов с даты (по умолч. -30 дней)
  directbank_client.py pull [YYYY-MM-DD]     — забрать все пакеты, сохранить в data/directbank/, распечатать выписки

Замечание: реализации DirectBank у банков отличаются (многие поверх iBank2).
Скрипт печатает статус/тело каждого шага, чтобы отладить на живом шлюзе.
"""
import base64, sys, ssl, urllib.request, urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
E = dict(l.strip().split("=", 1) for l in (ROOT / ".env").read_text().splitlines()
         if "=" in l and not l.strip().startswith("#"))

BASE = E.get("DIRECTBANK_URL", "").rstrip("/")
LOGIN = E.get("DIRECTBANK_LOGIN", "")
PASSWORD = E.get("DIRECTBANK_PASSWORD", "")
CUSTOMER = E.get("DIRECTBANK_CUSTOMER") or LOGIN
APIVER = E.get("DIRECTBANK_APIVER", "2.3")
OUTDIR = ROOT / "data" / "directbank"

if not BASE:
    sys.exit("DIRECTBANK_URL не задан в .env — нужен адрес шлюза банка (из файла настроек обмена).")

_CTX = ssl.create_default_context()  # TLS; для ГОСТ-only шлюза понадобится отдельная обвязка


def _req(method, path, *, sid=None, body=None, query=""):
    url = f"{BASE}/{path}{query}"
    headers = {
        "CustomerID": CUSTOMER,
        "APIVersion": APIVER,
        "AvailableAPIVersion": APIVER,
        "Content-Type": "application/xml; charset=utf-8",
    }
    if sid:
        headers["SID"] = sid
    else:
        token = base64.b64encode(f"{LOGIN}:{PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    r = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, context=_CTX, timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as ex:
        return ex.code, dict(ex.headers), ex.read()


def _find_sid(headers, raw):
    if headers.get("SID"):
        return headers["SID"]
    try:
        for el in ET.fromstring(raw).iter():
            if el.tag.lower().endswith("sid") and el.text:
                return el.text.strip()
    except ET.ParseError:
        pass
    return None


def logon():
    status, headers, raw = _req("POST", "Logon")
    print(f"POST /Logon → {status}")
    sid = _find_sid(headers, raw)
    if sid:
        print(f"SID: {sid}")
    else:
        print("SID не найден. Ответ шлюза:")
        print(raw.decode("utf-8", "replace")[:2000])
    return sid


def _default_since(argv):
    if argv:
        return datetime.strptime(argv[0], "%Y-%m-%d")
    return datetime.now() - timedelta(days=30)


def list_packs(argv):
    sid = logon()
    if not sid:
        sys.exit("Нет SID — вход не удался.")
    since = _default_since(argv).strftime("%Y-%m-%dT%H:%M:%S")
    status, _, raw = _req("GET", "GetPackList", sid=sid, query=f"?date={since}")
    print(f"GET /GetPackList?date={since} → {status}")
    print(raw.decode("utf-8", "replace")[:4000])
    return sid, raw


def pull(argv):
    sid, raw = list_packs(argv)
    ids = []
    try:
        for el in ET.fromstring(raw).iter():
            if el.tag.lower().endswith("id") and el.text and el.text.strip():
                ids.append(el.text.strip())
    except ET.ParseError:
        print("Не разобрал список пакетов как XML — проверь формат ответа выше.")
        return
    if not ids:
        print("\nВходящих пакетов нет. Возможно, банк отдаёт выписку только по запросу —")
        print("тогда нужен документ 'запрос выписки' (может требовать ЭП). Обсудим по факту.")
        return
    OUTDIR.mkdir(parents=True, exist_ok=True)
    print(f"\nПакетов к загрузке: {len(ids)}")
    for pid in ids:
        status, _, body = _req("GET", "GetPack", sid=sid, query=f"?id={pid}")
        dest = OUTDIR / f"{pid}.xml"
        dest.write_bytes(body)
        print(f"  GetPack {pid} → {status}, сохранён {dest.relative_to(ROOT)} ({len(body)} б)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "logon"
    rest = sys.argv[2:]
    if cmd == "logon":
        logon()
    elif cmd == "list":
        list_packs(rest)
    elif cmd == "pull":
        pull(rest)
    else:
        sys.exit(__doc__)
