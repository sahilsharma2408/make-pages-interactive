import http.client, json, socket, socketserver, threading, tempfile, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import proxy  # module under test (run from lib/)

def free_port():
    s = socket.socket(); s.bind(("", 0)); p = s.getsockname()[1]; s.close(); return p

class Upstream(BaseHTTPRequestHandler):
    """Configurable fake target."""
    def log_message(self, *a): pass
    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.send_response(101); self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade"); self.end_headers()
            try:
                while True:
                    d = self.connection.recv(1024)
                    if not d: break
                    self.connection.sendall(d)   # echo
            except OSError: pass
            return
        if self.path == "/json":
            b = b'{"ok":1}'; ct = "application/json"
        else:
            b = b"<html><head><title>x</title></head><body>hi</body></html>"; ct = "text/html"
        self.send_response(200); self.send_header("Content-Type", ct)
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

def start_upstream():
    port = free_port()
    srv = ThreadingHTTPServer(("", port), Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port

def start_proxy(target_port):
    fb = Path(tempfile.mkdtemp()) / "feedback"; fb.mkdir(parents=True)
    (fb / "inbox.jsonl").touch(); (fb / "history.json").write_text("[]")
    proxy.Handler.target = urlsplit(f"http://localhost:{target_port}")
    proxy.Handler.feedback_dir = fb
    port = free_port()
    class S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True; daemon_threads = True
    srv = S(("", port), proxy.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port, fb

def get(port, path):
    c = http.client.HTTPConnection("localhost", port, timeout=5)
    c.request("GET", path); r = c.getresponse(); body = r.read(); c.close()
    return r.status, dict(r.getheaders()), body

def post(port, path, obj):
    c = http.client.HTTPConnection("localhost", port, timeout=5)
    c.request("POST", path, body=json.dumps(obj), headers={"Content-Type": "application/json"})
    r = c.getresponse(); body = r.read(); c.close()
    return r.status, body

def test_info():
    up, uport = start_upstream(); _, pport, _ = start_proxy(uport)
    st, _, body = get(pport, "/__cf/info"); j = json.loads(body)
    assert st == 200 and str(uport) in j["target"], j
    print("PASS test_info")

def test_lib():
    up, uport = start_upstream(); _, pport, _ = start_proxy(uport)
    st, _, body = get(pport, "/__cf/lib/feedback.js")
    assert st == 200 and b"__claudeFeedbackInit" in body
    print("PASS test_lib")

def test_history():
    up, uport = start_upstream(); _, pport, _ = start_proxy(uport)
    st, _, body = get(pport, "/__cf/history.json")
    assert st == 200 and body.strip() == b"[]"
    print("PASS test_history")

def test_feedback_post():
    up, uport = start_upstream(); _, pport, fb = start_proxy(uport)
    st, _ = post(pport, "/__cf/feedback", {"comments": [{"cf_id": "c1"}]})
    assert st == 200
    lines = (fb / "inbox.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["comments"][0]["cf_id"] == "c1"
    print("PASS test_feedback_post")

def test_proxy_passthrough_json():
    up, uport = start_upstream(); _, pport, _ = start_proxy(uport)
    st, headers, body = get(pport, "/json")
    assert st == 200 and json.loads(body) == {"ok": 1}, (st, body)
    print("PASS test_proxy_passthrough_json")

def test_inject_and_csp():
    up, uport = start_upstream(); _, pport, _ = start_proxy(uport)
    st, headers, body = get(pport, "/")
    assert st == 200
    assert b"__CF_CONFIG" in body, "config not injected"
    assert b"/__cf/lib/feedback.js" in body, "script not injected"
    assert b"</head>" in body and body.index(b"__CF_CONFIG") < body.index(b"</head>"), "must inject before </head>"
    lower = {k.lower(): v for k, v in headers.items()}
    assert "content-security-policy" not in lower, "CSP should be stripped"
    assert "x-frame-options" not in lower, "X-Frame-Options should be stripped"
    print("PASS test_inject_and_csp")

def test_ws_tunnel():
    up, uport = start_upstream(); _, pport, _ = start_proxy(uport)
    s = socket.create_connection(("localhost", pport), timeout=5)
    req = (
        "GET /socket HTTP/1.1\r\nHost: localhost\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: x\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(req.encode())
    time.sleep(0.2)
    head = s.recv(1024)
    assert b"101" in head, head
    s.sendall(b"ping")
    time.sleep(0.2)
    echo = s.recv(1024)
    assert b"ping" in echo, echo
    s.close()
    print("PASS test_ws_tunnel")

def test_502_when_target_down():
    dead = free_port()  # nothing listening here
    _, pport, _ = start_proxy(dead)
    st, _, body = get(pport, "/")
    assert st == 502 and b"could not reach" in body, (st, body)
    print("PASS test_502_when_target_down")

if __name__ == "__main__":
    test_info(); test_lib(); test_history(); test_feedback_post()
    test_proxy_passthrough_json()
    test_inject_and_csp()
    test_ws_tunnel()
    test_502_when_target_down()
    print("ALL PASS (task 6)")
