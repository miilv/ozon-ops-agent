#!/usr/bin/env python3
"""Отправка в TG: echo "текст" | tg_send.py  или  tg_send.py "текст" [--dm ID]
Через bridge (rich markdown); фолбэк — прямой API plain, если bridge лежит."""
import json, sys, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
e = dict(l.strip().split("=", 1) for l in (ROOT/".env").read_text().splitlines()
         if "=" in l and not l.strip().startswith("#"))
chat = e["TG_TEAM_CHAT"]
args = sys.argv[1:]
if "--dm" in args:
    i = args.index("--dm"); chat = args[i+1]; args = args[:i] + args[i+2:]
text = " ".join(args) if args else sys.stdin.read()
try:  # основной путь: bridge (rich)
    req = urllib.request.Request("http://127.0.0.1:8765/send",
        data=json.dumps({"chat": int(chat), "text": text[:4000]}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30)
    print("sent(rich)")
except Exception:  # фолбэк: напрямую, plain
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{e['TG_BOT_TOKEN']}/sendMessage",
        data=json.dumps({"chat_id": int(chat), "text": text[:4000]}).encode(),
        headers={"Content-Type": "application/json"}), timeout=30)
    print("sent(plain-fallback)")
