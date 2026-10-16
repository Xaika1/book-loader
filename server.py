#!/usr/bin/env python3
"""Учебники по Wi-Fi: сервер на компьютере, с которого планшеты сами скачивают книги.

Запуск: python3 server.py
  * админка (загрузка учебников) — http://127.0.0.1:8765, открывается только на этом компьютере;
  * планшеты с приложением «Учебники» находят компьютер в сети и скачивают всё сами.
Нужен только Python 3.8+.
"""
import hashlib
import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

BASE = Path(__file__).resolve().parent
LIBRARY = BASE / "library"
INDEX_FILE = LIBRARY / ".index.json"
STATIC = BASE / "static"
APK_FILE = BASE / "apk" / "uchebniki.apk"
DEVICES_FILE = BASE / "devices.json"
JOURNAL_FILE = BASE / "journal.log"
HELP_FILE = BASE / "ИНСТРУКЦИЯ.md"

PORT = int(os.environ.get("PORT", "8765"))
DISCOVERY_PORT = int(os.environ.get("DISCOVERY_PORT", "8766"))
DISCOVERY_REQUEST = b"UCHEBNIKI_DISCOVER"
MAX_DOWNLOADS = int(os.environ.get("MAX_DOWNLOADS", "10"))  # одновременных скачиваний
POLL_INTERVAL = 30   # как часто планшет проверяет обновления, секунд
ONLINE_TIMEOUT = 100  # сколько секунд без связи считаем планшет «на связи»

BOOK_EXT = {
    "pdf", "epub", "fb2", "djvu", "djv", "mobi", "azw", "azw3", "txt", "rtf",
    "doc", "docx", "odt", "chm", "cbz", "cbr", "html", "htm", "zip", "ppt", "pptx",
}

LIB_LOCK = threading.Lock()
DEV_LOCK = threading.Lock()
DOWNLOADS = threading.BoundedSemaphore(MAX_DOWNLOADS)
INDEX = {}       # path -> {"size", "mtime", "sha256"}
MANIFEST = {"version": "", "books": []}
DEVICES = {}     # id -> данные планшета


class ApiError(Exception):
    pass


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return ("%.0f %s" if unit in ("Б", "КБ") else "%.1f %s") % (n, unit)
        n /= 1024.0


def journal(text):
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
    with JOURNAL_FILE.open("a", encoding="utf-8") as f:
        f.write(line)


# ---------------------------------------------------------------- библиотека

def safe_rel(path):
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise ApiError("Недопустимое имя файла: %s" % path)
    return "/".join(parts)


def file_sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_book(p):
    return (p.is_file() and not any(part.startswith(".") for part in p.relative_to(LIBRARY).parts)
            and p.suffix.lower().lstrip(".") in BOOK_EXT)


def scan_library():
    """Пересобирает список учебников. Хэш считается только для новых/изменённых файлов,
    поэтому книги можно класть и прямо в папку library — они подхватятся сами."""
    global MANIFEST
    with LIB_LOCK:
        found = {}
        for p in LIBRARY.rglob("*"):
            if not is_book(p):
                continue
            rel = p.relative_to(LIBRARY).as_posix()
            st = p.stat()
            old = INDEX.get(rel)
            if old and old["size"] == st.st_size and old["mtime"] == st.st_mtime:
                found[rel] = old
            else:
                found[rel] = {"size": st.st_size, "mtime": st.st_mtime, "sha256": file_sha256(p)}
        if found == INDEX and MANIFEST["version"]:
            return
        INDEX.clear()
        INDEX.update(found)
        books = [{"path": k, "size": v["size"], "sha256": v["sha256"]} for k, v in sorted(INDEX.items())]
        version = hashlib.sha1(json.dumps(books, sort_keys=True).encode()).hexdigest()[:12]
        MANIFEST = {"version": version, "books": books}
        tmp = INDEX_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(INDEX, ensure_ascii=False), "utf-8")
        tmp.replace(INDEX_FILE)


def load_index():
    if INDEX_FILE.exists():
        try:
            INDEX.update(json.loads(INDEX_FILE.read_text("utf-8")))
        except (OSError, ValueError):
            pass
    scan_library()


def library_loop():
    while True:
        time.sleep(10)
        try:
            scan_library()
        except Exception as e:
            print("Ошибка проверки папки library:", e)


def remove_empty_dirs(root):
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------- планшеты

def load_devices():
    if DEVICES_FILE.exists():
        try:
            for dev_id, d in json.loads(DEVICES_FILE.read_text("utf-8")).items():
                DEVICES[dev_id] = d
        except (OSError, ValueError):
            print("devices.json повреждён — список планшетов начнётся заново")


def save_devices():
    keep = ("name", "model", "android", "first_seen", "last_seen", "ip")
    data = {k: {f: v.get(f) for f in keep} for k, v in DEVICES.items()}
    tmp = DEVICES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(DEVICES_FILE)


def next_device_name():
    used = {int(m.group(1)) for d in DEVICES.values()
            for m in [re.fullmatch(r"Планшет (\d+)", d.get("name") or "")] if m}
    n = 1
    while n in used:
        n += 1
    return "Планшет %d" % n


REPORT_FIELDS = {"model": str, "android": str, "app": str, "state": str, "error": str, "current": str,
                 "battery": int, "free": int, "done": int, "total": int, "have": int, "version": str}


def handle_report(body, ip):
    dev_id = str(body.get("id") or "")
    if not re.fullmatch(r"[\w-]{8,64}", dev_id):
        raise ApiError("Неверный id")
    with DEV_LOCK:
        dev = DEVICES.get(dev_id)
        is_new = dev is None
        if is_new:
            dev = DEVICES[dev_id] = {"name": next_device_name(), "first_seen": time.time()}
        for key, typ in REPORT_FIELDS.items():
            if key in body and body[key] is not None:
                try:
                    dev[key] = typ(body[key])
                except (TypeError, ValueError):
                    pass
        old_state = dev.get("_logged_state")
        dev.update(last_seen=time.time(), ip=ip)
        state = (dev.get("state"), dev.get("version"))
        if is_new or (dev.get("state") in ("ok", "error") and state != old_state):
            dev["_logged_state"] = state
            if is_new:
                journal("Новый планшет: %s (%s, %s)" % (dev["name"], dev.get("model"), ip))
            elif dev["state"] == "ok":
                journal("%s: все учебники на месте (%s шт.)" % (dev["name"], dev.get("have")))
            else:
                journal("%s: ошибка — %s" % (dev["name"], dev.get("error")))
        if is_new or time.time() - dev.get("_saved", 0) > 300:
            dev["_saved"] = time.time()
            save_devices()
        return {"name": dev["name"], "interval": POLL_INTERVAL}


def devices_state():
    now = time.time()
    with DEV_LOCK:
        out = []
        for dev_id, d in DEVICES.items():
            item = {k: v for k, v in d.items() if not k.startswith("_")}
            item["id"] = dev_id
            item["online"] = now - (d.get("last_seen") or 0) < ONLINE_TIMEOUT
            out.append(item)
    return out


# ---------------------------------------------------------------- сеть

def ip_rank(ip):
    """Обычные школьные/домашние сети — первыми; VPN-туннели и служебные адреса — отбрасываем."""
    a, b = (int(x) for x in ip.split(".")[:2])
    if a == 127 or (a == 169 and b == 254) or (a == 198 and b in (18, 19)):
        return None  # loopback, link-local, туннели прокси/VPN (FlClash, Clash и т.п.)
    if a == 192 and b == 168:
        return 0
    if a == 10 or (a == 172 and 16 <= b <= 31):
        return 1
    if a == 100 and 64 <= b <= 127:
        return 3  # Tailscale/CGNAT
    return 2


def local_ips():
    found = []
    try:  # все IPv4-адреса интерфейсов (Linux)
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "up"], capture_output=True, text=True, timeout=5).stdout
        found += re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # пакет не отправляется, просто выбирается интерфейс
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    ranked = {ip: ip_rank(ip) for ip in found}
    return sorted((ip for ip, r in ranked.items() if r is not None), key=lambda ip: (ranked[ip], found.index(ip)))


def discovery_loop():
    """Отвечает планшетам, которые ищут компьютер широковещательным UDP-запросом."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", DISCOVERY_PORT))
    except OSError as e:
        print("Поиск по сети не работает (порт %d занят?): %s" % (DISCOVERY_PORT, e))
        return
    reply = json.dumps({"port": PORT, "host": socket.gethostname()}).encode()
    while True:
        try:
            data, addr = sock.recvfrom(256)
            if data.strip() == DISCOVERY_REQUEST:
                sock.sendto(reply, addr)
        except OSError:
            time.sleep(1)


def is_local(client_ip):
    return client_ip.startswith("127.") or client_ip == "::1"


# ---------------------------------------------------------------- инструкция (простой markdown)

def inline_md(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)


def render_markdown(md):
    out, para, list_tag, in_code = [], [], None, False

    def flush():
        nonlocal list_tag
        if para:
            out.append("<p>%s</p>" % inline_md(" ".join(para)))
            para.clear()
        if list_tag:
            out.append("</%s>" % list_tag)
            list_tag = None

    for line in md.splitlines():
        if line.startswith("```"):
            flush()
            out.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        m_h = re.match(r"(#{1,4}) (.*)", line)
        m_ul = re.match(r"\s*[-*] (.*)", line)
        m_ol = re.match(r"\s*(\d+)\. (.*)", line)
        if m_h:
            flush()
            lvl, title = len(m_h.group(1)), m_h.group(2)
            m_id = re.search(r"\s*\{#([\w-]+)\}$", title)  # "## Заголовок {#якорь}"
            anchor = ' id="%s"' % m_id.group(1) if m_id else ""
            if m_id:
                title = title[:m_id.start()]
            out.append("<h%d%s>%s</h%d>" % (lvl, anchor, inline_md(title), lvl))
        elif m_ul or m_ol:
            tag = "ul" if m_ul else "ol"
            if para:
                out.append("<p>%s</p>" % inline_md(" ".join(para)))
                para.clear()
            if list_tag != tag:
                if list_tag:
                    out.append("</%s>" % list_tag)
                out.append("<%s>" % tag)
                list_tag = tag
            if m_ul:
                out.append("<li>%s</li>" % inline_md(m_ul.group(1)))
            else:  # номер сохраняем, даже если список прерван блоком кода
                out.append('<li value="%s">%s</li>' % (m_ol.group(1), inline_md(m_ol.group(2))))
        elif line.startswith("> "):
            flush()
            out.append('<div class="note">%s</div>' % inline_md(line[2:]))
        elif not line.strip():
            flush()
        elif list_tag and line.startswith("  "):
            out[-1] = out[-1][:-5] + " " + inline_md(line.strip()) + "</li>"
        else:
            if list_tag:
                flush()
            para.append(line.strip())
    flush()
    return "\n".join(out)


PAGE_STYLE = """<style>
:root{--bg:#f6f5f2;--fg:#1d1d1b;--muted:#6b6a65;--card:#fff;--line:#e3e1db;--accent:#2f6f4f;--accent-fg:#fff;--code:#efede7}
@media (prefers-color-scheme:dark){:root{--bg:#18191a;--fg:#ecebe7;--muted:#a3a29c;--card:#222325;--line:#36373a;--accent:#6cc296;--accent-fg:#0f2419;--code:#2c2d30}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,sans-serif}
main{max-width:820px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:28px;line-height:1.25}h2{margin-top:40px;padding-top:16px;border-top:1px solid var(--line)}
a{color:var(--accent)}code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.92em}
pre{background:var(--code);padding:12px 14px;border-radius:8px;overflow-x:auto;font-size:14px}
.note{background:var(--card);border-left:4px solid var(--accent);padding:10px 14px;border-radius:6px;margin:12px 0}
li{margin:4px 0}
.big{display:block;text-align:center;background:var(--accent);color:var(--accent-fg);text-decoration:none;font-size:22px;font-weight:700;padding:18px;border-radius:12px;margin:20px 0}
.books a{display:block;padding:8px 0;border-bottom:1px solid var(--line);text-decoration:none}
.muted{color:var(--muted)}
</style>"""

HELP_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Инструкция</title>%s</head>
<body><main><a href="/">← Вернуться к программе</a>
%s</main></body></html>"""

TABLET_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Учебники</title>%s</head>
<body><main>
<h1>Учебники</h1>
<p>Установите на этот планшет приложение «Учебники» — оно само скачает все учебники в папку <b>Books</b>
и будет докачивать новые.</p>
%s
<ol>
<li>Нажмите кнопку выше и дождитесь скачивания файла <b>uchebniki.apk</b>.</li>
<li>Откройте скачанный файл (в уведомлениях или в «Загрузках»).</li>
<li>Если планшет спросит, разрешите установку из этого источника и нажмите <b>«Установить»</b>.</li>
<li>Откройте приложение «Учебники» и нажмите кнопки, которые оно покажет (разрешить доступ к файлам и уведомления).</li>
</ol>
<h2>Или скачать учебники вручную</h2>
<p class="muted">%s</p>
<div class="books">%s</div>
</main></body></html>"""


def tablet_page():
    apk = ('<a class="big" href="/app.apk">Скачать приложение</a>' if APK_FILE.exists()
           else '<p class="note">Файл приложения не найден на компьютере (apk/uchebniki.apk).</p>')
    books = MANIFEST["books"]
    links = "".join('<a href="/books/%s">%s <span class="muted">· %s</span></a>'
                    % (quote(b["path"]), html.escape(b["path"]), human(b["size"])) for b in books)
    count = "Учебников: %d" % len(books) if books else "Учебников пока нет."
    return TABLET_PAGE % (PAGE_STYLE, apk, count, links)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Uchebniki"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_body(self, body, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, data, code=200):
        self.send_body(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code)

    def send_html(self, text):
        self.send_body(text.encode("utf-8"), "text/html; charset=utf-8")

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            raise ApiError("Слишком большой запрос")
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def dispatch(self, method):
        # http.server читает строку запроса как latin-1; возвращаем UTF-8 для русских имён
        url = urlparse(self.path.encode("latin-1").decode("utf-8", "replace"))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        local = is_local(self.client_address[0])
        try:
            # --- доступно планшетам в сети
            if method in ("GET", "HEAD") and path == "/":
                if local:
                    return self.send_html((STATIC / "index.html").read_text("utf-8"))
                return self.send_html(tablet_page())
            if method == "GET" and path == "/api/manifest":
                return self.send_json(MANIFEST)
            if method == "POST" and path == "/api/report":
                return self.send_json(handle_report(self.read_json(), self.client_address[0]))
            if method in ("GET", "HEAD") and path.startswith("/books/"):
                return self.send_book(unquote(path[len("/books/"):]))
            if method in ("GET", "HEAD") and path == "/app.apk":
                return self.send_file(APK_FILE, "application/vnd.android.package-archive", "uchebniki.apk")
            if method == "GET" and path == "/help":
                return self.send_html(HELP_PAGE % (PAGE_STYLE, render_markdown(HELP_FILE.read_text("utf-8"))))

            # --- админка: только с этого компьютера
            if not path.startswith("/api/admin/"):
                return self.send_json({"error": "Не найдено"}, 404)
            if not local:
                return self.send_json({"error": "Управление доступно только на компьютере с программой"}, 403)
            return self.admin(method, path[len("/api/admin/"):], params)
        except ApiError as e:
            self.send_json({"error": str(e)}, 400)
        except (KeyError, ValueError) as e:
            self.send_json({"error": "Неверный запрос: %s" % e}, 400)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as e:
            self.send_json({"error": "Внутренняя ошибка: %s" % e}, 500)

    def admin(self, method, route, params):
        if method == "GET" and route == "state":
            ips = local_ips()
            return self.send_json({
                "books": [{"path": b["path"], "size": b["size"]} for b in MANIFEST["books"]],
                "version": MANIFEST["version"], "devices": devices_state(),
                "addresses": ["http://%s:%d" % (ip, PORT) for ip in ips], "apk": APK_FILE.exists(),
            })
        if method == "GET" and route == "journal":
            text = JOURNAL_FILE.read_text("utf-8") if JOURNAL_FILE.exists() else ""
            return self.send_json({"text": text[-60000:]})
        if method == "PUT" and route == "upload":
            return self.upload(params.get("path", ""))
        body = self.read_json() if method == "POST" else {}
        if method == "POST" and route == "delete":
            removed = []
            for p in body.get("paths", []):
                f = LIBRARY / safe_rel(p)
                if f.is_file():
                    f.unlink()
                    removed.append(p)
            remove_empty_dirs(LIBRARY)
            scan_library()
            if removed:
                journal("Удалены учебники (%d): %s" % (len(removed), ", ".join(removed)))
            return self.send_json({"ok": True})
        if method == "POST" and route == "rename":
            name = (body.get("name") or "").strip()[:60]
            if not name:
                raise ApiError("Имя не может быть пустым")
            with DEV_LOCK:
                DEVICES[body["id"]]["name"] = name
                save_devices()
            return self.send_json({"ok": True})
        if method == "POST" and route == "forget":
            with DEV_LOCK:
                DEVICES.pop(body["id"], None)
                save_devices()
            return self.send_json({"ok": True})
        return self.send_json({"error": "Не найдено"}, 404)

    def upload(self, rel):
        rel = safe_rel(rel)
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        if ext not in BOOK_EXT:
            raise ApiError("Формат .%s не поддерживается" % ext if ext else "У файла нет расширения")
        remaining = int(self.headers.get("Content-Length") or 0)
        dest = LIBRARY / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / (".%s.part" % dest.name)
        try:
            with tmp.open("wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise ApiError("Загрузка прервалась")
                    f.write(chunk)
                    remaining -= len(chunk)
            replaced = dest.exists()
            tmp.replace(dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        scan_library()
        journal("%s учебник: %s" % ("Заменён" if replaced else "Добавлен", rel))
        self.send_json({"ok": True, "path": rel})

    def send_book(self, rel):
        rel = safe_rel(rel)
        if rel not in INDEX:
            return self.send_json({"error": "Нет такого учебника"}, 404)
        self.send_file(LIBRARY / rel, "application/octet-stream", rel.rsplit("/", 1)[-1], limited=True)

    def send_file(self, path, ctype, filename, limited=False):
        """Отдаёт файл, поддерживает докачку (Range: bytes=N-)."""
        if not path.is_file():
            return self.send_json({"error": "Файл не найден"}, 404)
        if limited and not DOWNLOADS.acquire(blocking=False):
            # Слишком много планшетов качают одновременно — пусть подождут, Wi-Fi не резиновый
            return self.send_body(b"busy", "text/plain", 503, {"Retry-After": "10"})
        try:
            size = path.stat().st_size
            start = 0
            m = re.fullmatch(r"bytes=(\d+)-", self.headers.get("Range") or "")
            if m:
                start = int(m.group(1))
                if start >= size:
                    return self.send_body(b"", ctype, 416, {"Content-Range": "bytes */%d" % size})
            self.send_response(206 if start else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size - start))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(filename))
            if start:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, size - 1, size))
            self.end_headers()
            if self.command == "HEAD":
                return
            with path.open("rb") as f:
                f.seek(start)
                for chunk in iter(lambda: f.read(256 * 1024), b""):
                    self.wfile.write(chunk)
        finally:
            if limited:
                DOWNLOADS.release()

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    sys.stdout.reconfigure(line_buffering=True)
    LIBRARY.mkdir(exist_ok=True)
    print("Проверяю учебники в папке library…")
    load_index()
    load_devices()
    threading.Thread(target=library_loop, daemon=True).start()
    threading.Thread(target=discovery_loop, daemon=True).start()
    try:
        server = Server(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print("Не удалось запустить на порту %d: %s\nВозможно, программа уже запущена." % (PORT, e))
        sys.exit(1)
    print("Программа запущена. Управление: http://127.0.0.1:%d" % PORT)
    for ip in local_ips():
        print("Адрес для планшетов:  http://%s:%d" % (ip, PORT))
    print("Чтобы остановить — закройте это окно или нажмите Ctrl+C")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, webbrowser.open, args=("http://127.0.0.1:%d" % PORT,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()
