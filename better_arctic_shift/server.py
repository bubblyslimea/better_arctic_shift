"""a small read-only API for saved threads"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .model import fullname, thread_id
from .store import Store
from .transfer import scrapbook


def handler(database):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get('Host') not in allowed_hosts or self.headers.get('Origin') not in {None, *(f'http://{host}' for host in allowed_hosts)}:
                self.send_error(403, "this API only accepts local requests")
                return
            store = Store(database)
            try:
                url = urlparse(self.path)
                parts = url.path.strip("/").split("/")
                query = parse_qs(url.query)
                if parts == ["health"]:
                    result = {"status": "ok"}
                elif parts == ["threads"]:
                    result = [row[0] for row in store.db.execute("SELECT DISTINCT thread FROM observations ORDER BY thread")]
                elif len(parts) >= 2 and parts[0] == "threads":
                    tid = thread_id(parts[1])
                    if not store.entities(tid):
                        self.send_error(404)
                        return
                    if len(parts) == 2:
                        result = scrapbook(store, tid)
                    elif parts[2:] == ["search"]:
                        result = store.search(tid, query.get("q", [""])[0], query.get("removed", ["0"])[0] == "1")
                    elif len(parts) == 4 and parts[2] == "history":
                        result = store.entities(tid).get(fullname(parts[3]))
                        if result is None:
                            self.send_error(404)
                            return
                    else:
                        self.send_error(404)
                        return
                else:
                    self.send_error(404)
                    return
                body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, KeyError, TypeError):
                self.send_error(400, "couldn't read that request")
            finally:
                store.close()

        def log_message(self, *_args):
            pass
    return Handler


def serve(database, port=8765):
    with ThreadingHTTPServer(("127.0.0.1", port), handler(database)) as server:
        print(f"thread history is ready at http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
