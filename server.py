#!/usr/bin/env python3
"""Minimal yt-dlp web frontend. Standard library only; yt-dlp runs as a
detached subprocess per job, so downloads survive closing the browser and
even restarting this server. All job state lives on disk in DATA_DIR/<id>/.
"""
import json, os, re, shutil, signal, sys, threading, time, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote, unquote, urlparse, parse_qs
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.environ.get("DATA_DIR", os.path.join(HERE, "downloads")))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "2"))
BIN = os.path.dirname(sys.executable)  # venv bin: yt-dlp, deno, ffmpeg
# Netscape cookies.txt used for every job (e.g. to pass YouTube's bot check). Never served over HTTP.
COOKIES = os.path.abspath(os.environ.get("COOKIES_FILE", os.path.join(HERE, "cookies.txt")))
SETTINGS = os.path.abspath(os.environ.get("SETTINGS_FILE", os.path.join(HERE, "settings.json")))
DEFAULTS = {"ipv4": True,  # force IPv4 (-4): many hosts have flaky/blocked IPv6 to YouTube
            "retries": 3}  # automatic retries for a failed job (0 = off)
# Errors that retrying won't fix: give up immediately instead of hammering the site.
PERMANENT = re.compile(r"Unsupported URL|is not a valid URL|Video unavailable|Private video|"
                       r"has been removed|members-only|Join this channel|confirm your age|"
                       r"not a bot|Requested format is not available|HTTP Error 404|"
                       r"No video formats found|only works when logged-in", re.I)
JOB_COOKIES = ".cookies.txt"  # per-job private copy; yt-dlp rewrites it, removed when the job ends
INTERNAL = {"job.json", "log.txt", "rc", "pid", "cancelled", "title.txt", "attempts", "noretry"}
PARTIAL = re.compile(r"\.(part|ytdl|temp)$|\.part-Frag\d+|\.f\d+\.\w+$")
ID_RE = re.compile(r"^[0-9a-f]{12}$")
LANGS_RE = re.compile(r"^[\w.,*\-]{1,100}$")
lock = threading.Lock()

def _res(h):  # best stream up to height h, preferring MP4/M4A; yt-dlp falls back if unavailable
    return ["-S", f"res:{h},ext:mp4:m4a", "--merge-output-format", "mp4"]


FORMATS = {
    "best":  [],
    "mp4":   ["-S", "res,ext:mp4:m4a", "--merge-output-format", "mp4"],
    "2160":  _res(2160), "1440": _res(1440), "1080": _res(1080),
    "720":   _res(720), "480": _res(480), "360": _res(360),
    "audio": ["-f", "ba/b", "-x"],
    "mp3":   ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"],
    "m4a":   ["-f", "ba[ext=m4a]/ba/b", "-x", "--audio-format", "m4a"],
    "opus":  ["-f", "ba[acodec=opus]/ba/b", "-x", "--audio-format", "opus"],
    "wav":   ["-f", "ba/b", "-x", "--audio-format", "wav"],
    "none":  ["--skip-download"],
}


def build_args(o):
    a = ["--newline", "--no-colors", "--no-mtime", "--windows-filenames",
         "-o", "%(title).150B [%(id)s].%(ext)s",
         "--print-to-file", "%(title)s", "title.txt",
         "--progress-template",
         "download:[dl] %(progress._percent_str)s of %(progress._total_bytes_str)s"
         " at %(progress._speed_str)s ETA %(progress._eta_str)s"]
    a += FORMATS.get(o.get("format"), [])
    a.append("--yes-playlist" if o.get("playlist") else "--no-playlist")
    a += ["--write-thumbnail", "--convert-thumbnails", "jpg"]  # always: used as the card preview
    if o.get("subs") or o.get("autosubs"):
        langs = o.get("langs") or "en.*"
        a += ["--sub-langs", langs if LANGS_RE.match(langs) else "en.*", "--convert-subs", "srt"]
        if o.get("subs"): a.append("--write-subs")
        if o.get("autosubs"): a.append("--write-auto-subs")
    if o.get("description"): a.append("--write-description")
    if o.get("infojson"): a.append("--write-info-json")
    if o.get("embed") and o.get("format") != "none":
        a += ["--embed-metadata", "--embed-thumbnail", "--embed-chapters"]
        if o.get("subs") or o.get("autosubs"): a.append("--embed-subs")
    return a


def jdir(jid): return os.path.join(DATA, jid)


def cookie_rows(text):
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"): line = line[10:]
        elif line.startswith("#") or not line.strip(): continue
        f = line.split("\t")
        if len(f) == 7: yield f


def cookies_info():
    text = read(COOKIES)
    if not text: return {"set": False}
    doms = {}
    for f in cookie_rows(text):
        d = f[0].lstrip(".")
        doms[d] = doms.get(d, 0) + 1
    top = sorted(doms, key=lambda d: -doms[d])
    return {"set": True, "count": sum(doms.values()), "domains": top[:6], "more": max(0, len(top) - 6),
            "updated": os.path.getmtime(COOKIES)}


_settings = None


def get_settings():
    global _settings
    if _settings is None:
        try: saved = json.loads(read(SETTINGS) or "{}")
        except ValueError: saved = {}
        _settings = {k: type(v)(saved.get(k, v)) for k, v in DEFAULTS.items()}
    return dict(_settings)


def save_settings(new):
    global _settings
    write_private(SETTINGS, json.dumps(new)); _settings = dict(new)


def retry_delay(n):  # no wait: flaky sites often succeed on an immediate retry
    return 0


def retry_info(jid):
    """(next_attempt, max, retry_at) if this failed job will be retried automatically, else None."""
    d = jdir(jid)
    if os.path.exists(os.path.join(d, "noretry")): return None
    tries, mx = int(read(os.path.join(d, "attempts")) or 0), get_settings()["retries"]
    if tries >= mx: return None
    try: failed_at = os.path.getmtime(os.path.join(d, "rc"))
    except OSError: return None
    return tries + 1, mx, failed_at + retry_delay(tries)


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(text)


def read(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f: return f.read()
    except OSError: return default


def pid_alive(jid):
    pid = read(os.path.join(jdir(jid), "pid"))
    if not pid: return None
    pid = int(pid)
    try:  # guard against pid reuse after reboot: process cwd must be the job dir
        return os.path.realpath(f"/proc/{pid}/cwd") == os.path.realpath(jdir(jid)) and pid
    except OSError:
        return False


def status(jid):
    d = jdir(jid)
    if os.path.exists(os.path.join(d, "cancelled")): return "cancelled"
    rc = read(os.path.join(d, "rc"))
    if rc is not None:
        if rc.strip() == "0": return "done"
        return "retrying" if retry_info(jid) else "error"
    if not os.path.exists(os.path.join(d, "pid")): return "queued"
    return "running" if pid_alive(jid) else "interrupted"


def read_tail(path, n=6000):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - n)); return f.read().decode("utf-8", "replace")
    except OSError: return ""


def last_line(path, n=4096):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - n))
            lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
            return lines[-1] if lines else ""
    except OSError: return ""


def job_info(jid):
    d = jdir(jid)
    meta = json.loads(read(os.path.join(d, "job.json"), "{}"))
    st = status(jid)
    titles = [t for t in (read(os.path.join(d, "title.txt")) or "").splitlines() if t]
    files = []
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if name in INTERNAL or name.startswith(".") or PARTIAL.search(name) or not os.path.isfile(p): continue
        files.append({"name": name, "size": os.path.getsize(p)})
    line = last_line(os.path.join(d, "log.txt"))
    return {"id": jid, "url": meta.get("url"), "opts": meta.get("opts", {}),
            "created": meta.get("created", 0), "status": st,
            "title": titles[0] if titles else None, "count": len(titles),
            "progress": line if st in ("running", "error", "interrupted", "retrying") else "",
            "retry": dict(zip(("attempt", "max", "at"), retry_info(jid))) if st == "retrying" else None,
            "files": files}


def all_ids():
    try: return [i for i in os.listdir(DATA) if ID_RE.match(i)]
    except OSError: return []


def start(jid):
    d = jdir(jid)
    meta = json.loads(read(os.path.join(d, "job.json")))
    cmd = [os.path.join(BIN, "yt-dlp")] + build_args(meta["opts"])
    if get_settings()["ipv4"]: cmd.append("-4")
    text = read(COOKIES)
    if text:
        write_private(os.path.join(d, JOB_COOKIES), text)
        cmd += ["--cookies", JOB_COOKIES]
    cmd += ["--", meta["url"]]
    env = dict(os.environ, PATH=BIN + os.pathsep + os.environ.get("PATH", ""))
    # sh wrapper records the exit code even if this server is gone by then.
    p = subprocess.Popen(["sh", "-c", f'"$@" >>log.txt 2>&1; echo $? >rc; rm -f {JOB_COOKIES}', "sh"] + cmd,
                         cwd=d, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    with open(os.path.join(d, "pid"), "w") as f: f.write(str(p.pid))


def scheduler():
    while True:
        try:
            try:  # reap finished children (rc is written by the sh wrapper)
                while os.waitpid(-1, os.WNOHANG)[0]: pass
            except ChildProcessError: pass
            with lock:
                ids = sorted(all_ids(), key=lambda i: os.path.getmtime(os.path.join(jdir(i), "job.json")))
                sts = {i: status(i) for i in ids}
                for i, s in sts.items():  # killed by reboot etc. -> requeue (yt-dlp resumes .part)
                    if s == "interrupted":
                        os.remove(os.path.join(jdir(i), "pid")); sts[i] = "queued"
                    if s == "retrying":
                        d = jdir(i)
                        if PERMANENT.search(read_tail(os.path.join(d, "log.txt"))):
                            open(os.path.join(d, "noretry"), "w").close(); sts[i] = "error"
                        else:
                            n, mx, at = retry_info(i)
                            if time.time() >= at:
                                with open(os.path.join(d, "attempts"), "w") as f: f.write(str(n))
                                with open(os.path.join(d, "log.txt"), "a") as f:
                                    f.write(f"\n--- automatic retry {n}/{mx} ---\n")
                                for x in ("rc", "pid"):
                                    try: os.remove(os.path.join(d, x))
                                    except OSError: pass
                                sts[i] = "queued"
                    if s in ("done", "error", "retrying", "cancelled", "interrupted"):  # e.g. cancelled before sh's rm ran
                        try: os.remove(os.path.join(jdir(i), JOB_COOKIES))
                        except OSError: pass
                free = MAX_JOBS - sum(s == "running" for s in sts.values())
                for i in [i for i in ids if sts[i] == "queued"][:max(0, free)]:
                    start(i)
        except Exception as e:
            print("scheduler:", e, file=sys.stderr)
        time.sleep(2)


def cancel(jid):
    d = jdir(jid)
    open(os.path.join(d, "cancelled"), "w").close()
    pid = pid_alive(jid)
    if pid:
        try: os.killpg(pid, signal.SIGTERM)
        except OSError: pass


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a): pass

    def send(self, code, body=b"", ctype="application/json"):
        if not isinstance(body, bytes): body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)

    def route(self):
        path = urlparse(self.path).path
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if parts and parts[0] == "api" and len(parts) > 2 and not ID_RE.match(parts[2]):
            return None
        return path, parts

    def do_HEAD(self): self.do_GET()

    def do_GET(self):
        r = self.route()
        if not r: return self.send(404, {"error": "not found"})
        path, p = r
        if path == "/":
            return self.send(200, read(os.path.join(HERE, "index.html")).encode(), "text/html; charset=utf-8")
        if p == ["api", "cookies"]:
            return self.send(200, cookies_info())
        if p == ["api", "settings"]:
            return self.send(200, get_settings())
        if p == ["api", "jobs"]:
            jobs = []
            for i in all_ids():
                try: jobs.append(job_info(i))
                except Exception: pass
            return self.send(200, sorted(jobs, key=lambda j: -j["created"]))
        if len(p) == 4 and p[:2] == ["api", "jobs"] and p[3] == "log":
            return self.send(200, (read(os.path.join(jdir(p[2]), "log.txt")) or "")[-20000:].encode(),
                             "text/plain; charset=utf-8")
        if len(p) == 3 and p[0] == "f" and ID_RE.match(p[1]):
            return self.file(p[1], p[2], "dl" in parse_qs(urlparse(self.path).query, keep_blank_values=True))
        self.send(404, {"error": "not found"})

    def file(self, jid, name, attach):
        if "/" in name or name in INTERNAL or name.startswith("."):
            return self.send(404, {"error": "not found"})
        fp = os.path.join(jdir(jid), name)
        if not os.path.isfile(fp): return self.send(404, {"error": "not found"})
        size = os.path.getsize(fp)
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if m and size:
            if m[1]: start, end = int(m[1]), int(m[2]) if m[2] else size - 1
            elif m[2]: start = max(0, size - int(m[2]))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416); self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0"); self.end_headers(); return
            code = 206
        ext = name.rsplit(".", 1)[-1].lower()
        ctype = {"mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska", "m4a": "audio/mp4",
                 "mp3": "audio/mpeg", "opus": "audio/ogg", "ogg": "audio/ogg", "jpg": "image/jpeg",
                 "png": "image/png", "webp": "image/webp", "srt": "text/plain; charset=utf-8",
                 "vtt": "text/vtt", "json": "application/json", "description": "text/plain; charset=utf-8",
                 }.get(ext, "application/octet-stream")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition",
                         f"{'attachment' if attach else 'inline'}; filename*=UTF-8''{quote(name)}")
        self.end_headers()
        if self.command == "HEAD": return
        with open(fp, "rb") as f:
            try:  # zero-copy where possible
                os.sendfile(self.wfile.fileno(), f.fileno(), start, end - start + 1)
            except (AttributeError, OSError, ValueError):
                f.seek(start); left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk: break
                    self.wfile.write(chunk); left -= len(chunk)

    def drain(self, limit=5_000_000):
        """Read the whole request body up front. Leaving any of it unread would corrupt
        the next request on the same keep-alive connection."""
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self.close_connection = True; self._raw = b""
        else:
            self._raw = self.rfile.read(n) if n else b""

    def body(self, limit=100_000):
        raw = getattr(self, "_raw", b"")
        try: return json.loads(raw) if raw and len(raw) <= limit else {}
        except ValueError: return {}

    def do_POST(self):
        self.drain()
        r = self.route()
        if not r: return self.send(404, {"error": "not found"})
        _, p = r
        # JSON-only: other sites can't send this cross-origin without a CORS preflight (we never allow one)
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send(415, {"error": "expected application/json"})
        if p == ["api", "settings"]:
            new = get_settings()
            for k, v in self.body().items():
                if k in DEFAULTS and type(v) is type(DEFAULTS[k]): new[k] = v
            new["retries"] = max(0, min(10, new["retries"]))
            save_settings(new)
            return self.send(200, new)
        if p == ["api", "cookies"]:
            text = str(self.body(5_000_000).get("text", "")).replace("\r\n", "\n")
            if not any(True for _ in cookie_rows(text)):
                return self.send(400, {"error": "Not a cookies.txt file (Netscape format, tab-separated)."})
            write_private(COOKIES, text if text.endswith("\n") else text + "\n")
            return self.send(200, cookies_info())
        if p == ["api", "jobs"]:
            b = self.body()
            urls = [u.strip() for u in str(b.get("url", "")).split() if u.strip()]
            urls = [u for u in urls if re.match(r"^https?://", u)]
            if not urls: return self.send(400, {"error": "enter at least one http(s) URL"})
            opts = {k: b.get(k) for k in ("format", "playlist", "subs", "autosubs",
                                          "langs", "description", "infojson", "embed")}
            ids = []
            for u in urls:
                jid = uuid.uuid4().hex[:12]
                os.makedirs(jdir(jid))
                with open(os.path.join(jdir(jid), "job.json"), "w") as f:
                    json.dump({"url": u, "opts": opts, "created": time.time()}, f)
                ids.append(jid)
            return self.send(201, {"ids": ids})
        if len(p) == 4 and p[:2] == ["api", "jobs"] and os.path.isdir(jdir(p[2])):
            jid = p[2]
            with lock:
                if p[3] == "cancel":
                    cancel(jid)
                elif p[3] == "retry":
                    if status(jid) == "running": return self.send(409, {"error": "running"})
                    for n in ("rc", "pid", "cancelled", "attempts", "noretry"):
                        try: os.remove(os.path.join(jdir(jid), n))
                        except OSError: pass
                    with open(os.path.join(jdir(jid), "log.txt"), "a") as f: f.write("\n--- retry ---\n")
                else: return self.send(404, {"error": "not found"})
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    def do_DELETE(self):
        self.drain()
        r = self.route()
        if not r: return self.send(404, {"error": "not found"})
        _, p = r
        if p == ["api", "cookies"]:
            try: os.remove(COOKIES)
            except OSError: pass
            return self.send(200, cookies_info())
        if len(p) == 3 and p[:2] == ["api", "jobs"] and os.path.isdir(jdir(p[2])):
            with lock:
                if status(p[2]) == "running":
                    cancel(p[2]); time.sleep(1)
                shutil.rmtree(jdir(p[2]), ignore_errors=True)
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    threading.Thread(target=scheduler, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    srv.daemon_threads = True
    print(f"yt-web on http://{HOST}:{PORT}  (files in {DATA})")
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
