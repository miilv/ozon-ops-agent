#!/usr/bin/env python3
"""tg.py send "текст" [--dm ID] | ask "вопрос" [--options a,b] [--timeout сек] | history [N]"""
import json, sys, urllib.request
def call(method, path, body=None, q=""):
    url=f"http://127.0.0.1:8765{path}{q}"
    req=urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
        headers={"Content-Type":"application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=float(body.get("timeout",3600))+10 if body and "timeout" in body else 60) as r:
        return json.loads(r.read())
a=sys.argv[1:]
if a[0]=="send":
    body={"text":a[1]}
    if "--dm" in a: body["chat"]=int(a[a.index("--dm")+1])
    print(call("POST","/send",body))
elif a[0]=="ask":
    body={"question":a[1]}
    if "--options" in a: body["options"]=a[a.index("--options")+1].split(",")
    if "--timeout" in a: body["timeout"]=int(a[a.index("--timeout")+1])
    print(json.dumps(call("POST","/ask",body),ensure_ascii=False))
elif a[0]=="history":
    n=a[1] if len(a)>1 else "50"
    for m in call("GET","/history",q=f"?limit={n}"):
        print(f"[{m['from']}] {m['text'] or ''} {('('+m['media']+')') if m['media'] else ''}")
