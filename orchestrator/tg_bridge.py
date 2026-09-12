#!/usr/bin/env python3
"""tg-bridge: единственный владелец бот-токена (один polling на токен).

- Логирует ВСЕ сообщения командного чата и личек в SQLite (chat_log).
- Личка (whitelist) и тэг/reply в чате -> сессия claude -p; reply -> --resume.
- Локальный HTTP API 127.0.0.1:8765: /send /ask /history (для скиллов и кроном).
- Кнопки (callback_query) резолвят pending_asks.
- Фото/голосовые скачивает в data/chat_media/, кладёт путь в лог.
Stdlib-only. Запускается systemd-юнитом tg-bridge.
"""
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "store.sqlite"
MEDIA = ROOT / "data" / "chat_media"
ENV = dict(
    l.strip().split("=", 1)
    for l in (ROOT / ".env").read_text().splitlines()
    if "=" in l and not l.strip().startswith("#")
)
TOKEN = ENV["TG_BOT_TOKEN"]
TEAM_CHAT = int(ENV["TG_TEAM_CHAT"])
API = f"https://api.telegram.org/bot{TOKEN}"
BOT_USERNAME = "your_ops_bot"
TEAM = {100000001: ("Владелец", "владелец; апрувит T3"),
        100000002: ("Оператор", "операционка; апрувит T3 по ценам/акциям")}
ALLOWED_DM = set(TEAM)
CLAUDE = "/usr/bin/claude" if os.path.exists("/usr/bin/claude") else "claude"
PURCHASE_RE = re.compile(r"(куп|взял|затар|забрал)\w*\D{0,40}?(\d[\d\s]{3,9})\s*(?:р|₽|k|к|тыс)?", re.I)


def tg(method: str, **kw):
    req = urllib.request.Request(f"{API}/{method}", data=json.dumps(kw).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=65) as r:
        return json.loads(r.read())



def md_to_html(text: str) -> str:
    """Markdown агента -> Telegram HTML (безопасно, с плейсхолдерами для кода)."""
    stash = []
    def keep(html):
        stash.append(html); return f"\x00{len(stash)-1}\x00"
    # блоки кода до экранирования
    text = re.sub(r"```[a-zA-Z0-9]*\n(.*?)```",
                  lambda m: keep("<pre>" + m.group(1).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;") + "</pre>"),
                  text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`",
                  lambda m: keep("<code>" + m.group(1).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;") + "</code>"),
                  text)
    text = text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.M)
    for i, html in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", html)
    return text


def send_text(chat_id: int, text: str, reply_to=None, reply_markup=None):
    """Отправка: rich markdown (нативный Bot API) → HTML-конвертер → plain."""
    rich = dict(chat_id=chat_id, rich_message={"markdown": text[:4096]})
    if reply_to: rich["reply_to_message_id"] = reply_to
    if reply_markup: rich["reply_markup"] = reply_markup
    try:
        return tg("sendRichMessage", **rich)
    except Exception:
        pass
    kw = dict(chat_id=chat_id, text=md_to_html(text)[:4096], parse_mode="HTML",
              disable_web_page_preview=True)
    if reply_to: kw["reply_to_message_id"] = reply_to
    if reply_markup: kw["reply_markup"] = reply_markup
    try:
        return tg("sendMessage", **kw)
    except Exception:
        kw.pop("parse_mode", None); kw["text"] = text[:4096]
        return tg("sendMessage", **kw)


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS chat_log(
      msg_id INTEGER, chat_id INTEGER, user_id INTEGER, user_name TEXT,
      ts INTEGER, text TEXT, media_path TEXT, reply_to INTEGER, flags TEXT,
      PRIMARY KEY(chat_id, msg_id));
    CREATE TABLE IF NOT EXISTS sessions(
      chat_id INTEGER, root_msg INTEGER, session_id TEXT, updated INTEGER,
      PRIMARY KEY(chat_id, root_msg));
    CREATE TABLE IF NOT EXISTS bot_msgs(
      chat_id INTEGER, msg_id INTEGER, root_msg INTEGER,
      PRIMARY KEY(chat_id, msg_id));
    CREATE TABLE IF NOT EXISTS pending_asks(
      id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, msg_id INTEGER,
      question TEXT, options TEXT, answer TEXT, answered_by TEXT,
      created INTEGER, timeout_sec INTEGER);
    CREATE TABLE IF NOT EXISTS action_log(
      ts INTEGER, source TEXT, kind TEXT, detail TEXT);
    """)
    return c


def log_action(kind: str, detail: str, source: str = "bridge"):
    c = db()
    c.execute("INSERT INTO action_log VALUES(?,?,?,?)", (int(time.time()), source, kind, detail))
    c.commit(); c.close()


def download_media(file_id: str, suffix: str) -> str:
    MEDIA.mkdir(parents=True, exist_ok=True)
    info = tg("getFile", file_id=file_id)
    fp = info["result"]["file_path"]
    dst = MEDIA / f"{int(time.time())}_{file_id[-8:]}{suffix}"
    urllib.request.urlretrieve(f"https://api.telegram.org/file/bot{TOKEN}/{fp}", dst)
    return str(dst.relative_to(ROOT))


# Долгие сессии не режем: глубокое исследование легко идёт час+.
# Единственный предохранитель — HARD_CAP от зависшего процесса (6 ч, не рабочий лимит).
HEARTBEAT_FIRST = 900     # первый «жив, работаю» через 15 мин
HEARTBEAT_EVERY = 1800    # дальше раз в 30 мин
HEARTBEAT_MAX = 4         # не больше 4 пингов на сессию
HARD_CAP = 6 * 3600


def spawn_session(chat_id: int, root_msg: int, prompt: str, resume: str | None):
    """Запуск claude -p в фоне; ответ шлём в чат reply'ем на root_msg."""
    def run():
        cmd = [CLAUDE, "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"]
        if resume:
            cmd += ["--resume", resume]
        try:
            # stdout/stderr в файлы, а не в PIPE: длинный ответ переполнит буфер трубы и повесит процесс
            with tempfile.TemporaryFile("w+") as fout, tempfile.TemporaryFile("w+") as ferr:
                proc = subprocess.Popen(cmd, cwd=ROOT, stdout=fout, stderr=ferr,
                                        text=True, stdin=subprocess.DEVNULL)
                start, pings = time.time(), 0
                while proc.poll() is None:
                    time.sleep(5)
                    elapsed = time.time() - start
                    if elapsed > HARD_CAP:
                        proc.kill()
                        break
                    if pings < HEARTBEAT_MAX and elapsed >= HEARTBEAT_FIRST + pings * HEARTBEAT_EVERY:
                        pings += 1
                        try:
                            send_text(chat_id, f"⏳ Работаю, {int(elapsed // 60)} мин — сессия жива.",
                                      reply_to=root_msg)
                        except Exception:
                            pass
                proc.wait()
                fout.seek(0); ferr.seek(0)
                stdout, stderr = fout.read(), ferr.read()
            data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
            answer = (data.get("result") or stderr or "…").strip()[:3900]
            sid = data.get("session_id")
        except Exception as exc:
            answer, sid = f"⚠️ сессия упала: {type(exc).__name__}: {exc}"[:500], resume
        r = send_text(chat_id, answer, reply_to=root_msg)
        bot_mid = r["result"]["message_id"]
        c = db()
        if sid:
            c.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)",
                      (chat_id, root_msg, sid, int(time.time())))
        c.execute("INSERT OR REPLACE INTO bot_msgs VALUES(?,?,?)", (chat_id, bot_mid, root_msg))
        c.commit(); c.close()
    threading.Thread(target=run, daemon=True).start()


def build_prompt(chat_id: int, user_id: int, text: str, is_dm: bool) -> str:
    name, role = TEAM.get(user_id, (f"id{user_id}", "не в команде"))
    c = db()
    tail = c.execute(
        "SELECT user_name, text FROM chat_log WHERE chat_id=? ORDER BY ts DESC LIMIT 30",
        (TEAM_CHAT,)).fetchall()[::-1]
    c.close()
    chat_tail = "\n".join(f"{u}: {t}" for u, t in tail if t)
    where = "личных сообщениях (можно подробно)" if is_dm else \
            "командном чате — отвечай ОДНИМ сообщением с результатом, без шагов"
    return (f"Тебе пишет {name} ({role}) в {where}.\n"
            f"Последние сообщения командного чата для контекста:\n{chat_tail}\n"
            f"---\nСообщение: {text}\n"
            f"Ответ верни просто текстом — bridge сам отправит его в Telegram.")


def handle_message(m: dict):
    chat_id = m["chat"]["id"]
    user = m.get("from", {})
    uid = user.get("id", 0)
    text = m.get("text") or m.get("caption") or ""
    media_path, flags = None, []
    if "photo" in m:
        media_path = download_media(m["photo"][-1]["file_id"], ".jpg"); flags.append("photo")
    if "voice" in m:
        media_path = download_media(m["voice"]["file_id"], ".oga"); flags.append("voice")
    if PURCHASE_RE.search(text or ""):
        flags.append("purchase?")
    c = db()
    c.execute("INSERT OR REPLACE INTO chat_log VALUES(?,?,?,?,?,?,?,?,?)",
              (m["message_id"], chat_id, uid, user.get("first_name", "?"),
               m.get("date", int(time.time())), text, media_path,
               (m.get("reply_to_message") or {}).get("message_id"), ",".join(flags)))
    c.commit()

    is_dm = m["chat"]["type"] == "private"
    if is_dm and uid not in ALLOWED_DM:
        c.close(); return
    mentioned = f"@{BOT_USERNAME}" in text
    reply_to = (m.get("reply_to_message") or {})
    reply_to_bot = (reply_to.get("from") or {}).get("username") == BOT_USERNAME

    resume, root = None, m["message_id"]
    if reply_to_bot:
        row = c.execute("SELECT root_msg FROM bot_msgs WHERE chat_id=? AND msg_id=?",
                        (chat_id, reply_to["message_id"])).fetchone()
        if row:
            root = row[0]
            srow = c.execute("SELECT session_id FROM sessions WHERE chat_id=? AND root_msg=?",
                             (chat_id, root)).fetchone()
            resume = srow[0] if srow else None
    elif is_dm and not mentioned:
        # личка: продолжаем последнюю сессию, если ей < 2 часов
        srow = c.execute("SELECT root_msg, session_id FROM sessions WHERE chat_id=? "
                         "AND updated > ? ORDER BY updated DESC LIMIT 1",
                         (chat_id, int(time.time()) - 7200)).fetchone()
        if srow:
            root, resume = srow
    c.close()

    if is_dm or mentioned or reply_to_bot:
        try:  # 👍 = «видел, работаю»
            tg("setMessageReaction", chat_id=chat_id, message_id=m["message_id"],
               reaction=[{"type": "emoji", "emoji": "👍"}])
        except Exception:
            pass
        prompt = build_prompt(chat_id, uid, text.replace(f"@{BOT_USERNAME}", "").strip(), is_dm)
        if media_path:
            prompt += f"\n(Приложен файл: {media_path} — если это фото, посмотри его Read'ом.)"
        spawn_session(chat_id, root, prompt, resume)


def handle_callback(cb: dict):
    data = cb.get("data", "")
    uid = cb["from"]["id"]
    name = TEAM.get(uid, (f"id{uid}",))[0]
    if data.startswith("ask:"):
        _, ask_id, opt = data.split(":", 2)
        if uid not in TEAM:
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="нет прав"); return
        c = db()
        c.execute("UPDATE pending_asks SET answer=?, answered_by=? WHERE id=? AND answer IS NULL",
                  (opt, name, int(ask_id)))
        c.commit(); c.close()
        tg("answerCallbackQuery", callback_query_id=cb["id"], text=f"принято: {opt}")
        msg = cb.get("message", {})
        tg("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
           text=msg.get("text", "") + f"\n\n☑️ {name}: {opt}")
        log_action("ask_answered", f"#{ask_id} {name}: {opt}")


class ApiHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # тихо
        pass

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/history":
            chat = int(q.get("chat", [TEAM_CHAT])[0])
            limit = int(q.get("limit", ["50"])[0])
            c = db()
            rows = c.execute("SELECT ts,user_name,text,media_path,flags FROM chat_log "
                             "WHERE chat_id=? ORDER BY ts DESC LIMIT ?", (chat, limit)).fetchall()
            c.close()
            self._json(200, [{"ts": r[0], "from": r[1], "text": r[2],
                              "media": r[3], "flags": r[4]} for r in rows[::-1]])
        else:
            self._json(404, {"err": "unknown"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/send":
            r = send_text(body.get("chat", TEAM_CHAT), body["text"])
            self._json(200, {"ok": True, "msg_id": r["result"]["message_id"]})
        elif self.path == "/ask":
            opts = body.get("options", ["да", "нет"])
            timeout = int(body.get("timeout", 3600))
            c = db()
            cur = c.execute("INSERT INTO pending_asks(chat_id,question,options,created,timeout_sec) "
                            "VALUES(?,?,?,?,?)",
                            (body.get("chat", TEAM_CHAT), body["question"], json.dumps(opts),
                             int(time.time()), timeout))
            ask_id = cur.lastrowid
            c.commit()
            kb = {"inline_keyboard": [[{"text": o, "callback_data": f"ask:{ask_id}:{o}"} for o in opts]]}
            r = send_text(body.get("chat", TEAM_CHAT), f"❓ {body['question']}", reply_markup=kb)
            c.execute("UPDATE pending_asks SET msg_id=? WHERE id=?",
                      (r["result"]["message_id"], ask_id))
            c.commit(); c.close()
            deadline = time.time() + timeout
            while time.time() < deadline:
                c = db()
                row = c.execute("SELECT answer, answered_by FROM pending_asks WHERE id=?",
                                (ask_id,)).fetchone()
                c.close()
                if row and row[0]:
                    self._json(200, {"answer": row[0], "by": row[1]}); return
                time.sleep(2)
            self._json(200, {"answer": None, "by": None, "note": "timeout = отказ"})
        else:
            self._json(404, {"err": "unknown"})


def poll_loop():
    offset = 0
    while True:
        try:
            upd = tg("getUpdates", offset=offset, timeout=50,
                     allowed_updates=["message", "callback_query"])
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                try:
                    if "message" in u:
                        handle_message(u["message"])
                    elif "callback_query" in u:
                        handle_callback(u["callback_query"])
                except Exception as exc:
                    log_action("handler_error", f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            log_action("poll_error", f"{type(exc).__name__}: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=lambda: ThreadingHTTPServer(("127.0.0.1", 8765), ApiHandler).serve_forever(),
                     daemon=True).start()
    log_action("bridge_start", "polling begins")
    poll_loop()
