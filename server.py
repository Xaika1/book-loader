#!/usr/bin/env python3
"""Загрузка книг на много Android-планшетов одновременно через ADB.

Запуск: python3 server.py   (откроется страница http://127.0.0.1:8765)
Нужен только Python 3.8+ и программа adb (пакет android-tools).
"""
import html
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

BASE = Path(__file__).resolve().parent
LIBRARY = BASE / "library"
STATIC = BASE / "static"
CONFIG_FILE = BASE / "config.json"
JOURNAL_FILE = BASE / "journal.log"
HELP_FILE = BASE / "ИНСТРУКЦИЯ.md"

ADB = os.environ.get("ADB") or shutil.which("adb") or "adb"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))

BOOK_EXT = {
    "pdf", "epub", "fb2", "djvu", "djv", "mobi", "azw", "azw3", "txt", "rtf",
    "doc", "docx", "odt", "chm", "cbz", "cbr", "html", "htm", "zip",
}

DEFAULT_CONFIG = {
    "target_dir": "/sdcard/Books",  # куда класть книги на планшете
    "parallel": 10,                 # сколько планшетов обслуживать одновременно
    "devices": {},                  # serial -> {"name", "model"}
    "wifi": [],                     # сохранённые адреса ip:port
}

CONFIG_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
DEVICES = {}          # serial -> словарь с состоянием устройства
ADB_STATUS = {"ok": False, "error": "Запуск..."}
JOB = None            # текущая/последняя операция
INFO_POOL = ThreadPoolExecutor(max_workers=8)


# ---------------------------------------------------------------- настройки

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text("utf-8")))
        except (OSError, ValueError):
            print("config.json повреждён, используются настройки по умолчанию")
    return cfg


CONFIG = load_config()


def save_config():
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(CONFIG_FILE)


def next_device_name():
    used = set()
    for d in CONFIG["devices"].values():
        m = re.fullmatch(r"Планшет (\d+)", d.get("name", ""))
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return "Планшет %d" % n


# ---------------------------------------------------------------- adb

class AdbError(Exception):
    pass


class ApiError(Exception):
    pass


def friendly_error(text):
    t = (text or "").strip()
    low = t.lower()
    if "no space" in low or "enospc" in low:
        return "Не хватает места на планшете"
    if "not found" in low and "device" in low or "offline" in low or "closed" in low:
        return "Планшет отключился"
    if "unauthorized" in low:
        return "Не разрешена отладка — нажмите «Разрешить» на экране планшета"
    if "permission denied" in low or "read-only" in low:
        return "Нет доступа к папке на планшете (проверьте папку в настройках)"
    return t.splitlines()[-1] if t else "Неизвестная ошибка"


def run_adb(args, serial=None, timeout=60, job=None):
    cmd = [ADB] + (["-s", serial] if serial else []) + list(args)
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise AdbError("Программа adb не найдена. Установите пакет android-tools")
    if job is not None:
        job["_procs"].add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise AdbError("Планшет не отвечает (превышено время ожидания)")
    finally:
        if job is not None:
            job["_procs"].discard(p)
    return p.returncode, out, err


def shell(serial, command, timeout=30, job=None):
    return run_adb(["shell", command], serial, timeout, job)


def q(s):
    return shlex.quote(s)


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def parse_size(token):
    """Размер из вывода df: число в КБ или '12G', '512M'."""
    m = re.fullmatch(r"([\d.]+)([KMGT]?)", token.upper())
    if not m:
        return None
    mult = {"": 1024, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)]
    return int(float(m.group(1)) * mult)


def list_adb_devices():
    rc, out, err = run_adb(["devices", "-l"], timeout=15)
    if rc != 0:
        raise AdbError(err.strip() or "adb не отвечает")
    found = []
    for line in out.splitlines():
        if not line.strip() or line.startswith(("List of", "*")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        state = parts[1]
        if state == "no":              # "no permissions"
            state = "nopermissions"
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        found.append({"serial": parts[0], "state": state,
                      "model": info.get("model", "").replace("_", " ")})
    return found


def update_info(serial):
    """Заряд батареи и свободное место — в фоне, раз в полминуты."""
    try:
        target = CONFIG["target_dir"]
        rc, out, _ = shell(serial, "dumpsys battery | grep -m1 level; df %s 2>/dev/null | tail -n 1"
                           % q(posixpath.dirname(target.rstrip("/")) or "/sdcard"), timeout=20)
        battery = free = total = None
        for line in out.splitlines():
            m = re.search(r"level:\s*(\d+)", line)
            if m:
                battery = int(m.group(1))
                continue
            tokens = line.split()
            nums = [t for t in tokens[1:] if parse_size(t) is not None]
            if len(nums) >= 3:
                total, free = parse_size(nums[0]), parse_size(nums[2])
        with STATE_LOCK:
            dev = DEVICES.get(serial)
            if dev:
                dev.update(battery=battery, free=free, total=total)
    except Exception:
        pass
    finally:
        with STATE_LOCK:
            dev = DEVICES.get(serial)
            if dev:
                dev["info_at"] = time.time()
                dev["info_pending"] = False


def refresh_devices():
    found = list_adb_devices()
    now = time.time()
    changed = False
    with STATE_LOCK:
        seen = set()
        for d in found:
            s = d["serial"]
            seen.add(s)
            dev = DEVICES.setdefault(s, {"serial": s, "battery": None, "free": None,
                                         "total": None, "info_at": 0, "info_pending": False})
            dev["state"] = d["state"]
            if d["model"]:
                dev["model"] = d["model"]
            dev.setdefault("model", "")
            if d["state"] == "device":
                with CONFIG_LOCK:
                    known = CONFIG["devices"].get(s)
                    if not known:
                        name = next_device_name()
                        if ":" in s:  # Wi-Fi: берём имя USB-двойника, если знаем его
                            name = dev.get("wifi_name") or name
                        CONFIG["devices"][s] = {"name": name, "model": dev["model"]}
                        changed = True
                    elif dev["model"] and known.get("model") != dev["model"]:
                        known["model"] = dev["model"]
                        changed = True
                if now - dev["info_at"] > 30 and not dev["info_pending"]:
                    dev["info_pending"] = True
                    INFO_POOL.submit(update_info, s)
        for s, dev in DEVICES.items():
            if s not in seen:
                dev["state"] = "disconnected"
        # известные, но ни разу не подключённые в этом запуске
        for s, known in CONFIG["devices"].items():
            if s not in DEVICES:
                DEVICES[s] = {"serial": s, "state": "disconnected", "model": known.get("model", ""),
                              "battery": None, "free": None, "total": None,
                              "info_at": 0, "info_pending": False}
    if changed:
        with CONFIG_LOCK:
            save_config()


def monitor_loop():
    while True:
        try:
            refresh_devices()
            ADB_STATUS.update(ok=True, error="")
        except Exception as e:
            ADB_STATUS.update(ok=False, error=str(e))
        time.sleep(2)


def reconnect_wifi():
    for addr in list(CONFIG.get("wifi", [])):
        try:
            run_adb(["connect", addr], timeout=8)
        except AdbError:
            pass


# ---------------------------------------------------------------- библиотека

def safe_rel(path):
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise ApiError("Недопустимое имя файла: %s" % path)
    return "/".join(parts)


def list_library():
    books = []
    for p in sorted(LIBRARY.rglob("*")):
        if not p.is_file() or p.name.startswith(".") or p.suffix == ".part":
            continue
        rel = p.relative_to(LIBRARY).as_posix()
        st = p.stat()
        books.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime,
                      "ext": p.suffix.lower().lstrip(".")})
    return books


def remove_empty_dirs(root):
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------- операции

def remote_sizes(serial, paths, job):
    sizes = {}
    for chunk in chunks(paths, 25):
        cmd = "stat -c '%s %n' " + " ".join(q(p) for p in chunk) + " 2>/dev/null"
        _, out, _ = shell(serial, cmd, 30, job)
        for line in out.splitlines():
            size, _, name = line.rstrip("\r").partition(" ")
            if size.isdigit():
                sizes[name] = int(size)
    return sizes


def media_scan(serial, paths):
    """Сообщаем Android о новых/удалённых файлах, чтобы читалки их увидели."""
    try:
        for chunk in chunks(paths, 15):
            cmd = "; ".join("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d %s >/dev/null 2>&1"
                            % q("file://" + quote(p)) for p in chunk)
            shell(serial, cmd, 60)
        shell(serial, "content call --uri content://media --method scan_volume "
                      "--arg external_primary >/dev/null 2>&1", 60)
    except AdbError:
        pass


def push_device(job, serial):
    st = job["devices"][serial]
    target = job["target_dir"]
    books = job["books"]
    remote = {rel: "%s/%s" % (target, rel) for rel in books}

    sizes = {rel: (LIBRARY / rel).stat().st_size for rel in books}
    existing = remote_sizes(serial, list(remote.values()), job)
    need = sum(size for rel, size in sizes.items() if existing.get(remote[rel]) != size)
    with STATE_LOCK:
        free = DEVICES.get(serial, {}).get("free")
    if free is not None and need > free:
        raise AdbError("Не хватает места: нужно %s, свободно %s" % (human(need), human(free)))

    dirs = sorted({posixpath.dirname(r) for r in remote.values()})
    for chunk in chunks(dirs, 25):
        rc, out, err = shell(serial, "mkdir -p " + " ".join(q(d) for d in chunk), 30, job)
        if rc != 0 or "denied" in out.lower():
            raise AdbError(friendly_error(err or out))

    pushed = []
    for rel in books:
        if job["cancelled"]:
            return
        local = LIBRARY / rel
        size = sizes[rel]
        if existing.get(remote[rel]) == size:
            st["skipped"] += 1
            st["done"] += 1
            continue
        st["current"] = rel
        timeout = 120 + size // (256 * 1024)  # не медленнее ~256 КБ/с
        rc, out, err = run_adb(["push", str(local), remote[rel]], serial, timeout, job)
        if rc != 0:
            if job["cancelled"]:
                return
            raise AdbError(friendly_error(err or out))
        pushed.append(remote[rel])
        st["done"] += 1
        st["bytes"] += size
    st["current"] = ""
    media_scan(serial, pushed)


def delete_device(job, serial):
    st = job["devices"][serial]
    target = job["target_dir"]
    paths = ["%s/%s" % (target, rel) for rel in job["books"]]
    for chunk in chunks(paths, 25):
        if job["cancelled"]:
            return
        rc, out, err = shell(serial, "rm -f " + " ".join(q(p) for p in chunk), 60, job)
        if rc != 0:
            raise AdbError(friendly_error(err or out))
        st["done"] += len(chunk)
    shell(serial, "find %s -mindepth 1 -type d -empty -delete 2>/dev/null" % q(target), 30, job)
    media_scan(serial, paths)


def run_device(job, serial, worker):
    st = job["devices"][serial]
    if job["cancelled"]:
        st["state"] = "cancelled"
        return
    st["state"] = "working"
    try:
        worker(job, serial)
        st["state"] = "cancelled" if job["cancelled"] else "done"
    except Exception as e:
        st["state"] = "cancelled" if job["cancelled"] else "error"
        st["error"] = str(e) if isinstance(e, AdbError) else friendly_error(str(e))
    finally:
        st["current"] = ""
        with STATE_LOCK:
            if serial in DEVICES:
                DEVICES[serial]["info_at"] = 0  # обновить свободное место


def run_job(job):
    worker = push_device if job["action"] == "push" else delete_device
    with ThreadPoolExecutor(max_workers=job["parallel"]) as ex:
        for serial in job["devices"]:
            ex.submit(run_device, job, serial, worker)
    job["running"] = False
    job["finished"] = time.time()
    write_journal(job)


def start_job(action, books, serials):
    global JOB
    if action not in ("push", "delete"):
        raise ApiError("Неизвестное действие")
    books = [safe_rel(b) for b in books]
    if not books:
        raise ApiError("Не выбрано ни одной книги")
    if not serials:
        raise ApiError("Не выбрано ни одного планшета")
    if action == "push":
        missing = [b for b in books if not (LIBRARY / b).is_file()]
        if missing:
            raise ApiError("Книга не найдена в библиотеке: %s" % missing[0])
    with STATE_LOCK:
        if JOB and JOB["running"]:
            raise ApiError("Дождитесь окончания текущей операции")
        job = {
            "id": uuid.uuid4().hex[:8], "action": action, "books": books,
            "target_dir": CONFIG["target_dir"].rstrip("/"),
            "parallel": max(1, int(CONFIG["parallel"])),
            "running": True, "cancelled": False,
            "started": time.time(), "finished": None, "_procs": set(),
            "devices": {s: {"state": "waiting", "done": 0, "total": len(books), "skipped": 0,
                            "bytes": 0, "current": "", "error": ""} for s in serials},
        }
        JOB = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


def cancel_job():
    job = JOB
    if job and job["running"]:
        job["cancelled"] = True
        for p in list(job["_procs"]):
            try:
                p.terminate()
            except OSError:
                pass


def retry_job():
    job = JOB
    if not job or job["running"]:
        raise ApiError("Нечего повторять")
    failed = [s for s, st in job["devices"].items() if st["state"] in ("error", "cancelled")]
    if not failed:
        raise ApiError("Ошибок не было")
    return start_job(job["action"], job["books"], failed)


def device_name(serial):
    return CONFIG["devices"].get(serial, {}).get("name", serial)


def write_journal(job):
    action = "ЗАГРУЗКА" if job["action"] == "push" else "УДАЛЕНИЕ"
    lines = ["%s  %s: %d книг(и) на %d планшет(ов)" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job["started"])),
        action, len(job["books"]), len(job["devices"]))]
    lines += ["    книга: %s" % b for b in job["books"]]
    for s, st in sorted(job["devices"].items(), key=lambda kv: device_name(kv[0])):
        res = {"done": "успешно", "error": "ОШИБКА: " + st["error"], "cancelled": "отменено"}.get(
            st["state"], st["state"])
        if st["skipped"]:
            res += " (уже были: %d)" % st["skipped"]
        lines.append("    %s [%s]: %s" % (device_name(s), s, res))
    with JOURNAL_FILE.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return ("%.0f %s" if unit in ("Б", "КБ") else "%.1f %s") % (n, unit)
        n /= 1024.0


# ---------------------------------------------------------------- действия с устройствами

def device_books(serial):
    target = CONFIG["target_dir"].rstrip("/")
    rc, out, err = shell(serial, "find %s -type f -exec stat -c '%%s %%n' {} + 2>/dev/null" % q(target), 60)
    lib = {b["path"]: b["size"] for b in list_library()}
    books = []
    for line in out.splitlines():
        size, _, name = line.rstrip("\r").partition(" ")
        if not size.isdigit() or not name.startswith(target + "/"):
            continue
        rel = name[len(target) + 1:]
        books.append({"path": rel, "size": int(size), "in_library": lib.get(rel) == int(size)})
    books.sort(key=lambda b: b["path"].lower())
    missing = sorted(set(lib) - {b["path"] for b in books if b["in_library"]})
    return {"books": books, "missing": missing}


def enable_wifi(serial):
    rc, out, _ = shell(serial, "ip -f inet addr show wlan0", 15)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    if not m:
        raise ApiError("Планшет не подключён к Wi-Fi")
    run_adb(["tcpip", "5555"], serial, 15)
    time.sleep(3)
    addr = "%s:5555" % m.group(1)
    return connect_wifi(addr, name_from=serial)


def connect_wifi(addr, name_from=None):
    addr = addr.strip()
    if not re.fullmatch(r"[\w.\-]+(:\d+)?", addr):
        raise ApiError("Адрес должен выглядеть так: 192.168.1.25 или 192.168.1.25:5555")
    if ":" not in addr:
        addr += ":5555"
    rc, out, err = run_adb(["connect", addr], timeout=15)
    text = (out + err).strip()
    if "connected" not in text or "cannot" in text or "failed" in text:
        raise ApiError("Не удалось подключиться к %s: %s" % (addr, text))
    with CONFIG_LOCK:
        if addr not in CONFIG["wifi"]:
            CONFIG["wifi"].append(addr)
        if name_from and addr not in CONFIG["devices"]:
            base = CONFIG["devices"].get(name_from, {})
            CONFIG["devices"][addr] = {"name": base.get("name", addr) + " (Wi-Fi)",
                                       "model": base.get("model", "")}
        save_config()
    return addr


# ---------------------------------------------------------------- состояние для страницы

def public_state():
    with STATE_LOCK:
        devices = []
        for s, d in DEVICES.items():
            known = CONFIG["devices"].get(s, {})
            devices.append({
                "serial": s, "name": known.get("name", ""), "state": d["state"],
                "model": d.get("model") or known.get("model", ""),
                "battery": d["battery"], "free": d["free"], "total": d["total"],
                "wifi": ":" in s,
            })
        job = None
        if JOB:
            job = {k: v for k, v in JOB.items() if not k.startswith("_")}
    return {"adb": ADB_STATUS, "devices": devices, "job": job,
            "settings": {"target_dir": CONFIG["target_dir"], "parallel": CONFIG["parallel"]}}


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


HELP_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Инструкция</title>
<style>
:root{--bg:#f6f5f2;--fg:#1d1d1b;--muted:#6b6a65;--card:#fff;--line:#e3e1db;--accent:#2f6f4f;--code:#efede7}
@media (prefers-color-scheme:dark){:root{--bg:#18191a;--fg:#ecebe7;--muted:#a3a29c;--card:#222325;--line:#36373a;--accent:#6cc296;--code:#2c2d30}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,sans-serif}
main{max-width:820px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:28px}h2{margin-top:40px;padding-top:16px;border-top:1px solid var(--line)}
a{color:var(--accent)}code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.92em}
pre{background:var(--code);padding:12px 14px;border-radius:8px;overflow-x:auto;font-size:14px}
.note{background:var(--card);border-left:4px solid var(--accent);padding:10px 14px;border-radius:6px;margin:12px 0}
li{margin:4px 0}.back{display:inline-block;margin-bottom:8px}
</style></head><body><main><a class="back" href="/">← Вернуться к программе</a>
%s</main></body></html>"""


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "BookLoader"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def dispatch(self, method):
        # http.server читает строку запроса как latin-1; возвращаем UTF-8 для русских имён
        url = urlparse(self.path.encode("latin-1").decode("utf-8", "replace"))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = (method, url.path)
        try:
            if route == ("GET", "/"):
                return self.send_html((STATIC / "index.html").read_text("utf-8"))
            if route == ("GET", "/help"):
                return self.send_html(HELP_PAGE % render_markdown(HELP_FILE.read_text("utf-8")))
            if route == ("GET", "/api/state"):
                return self.send_json(public_state())
            if route == ("GET", "/api/library"):
                return self.send_json({"books": list_library()})
            if route == ("PUT", "/api/library/upload"):
                return self.upload(params.get("path", ""))
            if route == ("GET", "/api/journal"):
                text = JOURNAL_FILE.read_text("utf-8") if JOURNAL_FILE.exists() else ""
                return self.send_json({"text": text[-60000:]})

            body = self.read_json() if method == "POST" else {}
            if route == ("POST", "/api/library/delete"):
                for p in body.get("paths", []):
                    f = LIBRARY / safe_rel(p)
                    if f.is_file():
                        f.unlink()
                remove_empty_dirs(LIBRARY)
                return self.send_json({"ok": True})
            if route == ("POST", "/api/job"):
                job = start_job(body.get("action"), body.get("books", []), body.get("serials", []))
                return self.send_json({"ok": True, "id": job["id"]})
            if route == ("POST", "/api/job/cancel"):
                cancel_job()
                return self.send_json({"ok": True})
            if route == ("POST", "/api/job/retry"):
                retry_job()
                return self.send_json({"ok": True})
            if route == ("POST", "/api/device/rename"):
                name = (body.get("name") or "").strip()[:60]
                if not name:
                    raise ApiError("Имя не может быть пустым")
                with CONFIG_LOCK:
                    CONFIG["devices"].setdefault(body["serial"], {})["name"] = name
                    save_config()
                return self.send_json({"ok": True})
            if route == ("POST", "/api/device/forget"):
                s = body["serial"]
                with CONFIG_LOCK:
                    CONFIG["devices"].pop(s, None)
                    if s in CONFIG["wifi"]:
                        CONFIG["wifi"].remove(s)
                    save_config()
                with STATE_LOCK:
                    if DEVICES.get(s, {}).get("state") == "disconnected":
                        DEVICES.pop(s, None)
                return self.send_json({"ok": True})
            if route == ("POST", "/api/device/wake"):
                shell(body["serial"], "input keyevent 224", 15)
                return self.send_json({"ok": True})
            if route == ("POST", "/api/device/books"):
                return self.send_json(device_books(body["serial"]))
            if route == ("POST", "/api/wifi/enable"):
                return self.send_json({"ok": True, "address": enable_wifi(body["serial"])})
            if route == ("POST", "/api/wifi/connect"):
                return self.send_json({"ok": True, "address": connect_wifi(body.get("address", ""))})
            if route == ("POST", "/api/adb/restart"):
                run_adb(["kill-server"], timeout=15)
                run_adb(["start-server"], timeout=30)
                threading.Thread(target=reconnect_wifi, daemon=True).start()
                return self.send_json({"ok": True})
            if route == ("POST", "/api/settings"):
                target = (body.get("target_dir") or "").strip().rstrip("/")
                if not target.startswith("/") or ".." in target or len(target) < 2:
                    raise ApiError("Папка должна начинаться с /, например /sdcard/Books")
                parallel = int(body.get("parallel") or 10)
                if not 1 <= parallel <= 64:
                    raise ApiError("Одновременно можно обслуживать от 1 до 64 планшетов")
                with CONFIG_LOCK:
                    CONFIG.update(target_dir=target, parallel=parallel)
                    save_config()
                return self.send_json({"ok": True})
            self.send_json({"error": "Не найдено"}, 404)
        except (ApiError, AdbError) as e:
            self.send_json({"error": str(e)}, 400)
        except (KeyError, ValueError) as e:
            self.send_json({"error": "Неверный запрос: %s" % e}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send_json({"error": "Внутренняя ошибка: %s" % e}, 500)

    def upload(self, rel):
        rel = safe_rel(rel)
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        if ext not in BOOK_EXT:
            raise ApiError("Формат .%s не поддерживается" % ext if ext else "У файла нет расширения")
        remaining = int(self.headers.get("Content-Length") or 0)
        dest = LIBRARY / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with tmp.open("wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise ApiError("Загрузка прервалась")
                    f.write(chunk)
                    remaining -= len(chunk)
            tmp.replace(dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        self.send_json({"ok": True, "path": rel})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")


def main():
    LIBRARY.mkdir(exist_ok=True)
    try:
        run_adb(["start-server"], timeout=30)
    except AdbError as e:
        print("ВНИМАНИЕ:", e)
    threading.Thread(target=reconnect_wifi, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = "http://%s:%d" % ("127.0.0.1" if HOST in ("0.0.0.0", "") else HOST, PORT)
    print("Программа запущена: %s" % url)
    print("Чтобы остановить — закройте это окно или нажмите Ctrl+C")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()
