import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("ffmon.server")

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def make_server(api, web_dir: Path, host, port, status_extra):
    web_dir = Path(web_dir).resolve()
    routes = {
        "/api/overview": api.overview,
        "/api/tabs": api.tabs,
        "/api/tab": api.tab,
        "/api/processes": api.processes,
        "/api/extensions": api.extensions,
        "/api/userscripts": api.userscripts,
        "/api/events": api.events,
        "/api/status": lambda q: api.status(q, status_extra()),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in routes:
                q = {k: v[-1] for k, v in parse_qs(url.query).items()}
                try:
                    body = json.dumps(routes[url.path](q), separators=(",", ":")).encode()
                except Exception as e:
                    log.exception("api error")
                    self._send(500, "application/json", json.dumps({"error": str(e)}).encode())
                    return
                self._send(200, "application/json", body)
                return
            rel = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            path = (web_dir / rel).resolve()
            if web_dir not in path.parents and path != web_dir or not path.is_file():
                self._send(404, "text/plain", b"not found")
                return
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self._send(200, ctype, path.read_bytes())

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)
