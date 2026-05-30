"""
Instrumenting reverse proxy for the Claude Feedback library.

Forwards every request to a target app and injects the feedback widget into
HTML responses, while serving widget endpoints under /__cf/* locally. Lets you
make ANY running app (local dev server or remote site) commentable without
pre-instrumenting it. Stdlib only.

Usage:
    python lib/proxy.py <target-url> --port 5199 --feedback-dir <dir>
"""
import argparse, http.client, json, mimetypes, os, select, socket
import socketserver, sys, threading, time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

LIB_DIR = Path(__file__).resolve().parent
CF = "/__cf"

# --- auto-shutdown bookkeeping (mirrors server.py) ---
INITIAL_PPID = os.getppid()
_activity_lock = threading.Lock()
_last_activity = time.monotonic()

def _touch():
    global _last_activity
    with _activity_lock:
        _last_activity = time.monotonic()

def _idle_seconds():
    with _activity_lock:
        return time.monotonic() - _last_activity

# Response headers we drop/override when proxying HTML (and others):
# - CSP / X-Frame-Options would block our injected script.
# - length/encoding/connection are recomputed by us.
STRIP = {"content-security-policy", "content-security-policy-report-only",
         "x-frame-options", "content-length", "content-encoding",
         "transfer-encoding", "connection", "keep-alive",
         "strict-transport-security"}

INJECT = (
    '<script>window.__CF_CONFIG={{"feedbackUrl":"{p}/feedback",'
    '"historyUrl":"{p}/history.json","markSeenUrl":"{p}/mark-seen",'
    '"target":"{t}"}};</script>'
    '<link rel="stylesheet" href="{p}/lib/feedback.css">'
    '<script src="{p}/lib/feedback.js"></script>'
)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    target = None        # urlsplit() of target URL (set on class)
    feedback_dir = None  # Path (set on class)

    def log_message(self, fmt, *args):
        joined = " ".join(map(str, args))
        if joined.startswith(("POST", "PUT", "DELETE")) or " 4" in joined or " 5" in joined:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # All verbs funnel through one dispatcher.
    def _dispatch(self):
        _touch()
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self._tunnel_ws()
        cf_path = self.path.split("?", 1)[0]
        if cf_path == CF or cf_path.startswith(CF + "/"):
            return self._handle_cf()
        return self._proxy()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _dispatch

    # ---------------- /__cf/* (served locally) ----------------
    def _handle_cf(self):
        sub = self.path[len(CF):].split("?", 1)[0]
        if sub == "/info":
            return self._json(200, {
                "target": self.target.geturl(),
                "feedback_dir": str(self.feedback_dir),
                "lib_dir": str(LIB_DIR),
                "port": self.server.server_address[1],
            })
        if sub.startswith("/lib/"):
            return self._serve_lib(sub[len("/lib/"):])
        if sub == "/history.json":
            return self._serve_file(self.feedback_dir / "history.json", "application/json")
        if sub == "/feedback" and self.command == "POST":
            data = self._read_json()
            if data is None:
                return self._json(400, {"ok": False, "error": "invalid json"})
            data["received_at"] = time.time()
            data["received_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with open(self.feedback_dir / "inbox.jsonl", "a") as f:
                f.write(json.dumps(data) + "\n")
            sys.stdout.write(f"[feedback] batch with {len(data.get('comments', []))} comment(s)\n")
            sys.stdout.flush()
            return self._json(200, {"ok": True})
        if sub == "/mark-seen" and self.command == "POST":
            data = self._read_json() or {}
            (self.feedback_dir / "lastseen.json").write_text(json.dumps(data, indent=2))
            return self._json(200, {"ok": True})
        return self._json(404, {"ok": False, "error": "unknown __cf endpoint"})

    def _read_json(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n) if n else b""
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    def _serve_lib(self, rel):
        try:
            tgt = (LIB_DIR / rel).resolve()
        except Exception:
            return self.send_error(404)
        if not str(tgt).startswith(str(LIB_DIR) + os.sep):
            return self.send_error(403, "forbidden")
        if not tgt.is_file():
            return self.send_error(404)
        mime = mimetypes.guess_type(str(tgt))[0] or "application/octet-stream"
        return self._serve_file(tgt, mime)

    def _serve_file(self, p, mime):
        try:
            body = Path(p).read_bytes()
        except FileNotFoundError:
            body = b"[]" if str(p).endswith(".json") else b""
        return self._raw(200, body, mime)

    # ---------------- response writers ----------------
    def _json(self, status, payload):
        return self._raw(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _raw(self, status, body, mime):
        if mime.startswith(("text/", "application/json", "application/javascript")) and "charset" not in mime:
            mime += "; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _proxy(self):
        t = self.target
        n = int(self.headers.get("Content-Length", "0"))
        req_body = self.rfile.read(n) if n else None
        headers = {k: v for k, v in self.headers.items()}
        headers["Host"] = t.netloc
        headers["Accept-Encoding"] = "identity"   # skip gzip -> simple injection
        for h in ("Connection", "Keep-Alive", "Proxy-Connection", "Content-Length"):
            headers.pop(h, None)
        cls = http.client.HTTPSConnection if t.scheme == "https" else http.client.HTTPConnection
        port = t.port or (443 if t.scheme == "https" else 80)
        try:
            conn = cls(t.hostname, port, timeout=30)
            conn.request(self.command, self.path, body=req_body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except Exception as e:
            return self._raw(502, f"<h1>502 - proxy could not reach {t.geturl()}</h1>"
                                  f"<pre>{e}</pre>".encode("utf-8"), "text/html")
        ctype = resp.getheader("Content-Type", "")
        if "text/html" in ctype.lower():
            raw = self._inject(raw)
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in STRIP:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _inject(self, raw):
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw   # non-utf8 html: leave untouched
        snippet = INJECT.format(p=CF, t=self.target.geturl())
        low = html.lower()
        i = low.rfind("</head>")
        if i == -1:
            i = low.rfind("</body>")
        html = (html + snippet) if i == -1 else (html[:i] + snippet + html[i:])
        return html.encode("utf-8")

    def _tunnel_ws(self):
        t = self.target
        port = t.port or (443 if t.scheme == "https" else 80)
        try:
            up = socket.create_connection((t.hostname, port), timeout=30)
        except OSError:
            return self._raw(502, b"ws upstream unreachable", "text/plain")
        # Replay the original handshake verbatim to the target.
        req = f"{self.command} {self.path} {self.request_version}\r\n"
        for k, v in self.headers.items():
            req += f"{k}: {v}\r\n"
        req += "\r\n"
        up.sendall(req.encode("latin-1"))
        # Splice both directions until either side closes.
        client = self.connection
        client.setblocking(False); up.setblocking(False)
        socks = [client, up]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 60)
                if not r:
                    break
                for s in r:
                    other = up if s is client else client
                    try:
                        data = s.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        return
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except OSError:
                        return
        finally:
            try:
                up.close()
            except OSError:
                pass
        # Returning ends the handler; client socket is closed by the server.


def _watchdog(idle_timeout_s):
    watch_parent = (INITIAL_PPID != 1)
    while True:
        time.sleep(5)
        reason = None
        if watch_parent and os.getppid() == 1:
            reason = "parent process exited"
        elif idle_timeout_s > 0 and _idle_seconds() > idle_timeout_s:
            reason = f"idle for >{idle_timeout_s}s with no clients"
        if reason:
            sys.stdout.write(f"[proxy] {reason}; shutting down\n"); sys.stdout.flush()
            os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target_url", help="e.g. http://localhost:5173 or https://example.com")
    ap.add_argument("--port", type=int, default=5199)
    ap.add_argument("--feedback-dir", required=True)
    ap.add_argument("--idle-timeout", type=int, default=600)
    args = ap.parse_args()

    raw = args.target_url if "://" in args.target_url else "http://" + args.target_url
    target = urlsplit(raw)
    fb = Path(args.feedback_dir).resolve(); fb.mkdir(parents=True, exist_ok=True)
    (fb / "inbox.jsonl").touch(exist_ok=True)
    if not (fb / "history.json").exists():
        (fb / "history.json").write_text("[]")

    Handler.target = target
    Handler.feedback_dir = fb

    class ReuseTCP(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    try:
        srv = ReuseTCP(("", args.port), Handler)
    except OSError as e:
        print(f"[proxy] FATAL: port {args.port} unavailable ({e}).")
        print(f"[proxy]  check: curl -s http://localhost:{args.port}/__cf/info")
        print(f"[proxy]  or:    --port {args.port + 1}")
        sys.exit(1)

    threading.Thread(target=_watchdog, args=(args.idle_timeout,), daemon=True).start()
    print(f"[proxy] instrumenting {target.geturl()}  ->  http://localhost:{args.port}/")
    print(f"[proxy] inbox:   {fb / 'inbox.jsonl'}")
    print(f"[proxy] history: {fb / 'history.json'}")
    print(f"[proxy] info:    http://localhost:{args.port}/__cf/info")
    print(f"[proxy] auto-shutdown: parent-death OR {args.idle_timeout}s idle. Ctrl-C to stop")
    with srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n[proxy] stopping")


if __name__ == "__main__":
    main()
