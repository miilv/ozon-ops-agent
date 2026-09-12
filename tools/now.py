#!/usr/bin/env python3
"""NOW.md без гонок: now.py add "<Секция>" "<строка>" | done "<подстрока>" | show"""
import fcntl, sys
from pathlib import Path
P = Path(__file__).resolve().parent.parent / "state" / "NOW.md"
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    with open(P, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        lines = f.read().splitlines()
        if cmd == "add":
            sec, item = sys.argv[2], sys.argv[3]
            out, done = [], False
            for l in lines:
                out.append(l)
                if not done and l.strip().lstrip("#").strip() == sec:
                    out.append(item); done = True
            if not done: out += [f"## {sec}", item]
            lines = out
        elif cmd == "done":
            sub = sys.argv[2]
            lines = [l for l in lines if sub not in l]
        f.seek(0); f.truncate(); f.write("\n".join(lines) + "\n")
    print("\n".join(lines) if cmd == "show" else "ok")
main()
