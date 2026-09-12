#!/usr/bin/env python3
"""Generic Ozon Seller API caller: ozon.py /path '{"json":1}' [--dry]
Мутации (не в READONLY-префиксах) логируются в data/store.sqlite:action_log."""
import json, sqlite3, sys, time, urllib.request, urllib.error
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
E = dict(l.strip().split("=",1) for l in (ROOT/".env").read_text().splitlines()
         if "=" in l and not l.strip().startswith("#"))
READONLY = ("/list","/get","/info","/history","/tree","/attributes","/values",
            "/report","/realization","/totals","/summary","/rating","/analytics","/description")
def is_mut(p): return p not in ("/v1/actions",) and not any(s in p for s in READONLY)
def log(kind, detail):
    c = sqlite3.connect(ROOT/"data"/"store.sqlite", timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS action_log(ts INTEGER, source TEXT, kind TEXT, detail TEXT)")
    c.execute("INSERT INTO action_log VALUES(?,?,?,?)",(int(time.time()),"ozon.py",kind,detail[:2000]))
    c.commit(); c.close()
def main():
    args=[a for a in sys.argv[1:] if a!="--dry"]; dry="--dry" in sys.argv
    path=args[0]; body=json.loads(args[1]) if len(args)>1 else {}
    mut=is_mut(path)
    if dry:
        print(json.dumps({"DRY_RUN":True,"path":path,"mutating":mut,"body":body},ensure_ascii=False)); return
    if mut: log("call", f"POST {path} {json.dumps(body,ensure_ascii=False)[:500]}")
    req=urllib.request.Request("https://api-seller.ozon.ru"+path, data=json.dumps(body).encode(),
        headers={"Client-Id":E["OZON_CLIENT_ID"],"Api-Key":E["OZON_API_KEY"],"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req,timeout=90) as r: out=r.read().decode()
        if mut: log("result", f"{path} OK {out[:300]}")
        print(out)
    except urllib.error.HTTPError as e:
        err=e.read().decode()[:500]
        if mut: log("result", f"{path} HTTP{e.code} {err}")
        print(json.dumps({"http_error":e.code,"body":err})); sys.exit(1)
main()
