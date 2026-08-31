#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""青龙 YONEX 登录态 sidecar。

完整 session 只通过 Bearer 鉴权接口返回；账号列表始终脱敏。
"""

from __future__ import annotations

import argparse
import hmac
import html
import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit


DEFAULT_SESSION_DIR = "/ql/data/config/yonex-sessions"
DEFAULT_TOKEN_FILE = "/ql/data/config/yonex_api_token.txt"
DEFAULT_RENEWAL = "/ql/data/scripts/yonex/yonex_ql_login.py"
DEFAULT_RENEWAL_LOG = "/ql/data/log/yonex_api_refresh.log"
SAFE_REF = re.compile(r"^[A-Za-z0-9_.-]+$")


def load_token(token_file: Path, token_env: str) -> str:
    token = os.getenv(token_env, "").strip()
    if token:
        return token
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    raise RuntimeError(f"未配置访问令牌：请设置 {token_env} 或写入 {token_file}")


def masked_summary(session: dict[str, Any]) -> dict[str, Any]:
    updated_at = session.get("updated_at")
    if isinstance(updated_at, (int, float)):
        age = max(0, int(datetime.now(timezone.utc).timestamp() - updated_at))
    else:
        age = None
    return {
        "ref": session.get("ref"),
        "tag": session.get("tag", "all"),
        "app_id": session.get("app_id"),
        "authenticated": bool(session.get("token"))
        and not bool(session.get("is_guest", True)),
        "is_guest": bool(session.get("is_guest", True)),
        "token_present": bool(session.get("token")),
        "oid_present": bool(session.get("oid")),
        "updated_at": int(updated_at) if isinstance(updated_at, (int, float)) else None,
        "age_seconds": age,
    }


class API:
    def __init__(
        self,
        session_dir: Path,
        token: str,
        renewal: Path,
        renewal_log: Path,
    ):
        self.session_dir = Path(session_dir)
        self.token = token
        self.renewal = Path(renewal)
        self.renewal_log = Path(renewal_log)

    def _paths(self) -> list[Path]:
        return (
            sorted(self.session_dir.glob("yonex-*.json"))
            if self.session_dir.exists()
            else []
        )

    def accounts(
        self, tag: str = "", pageindex: int = 1, pagesize: int = 50
    ) -> dict[str, Any]:
        include_all = tag.lower() == "all"
        output = []
        for path in self._paths():
            ref = path.stem[len("yonex-") :]
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                if not tag or include_all:
                    output.append({"ref": ref, "error": "unreadable"})
                continue
            if not isinstance(session, dict):
                continue
            summary = masked_summary(session)
            summary["ref"] = summary.get("ref") or ref
            if tag and not include_all and summary.get("tag") != tag:
                continue
            output.append(summary)
        total = len(output)
        start = (pageindex - 1) * pagesize
        pages = (total + pagesize - 1) // pagesize
        return {
            "tag": tag or None,
            "totalcount": total,
            "pageindex": pageindex,
            "pagesize": pagesize,
            "totalpages": pages,
            "hasnext": pageindex < pages,
            "accounts": output[start : start + pagesize],
        }

    def session(self, ref: str) -> Optional[dict[str, Any]]:
        ref = str(ref or "").strip()
        if not ref:
            return None
        # 新版文件名带 ref 摘要，按 session 内的原始 ref 精确匹配；不会把
        # 调用方传入的 ref 拼进路径，因此带 /、? 等字符的账号也能安全查找。
        for path in self._paths():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and str(payload.get("ref") or "") == ref:
                return payload
        # 兼容早期仅对安全 ref 直接命名的 yonex-<ref>.json。
        if SAFE_REF.fullmatch(ref):
            path = self.session_dir / f"yonex-{ref}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return payload if isinstance(payload, dict) else None
        return None

    def trigger_refresh(self, ref: str = "", tag: str = "") -> dict[str, Any]:
        env = os.environ.copy()
        if ref.strip():
            env["YONEX_REFRESH_REF"] = ref.strip()
        if tag.strip():
            env["YONEX_REFRESH_TAG"] = tag.strip()
        self.renewal_log.parent.mkdir(parents=True, exist_ok=True)
        command = (
            f"cd {shlex.quote(str(self.renewal.parent))} && "
            f"nohup python3 {shlex.quote(str(self.renewal))} "
            f">> {shlex.quote(str(self.renewal_log))} 2>&1 &"
        )
        subprocess.Popen(["bash", "-lc", command], env=env, close_fds=True)
        return {
            "refresh": "renewal_started",
            "scope": "ref" if ref else "tag" if tag else "all",
            "ref": ref or None,
            "tag": tag or None,
        }

    def openapi(self) -> dict[str, Any]:
        return {
            "openapi": "3.0.3",
            "info": {"title": "YONEX Session API", "version": "1.0.0"},
            "paths": {
                "/health": {"get": {}},
                "/yonex/accounts": {"get": {}},
                "/yonex/session": {"get": {}},
                "/yonex/account": {"get": {}},
                "/yonex/refresh": {"post": {}},
            },
        }

    def docs_html(self, host: str) -> str:
        base = html.escape(f"http://{host}")
        rows = [
            ("GET", "/health", "健康检查"),
            ("GET", "/yonex/accounts", "脱敏账号列表"),
            ("GET", "/yonex/session?ref=1", "获取完整登录态"),
            ("GET", "/yonex/account?ID=1", "按账号 ID 获取登录态"),
            ("POST", "/yonex/refresh?ref=1", "后台触发续期"),
        ]
        table = "".join(
            f"<tr><td>{method}</td><td><code>{path}</code></td><td>{description}</td></tr>"
            for method, path, description in rows
        )
        return (
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<title>YONEX Session API</title>"
            "<style>body{font:15px system-ui;max-width:900px;margin:40px auto}"
            "td{padding:10px;border-bottom:1px solid #ddd}</style>"
            f"<h1>YONEX Session API</h1><p>基础地址：<code>{base}</code></p>"
            "<p>业务接口默认要求 Authorization: Bearer token。</p>"
            f"<table>{table}</table></html>"
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "YonexSessionAPI/1.0"

    @property
    def api(self) -> API:
        return self.server.api  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _param(self, name: str) -> str:
        return parse_qs(urlsplit(self.path).query).get(name, [""])[0].strip()

    def _auth(self) -> bool:
        header = self.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.startswith("Bearer ") else ""
        return bool(supplied) and hmac.compare_digest(supplied, self.api.token)

    def _handle(self) -> None:
        path = urlsplit(self.path).path
        if path == "/docs":
            body = self.api.docs_html(
                self.headers.get("Host", "localhost:5802")
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/openapi.json":
            self._json(200, self.api.openapi())
            return
        if not self._auth():
            self._json(401, {"code": 401, "msg": "unauthorized", "data": None})
            return
        if path == "/health":
            self._json(200, {"status": "ok", "service": "yonex-session-api"})
            return
        if path == "/yonex/accounts":
            try:
                page = max(1, min(int(self._param("pageindex") or 1), 1_000_000))
                size = max(1, min(int(self._param("pagesize") or 50), 100))
            except ValueError:
                self._json(
                    400,
                    {"code": 400, "msg": "pageindex/pagesize 必须是整数", "data": None},
                )
                return
            data = self.api.accounts(self._param("tag"), page, size)
            self._json(200, {"code": 0, "msg": "success", "data": data})
            return
        if path in {"/yonex/session", "/yonex/account"}:
            ref = self._param("ref") or self._param("ID") or self._param("id")
            session = self.api.session(ref)
            if session is None:
                self._json(
                    404,
                    {"code": 404, "msg": f"session not found: {ref}", "data": None},
                )
                return
            if path == "/yonex/account":
                session = {"ref": ref, **session}
            self._json(200, {"code": 0, "msg": "success", "data": session})
            return
        if path == "/yonex/refresh" and self.command == "POST":
            data = self.api.trigger_refresh(self._param("ref"), self._param("tag"))
            self._json(202, {"code": 0, "msg": "accepted", "data": data})
            return
        self._json(404, {"code": 404, "msg": "not found", "data": None})

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, fmt: str, *args: Any) -> None:
        status = args[0] if args else ""
        print(
            f"[yonex-session-api] {self.command} {self.path.split('?')[0]} -> {status}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="YONEX 登录态外部接口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5802)
    parser.add_argument(
        "--session-dir", default=os.getenv("YONEX_SESSION_DIR", DEFAULT_SESSION_DIR)
    )
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--token-env", default="YONEX_API_TOKEN")
    parser.add_argument("--renewal", default=DEFAULT_RENEWAL)
    parser.add_argument("--renewal-log", default=DEFAULT_RENEWAL_LOG)
    args = parser.parse_args(argv)
    token = load_token(Path(args.token_file), args.token_env)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.api = API(  # type: ignore[attr-defined]
        Path(args.session_dir),
        token,
        Path(args.renewal),
        Path(args.renewal_log),
    )
    print(
        f"[yonex-session-api] listening on {args.host}:{args.port} "
        f"session_dir={args.session_dir} auth=ON"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
