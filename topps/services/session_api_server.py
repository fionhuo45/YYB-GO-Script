#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""青龙 Topps 登录态外部接口（方案 B）。

只读 ``TOPPS_SESSION_DIR`` 下的 ``topps-<ref>.json`` 并返回完整登录态；
``POST /topps/refresh`` 触发一次青龙侧登录续期（后台执行，不阻塞响应）。

鉴权（默认开启）：``Authorization: Bearer <TOPPS_API_TOKEN>`` 或 ``?token=``。
token 读取顺序：环境变量 ``TOPPS_API_TOKEN`` -> 文件
``/ql/data/config/topps_api_token.txt``（数据卷，推荐）。
传入 ``--no-auth``（或环境变量 ``TOPPS_API_NO_AUTH=1``）可临时关闭鉴权，
关闭时直接返回登录态；恢复鉴权只需去掉该开关。

接口：
  GET  /health                 -> {"status":"ok","service":"topps-session-api"}
  GET  /topps/accounts         -> 账号清单（ref/tag/完整度/token 过期，脱敏）
  GET  /topps/session?ref=13   -> 完整登录态 JSON（16 字段 + tag）
  GET  /topps/account?ID=13    -> 按账号 ID 获取最新完整登录态 JSON
  POST /topps/refresh?[ref=&tag=] -> 触发全部、单账号或指定 Tag 的登录续期（后台执行）
"""

import argparse
import html
import hmac
import json
import os
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION_DIR = "/ql/data/config/topps-sessions"
DEFAULT_TOKEN_FILE = "/ql/data/config/topps_api_token.txt"
DEFAULT_RENEWAL_ENTRY = os.getenv(
    "TOPPS_RENEWAL_ENTRY", str(APP_ROOT / "jobs" / "topps_ql_login.py")
)
DEFAULT_RENEWAL_LOG = "/ql/data/log/topps_api_refresh.log"

LOGIN_FIELDS = [
    "token",
    "refresh_token",
    "mall_session",
    "acw_tc",
    "openid",
    "unionid",
    "mobile",
    "user_id",
    "level",
    "expires_at",
    "user_agent",
    "device_token",
    "crypto_key",
    "crypto_iv",
    "crypto_version",
    "crypto_expires_at",
]


def load_token(token_file: Path, token_env: str) -> str:
    token = os.getenv(token_env, "").strip()
    if token:
        return token
    if token_file.exists():
        value = token_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise RuntimeError(
        f"未配置访问令牌：请设置环境变量 {token_env} 或写入 {token_file}"
    )


def masked_summary(session: dict) -> dict:
    present = sum(1 for f in LOGIN_FIELDS if session.get(f) not in (None, ""))
    expires_at = session.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at > 0:
        expired = datetime.now(timezone.utc).timestamp() > expires_at
        expires = {
            "expires_at": int(expires_at),
            "expired": bool(expired),
        }
    else:
        expires = {"expires_at": None, "expired": None}
    return {
        "tag": session.get("tag", "all"),
        "user_id": session.get("user_id"),
        "level": session.get("level"),
        "login_fields": f"{present}/{len(LOGIN_FIELDS)}",
        **expires,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ToppsSessionAPI/1.0"

    @property
    def api(self):
        return self.server.api  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict) -> None:
        self._raw_json(status, payload)

    def _raw_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _unauthorized(self) -> None:
        self._json(401, {"code": 401, "msg": "unauthorized", "data": None})

    def _authorized(self) -> bool:
        if self.api.no_auth:
            return True
        header = self.headers.get("Authorization", "")
        token_param = self._query_param("token")
        supplied = ""
        if header.startswith("Bearer "):
            supplied = header[len("Bearer "):].strip()
        elif token_param:
            supplied = token_param.strip()
        return bool(supplied) and hmac.compare_digest(supplied, self.api.token)

    def log_message(self, fmt: str, *args) -> None:
        # 不打印 token；只记录路径与状态
        print(
            f"[session-api] {datetime.now().isoformat(timespec='seconds')} "
            f"{self.command} {self.path.split('?')[0]} -> {args[0] if args else ''}"
        )

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/docs":
            self._html(200, self.api.docs_html(self.headers.get("Host", "localhost:5800")))
            return
        if path == "/openapi.json":
            self._raw_json(200, self.api.openapi())
            return
        if not self._authorized():
            self._unauthorized()
            return
        if path == "/health":
            self._json(200, {"status": "ok", "service": "topps-session-api"})
            return
        if path == "/topps/accounts":
            try:
                pageindex = self._positive_int("pageindex", 1, 1_000_000)
                pagesize = self._positive_int("pagesize", 50, 100)
            except ValueError as error:
                self._json(400, {"code": 400, "msg": str(error), "data": None})
                return
            self._json(200, {
                "code": 0,
                "msg": "success",
                "data": self.api.accounts(self._query_param("tag"), pageindex, pagesize),
            })
            return
        if path == "/topps/session":
            ref = self._query_param("ref")
            session = self.api.session(ref)
            if session is None:
                self._json(404, {"code": 404, "msg": f"session not found: {ref}", "data": None})
                return
            self._json(200, {"code": 0, "msg": "success", "data": session})
            return
        if path == "/topps/account":
            ref = self._query_param("ID") or self._query_param("id") or self._query_param("ref")
            session = self.api.session(ref)
            if session is None:
                self._json(404, {"code": 404, "msg": f"session not found: {ref}", "data": None})
                return
            session = {"ref": ref, **session}
            self._json(200, {"code": 0, "msg": "success", "data": session})
            return
        if path == "/topps/refresh" and self.command == "POST":
            accepted = self.api.trigger_refresh(self._query_param("ref"), self._query_param("tag"))
            self._json(202, {"code": 0, "msg": "accepted", "data": accepted})
            return
        self._json(404, {"code": 404, "msg": "not found", "data": None})

    def _query_param(self, name: str) -> str:
        return parse_qs(urlsplit(self.path).query).get(name, [""])[0].strip()

    def _positive_int(self, name: str, default: int, maximum: int) -> int:
        raw = self._query_param(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as error:
            raise ValueError(f"{name} 必须是 1 到 {maximum} 的整数") from error
        if value < 1 or value > maximum:
            raise ValueError(f"{name} 必须是 1 到 {maximum} 的整数")
        return value

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()


class API:
    def __init__(
        self,
        session_dir: Path,
        token: str,
        renewal: Path,
        renewal_log: Path,
        no_auth: bool = False,
    ):
        self.session_dir = session_dir
        self.token = token
        self.renewal = renewal
        self.renewal_log = renewal_log
        self.no_auth = no_auth

    def _session_paths(self):
        if not self.session_dir.exists():
            return []
        return sorted(self.session_dir.glob("topps-*.json"))

    def accounts(self, tag: str = "", pageindex: int = 1, pagesize: int = 50) -> dict:
        tag = tag.strip()
        include_all_tags = tag.lower() == "all"
        out = []
        for path in self._session_paths():
            if path.name.endswith("-complete.json"):
                continue
            ref = path.name[len("topps-"):-len(".json")]
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                if not tag or include_all_tags:
                    out.append({"ref": ref, "error": "unreadable"})
                continue
            summary = {"ref": ref, **masked_summary(session)}
            if tag and not include_all_tags and summary["tag"] != tag:
                continue
            out.append(summary)
        totalcount = len(out)
        start = (pageindex - 1) * pagesize
        totalpages = (totalcount + pagesize - 1) // pagesize
        return {
            "tag": tag or None,
            "totalcount": totalcount,
            "pageindex": pageindex,
            "pagesize": pagesize,
            "totalpages": totalpages,
            "hasnext": pageindex < totalpages,
            "accounts": out[start:start + pagesize],
        }

    def session(self, ref: str):
        if not ref:
            return None
        path = self.session_dir / f"topps-{ref}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def trigger_refresh(self, ref: str = "", tag: str = "") -> dict:
        ref = ref.strip()
        tag = tag.strip()
        environment = os.environ.copy()
        if ref:
            environment["TOPPS_REFRESH_REF"] = ref
        if tag:
            environment["TOPPS_REFRESH_TAG"] = tag
        cmd = (
            "bash -c 'source /ql/shell/preload/env.sh 2>/dev/null; "
            f"cd {self.renewal.parent.parent} && "
            f"nohup python3 {self.renewal} >> {self.renewal_log} 2>&1 &'"
        )
        subprocess.Popen(cmd, shell=True, env=environment)
        return {
            "refresh": "renewal_started",
            "scope": "ref" if ref else "tag" if tag else "all",
            "ref": ref or None,
            "tag": tag or None,
        }

    def openapi(self) -> dict:
        return {
            "openapi": "3.0.3",
            "info": {"title": "Topps Session API", "version": "1.1.0"},
            "paths": {
                "/health": {"get": {"summary": "服务健康检查"}},
                "/topps/accounts": {"get": {"summary": "按 tag 分页获取账号脱敏列表"}},
                "/topps/session": {"get": {"summary": "按 ref 获取完整登录态"}},
                "/topps/account": {"get": {"summary": "按 ID 获取最新完整登录态"}},
                "/topps/refresh": {"post": {"summary": "后台触发全部、指定 ref 或指定 tag 的登录续期"}},
            },
        }

    def docs_html(self, host: str) -> str:
        base = html.escape(f"http://{host}")
        rows = [
            ("GET", "/health", "服务健康检查"),
            ("GET", "/topps/accounts?tag=lf&pageindex=1&pagesize=50", "按 Tag 分页获取账号脱敏摘要；data.totalcount 为匹配账号总数"),
            ("GET", "/topps/session?ref=13", "按账号 ref 获取完整登录态"),
            ("GET", "/topps/account?ID=13", "按账号 ID 获取最新完整登录态"),
            ("POST", "/topps/refresh?ref=13 或 /topps/refresh?tag=lf", "后台触发单账号或指定 Tag 的登录续期；不传参数时续期全部账号"),
            ("GET", "/openapi.json", "OpenAPI 接口描述"),
        ]
        table_rows = []
        for method, path, description in rows:
            endpoint = f'<a href="{base}{path}"><code>{path}</code></a>' if method == "GET" else f"<code>{path}</code>"
            table_rows.append(f"<tr><td><code>{method}</code></td><td>{endpoint}</td><td>{description}</td></tr>")
        table = "".join(table_rows)
        return f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Topps Session API</title><style>body{{margin:0;background:#f6f8fb;color:#172033;font:15px/1.6 system-ui,-apple-system,'Microsoft YaHei',sans-serif}}main{{max-width:900px;margin:48px auto;padding:0 24px}}section{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:28px;box-shadow:0 12px 32px #17203312}}h1{{margin:0 0 8px}}p{{color:#64748b}}table{{width:100%;border-collapse:collapse;margin-top:20px}}td,th{{padding:12px 10px;border-bottom:1px solid #edf1f6;text-align:left}}code{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;background:#eef2f8;padding:2px 5px;border-radius:4px}}a{{color:#2563eb;text-decoration:none}}</style></head><body><main><section><h1>Topps Session API</h1><p>基础地址：<code>{base}</code></p><p>完整登录态接口在启用鉴权时需要 <code>Authorization: Bearer &lt;token&gt;</code> 或 <code>?token=</code>。</p><table><thead><tr><th>方法</th><th>接口</th><th>说明</th></tr></thead><tbody>{table}</tbody></table></section></main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Topps 登录态外部接口")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5800)
    parser.add_argument("--session-dir", default=os.getenv("TOPPS_SESSION_DIR", DEFAULT_SESSION_DIR))
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--token-env", default="TOPPS_API_TOKEN")
    parser.add_argument("--renewal", default=DEFAULT_RENEWAL_ENTRY)
    parser.add_argument("--renewal-log", default=DEFAULT_RENEWAL_LOG)
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="临时关闭鉴权（等效环境变量 TOPPS_API_NO_AUTH=1）",
    )
    args = parser.parse_args()

    no_auth = args.no_auth or os.getenv("TOPPS_API_NO_AUTH", "").strip() == "1"
    token = "" if no_auth else load_token(Path(args.token_file), args.token_env)
    api = API(
        session_dir=Path(args.session_dir),
        token=token,
        renewal=Path(args.renewal),
        renewal_log=Path(args.renewal_log),
        no_auth=no_auth,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.api = api  # type: ignore[attr-defined]
    print(
        f"[session-api] listening on {args.host}:{args.port} "
        f"session_dir={args.session_dir} "
        f"(auth={'OFF' if no_auth else 'ON: ' + (args.token_file or args.token_env)})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
