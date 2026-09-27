#!/usr/bin/env python3
"""M2 HTTP 服务器(零依赖 stdlib 默认;可选 FastAPI 壳)。

启动:python3 -m myteacher.server [--db myteacher.db] [--port 8787]
环境变量:
  MYTEACHER_LLM_BASE_URL / MYTEACHER_LLM_API_KEY / MYTEACHER_LLM_MODEL
  (缺省 → StubLLM,无网可跑)
路由业务全在 api.Api.handle,本文件只是传输壳。
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from myteacher.api import Api
from myteacher.config import bootstrap
from myteacher.llm import build_llm
from myteacher.store import Store

MAX_BODY = 1 << 20


def build_api(db_path: str) -> Api:
    store = Store(db_path)
    bootstrap(store)
    return Api(store, llm=build_llm())


class Handler(BaseHTTPRequestHandler):
    api: Api = None  # 由 run() 注入(类属性)

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        # 内置地图页:GET / 或 /map → web/map.html(学习时浏览器直接看)
        if method == "GET" and parsed.path in ("/", "/map", "/map.html"):
            try:
                page = (Path(__file__).resolve().parent.parent
                        / "web" / "map.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            except OSError:
                pass
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            raw = self.rfile.read(min(length, MAX_BODY))
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = None
        status, payload = self.api.handle(method, parsed.path, query, body)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):  # 静默访问日志
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("MYTEACHER_DB", "myteacher.db"))
    ap.add_argument("--port", type=int, default=int(os.getenv(
        "MYTEACHER_PORT", "8787")))
    args = ap.parse_args()
    Handler.api = build_api(args.db)
    print(f"myteacher listening on http://127.0.0.1:{args.port}"
          f" (db={args.db})")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
