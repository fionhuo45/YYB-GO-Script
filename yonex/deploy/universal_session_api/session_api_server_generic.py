#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用微信小程序登录态 API。

项目适配器只描述会话目录、续期入口和摘要字段；HTTP 路由统一为：
``/projects``、``/{project}/accounts``、``/{project}/session``、
``/{project}/account``、``POST /{project}/refresh``。
旧的 Topps/wdngm 路径继续保留，便于已有调用方平滑迁移。
"""

from __future__ import annotations

import argparse
import html
import hmac
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


SCRIPTS_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = SCRIPTS_ROOT / "project"
PROJECT_INDEX = PROJECT_ROOT / "index.json"
SAFE_REF = re.compile(r"^[A-Za-z0-9_.-]+$")


def _component(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise RuntimeError(f"版本索引字段非法: {label}")
    if Path(value).name != value or "/" in value or "\\" in value:
        raise RuntimeError(f"版本索引字段不是单一目录名: {label}")
    return value


def _inside(path: Path, root: Path, label: str) -> Path:
    candidate = path.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"版本索引路径越出 {label}: {path}") from exc
    return candidate


def indexed_entry(project: str, entrypoint: str) -> Path:
    """Resolve an active project entrypoint from the local version index."""
    try:
        index = json.loads(PROJECT_INDEX.read_text(encoding="utf-8"))
        if index.get("schema_version") != 1:
            raise RuntimeError("不支持的版本索引格式")
        record = index["projects"][project]
        version = _component(record["current_version"], f"{project}.current_version")
        snapshot = _component(record["current_snapshot"], f"{project}.current_snapshot")
        relative = record["entrypoints"][entrypoint]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"无法从版本索引读取入口 {project}.{entrypoint}: {PROJECT_INDEX}"
        ) from exc
    if not isinstance(relative, str) or not relative.strip():
        raise RuntimeError(f"版本索引入口为空: {project}.{entrypoint}")

    project_root = _inside(PROJECT_ROOT / project, PROJECT_ROOT, f"project/{project}")
    snapshot_root = _inside(
        project_root / "versions" / version / "snapshots" / snapshot,
        project_root,
        f"project/{project}",
    )
    candidate = _inside(snapshot_root / relative, snapshot_root, f"{project} 当前快照")
    if not candidate.is_file():
        raise FileNotFoundError(f"版本索引入口不存在: {candidate}")
    return candidate


def renewal_entry(env_name: str, project: str) -> Path:
    override = os.getenv(env_name, "").strip()
    return Path(override) if override else indexed_entry(project, "login")


@dataclass(frozen=True)
class ProjectSpec:
    name: str
    display_name: str
    session_dir: Path
    token_env: str
    token_file: Path
    renewal: Path
    renewal_log: Path
    fields: tuple[str, ...]
    updated_field: str
    force_bearer_auth: bool = False


def project_specs() -> dict[str, ProjectSpec]:
    return {
        "topps": ProjectSpec(
            "topps", "Topps", Path(os.getenv("TOPPS_SESSION_DIR", "/ql/data/config/topps-sessions")),
            "TOPPS_API_TOKEN", Path("/ql/data/config/topps_api_token.txt"),
            renewal_entry("TOPPS_RENEWAL_ENTRY", "topps"),
            Path("/ql/data/log/topps_api_refresh.log"),
            ("token", "refresh_token", "mall_session", "acw_tc", "openid", "unionid", "mobile", "user_id", "level", "expires_at", "user_agent", "device_token", "crypto_key", "crypto_iv", "crypto_version", "crypto_expires_at"),
            "updated_at",
        ),
        "wdngm": ProjectSpec(
            "wdngm", "wdngm", Path(os.getenv("WDNGM_SESSION_DIR", "/ql/data/config/wdngm-sessions")),
            "WDNGM_API_TOKEN", Path("/ql/data/config/wdngm_api_token.txt"),
            renewal_entry("WDNGM_RENEWAL_ENTRY", "wdngm"),
            Path("/ql/data/log/wdngm_api_refresh.log"),
            ("apiAccessToken", "sessionId", "appId", "ref", "tag", "updatedAt"),
            "updated_at",
        ),
        "ln": ProjectSpec(
            "ln", "李宁", Path(os.getenv("LN_SESSION_DIR", "/ql/data/config/ln-sessions")),
            "LN_API_TOKEN", Path("/ql/data/config/ln_api_token.txt"),
            renewal_entry("LN_RENEWAL_ENTRY", "lining"),
            Path("/ql/data/log/ln_api_refresh.log"),
            ("appId", "saasId", "createdAt", "updatedAt", "loginMode", "channel", "ref", "tag", "authTokenVO", "userCoreVO", "wxOpenid"),
            "updated_at",
        ),
        "yonex": ProjectSpec(
            "yonex", "YONEX", Path(os.getenv("YONEX_SESSION_DIR", "/ql/data/config/yonex-sessions")),
            "YONEX_API_TOKEN", Path("/ql/data/config/yonex_api_token.txt"),
            renewal_entry("YONEX_RENEWAL_ENTRY", "yonex"),
            Path("/ql/data/log/yonex_api_refresh.log"),
            ("token", "oid", "ref", "tag", "app_id", "is_guest", "updated_at"),
            "updated_at",
            True,
        ),
    }


def load_token(spec: ProjectSpec) -> str:
    value = os.getenv(spec.token_env, "").strip()
    if value:
        return value
    if spec.token_file.exists():
        value = spec.token_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    return ""


def _timestamp_seconds(value: object) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().replace(".", "", 1).isdigit()
    ):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return int(number) if number > 0 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            return None
    return None


def masked_summary(spec: ProjectSpec, session: dict, ref: str) -> dict:
    present = sum(1 for field in spec.fields if session.get(field) not in (None, ""))
    updated = _timestamp_seconds(session.get("updatedAt"))
    if updated is None:
        updated = _timestamp_seconds(session.get("updated_at"))
    age = max(0, int(datetime.now(timezone.utc).timestamp() - updated)) if updated else None
    out = {"ref": session.get("ref") or ref, "tag": session.get("tag", "all"),
           "login_fields": f"{present}/{len(spec.fields)}", spec.updated_field: updated,
           "age_seconds": age}
    if spec.name == "topps":
        expires = session.get("expires_at")
        out["user_id"] = session.get("user_id")
        out["level"] = session.get("level")
        out["expires_at"] = int(expires) if isinstance(expires, (int, float)) else None
        out["expired"] = bool(datetime.now(timezone.utc).timestamp() > expires) if isinstance(expires, (int, float)) else None
    elif spec.name == "ln":
        auth = session.get("authTokenVO")
        auth = auth if isinstance(auth, dict) else {}
        expires = _timestamp_seconds(auth.get("expireTime"))
        out["app_id"] = session.get("appId")
        out["channel"] = session.get("channel")
        out["login_mode"] = session.get("loginMode")
        out["expires_at"] = expires
        out["expired"] = bool(datetime.now(timezone.utc).timestamp() > expires) if expires else None
    elif spec.name == "yonex":
        out["app_id"] = session.get("app_id")
        out["authenticated"] = bool(session.get("token")) and not bool(session.get("is_guest", True))
        out["is_guest"] = bool(session.get("is_guest", True))
        out["token_present"] = bool(session.get("token"))
        out["oid_present"] = bool(session.get("oid"))
    else:
        out["app_id"] = session.get("appId")
    return out


class ProjectAPI:
    def __init__(self, spec: ProjectSpec):
        self.spec = spec

    def paths(self) -> list[Path]:
        if not self.spec.session_dir.exists():
            return []
        return sorted(self.spec.session_dir.glob(f"{self.spec.name}-*.json"))

    def session(self, ref: str):
        if not ref:
            return None
        if self.spec.name == "yonex":
            for path in self.paths():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(payload, dict) and str(payload.get("ref") or "") == ref:
                    return payload
            if not SAFE_REF.fullmatch(ref):
                return None
        path = self.spec.session_dir / f"{self.spec.name}-{ref}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def accounts(self, tag: str = "", pageindex: int = 1, pagesize: int = 50) -> dict:
        include_all = tag.lower() == "all"
        items = []
        for path in self.paths():
            if path.name.endswith("-complete.json"):
                continue
            ref = path.name[len(self.spec.name) + 1:-len(".json")]
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                if not tag or include_all:
                    items.append({"ref": ref, "error": "unreadable"})
                continue
            summary = masked_summary(self.spec, session, ref)
            if tag and not include_all and summary.get("tag") != tag:
                continue
            items.append(summary)
        total = len(items)
        pages = (total + pagesize - 1) // pagesize
        start = (pageindex - 1) * pagesize
        return {"tag": tag or None, "totalcount": total,
                "pageindex": pageindex, "pagesize": pagesize, "totalpages": pages,
                "hasnext": pageindex < pages, "accounts": items[start:start + pagesize]}

    def refresh(self, ref: str = "", tag: str = "") -> dict:
        env = os.environ.copy()
        ref, tag = ref.strip(), tag.strip()
        if ref:
            env[f"{self.spec.name.upper()}_REFRESH_REF"] = ref
        if tag:
            env[f"{self.spec.name.upper()}_REFRESH_TAG"] = tag
        self.spec.renewal_log.parent.mkdir(parents=True, exist_ok=True)
        cmd = f"cd {shlex.quote(str(self.spec.renewal.parent.parent))} && nohup python3 {shlex.quote(str(self.spec.renewal))} >> {shlex.quote(str(self.spec.renewal_log))} 2>&1 &"
        subprocess.Popen(["bash", "-lc", cmd], env=env, close_fds=True)
        return {"refresh": "renewal_started", "scope": "ref" if ref else "tag" if tag else "all",
                "ref": ref or None, "tag": tag or None}


class Handler(BaseHTTPRequestHandler):
    server_version = "UniversalSessionAPI/1.0"

    @property
    def apis(self) -> dict[str, ProjectAPI]:
        return self.server.apis  # type: ignore[attr-defined]

    @property
    def no_auth(self) -> bool:
        return self.server.no_auth  # type: ignore[attr-defined]

    def _param(self, name: str) -> str:
        return parse_qs(urlsplit(self.path).query).get(name, [""])[0].strip()

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, project: str = "") -> bool:
        spec = self.apis[project].spec if project in self.apis else None
        force_bearer = bool(spec and spec.force_bearer_auth)
        if self.no_auth and not force_bearer:
            return True
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            supplied = authorization[7:].strip()
        else:
            supplied = "" if force_bearer else self._param("token")
        expected = load_token(spec) if spec else os.getenv("SESSION_API_TOKEN", "").strip()
        return bool(supplied) and bool(expected) and hmac.compare_digest(supplied, expected)

    def _docs(self) -> None:
        host = html.escape(f"http://{self.headers.get('Host', 'localhost:5800')}")
        rows = [("GET", "/projects", "列出已注册项目及接口")]
        for project in self.apis:
            rows += [("GET", f"/{project}/accounts", "账号脱敏列表"), ("GET", f"/{project}/session?ref=账号", "完整登录态"), ("GET", f"/{project}/account?ID=账号", "按 ID 获取登录态"), ("POST", f"/{project}/refresh?ref=账号", "后台触发续期")]
        table = "".join(f"<tr><td><code>{m}</code></td><td><code>{p}</code></td><td>{d}</td></tr>" for m, p, d in rows)
        body = f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>Universal Session API</title><style>body{{font:15px system-ui;max-width:1000px;margin:40px auto}}td{{padding:10px;border-bottom:1px solid #ddd}}code{{background:#eef2f8;padding:2px 5px}}</style><h1>Universal Session API</h1><p>基础地址：<code>{host}</code></p><table><tr><th>方法</th><th>接口</th><th>说明</th></tr>{table}</table></html>".encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _handle(self):
        path = urlsplit(self.path).path.strip("/")
        if path in {"", "docs"}:
            self._docs(); return
        if path == "openapi.json":
            self._json(200, self.server.openapi())  # type: ignore[attr-defined]
            return
        if path == "health":
            if not self._authorized():
                self._json(401, {"code": 401, "msg": "unauthorized", "data": None})
                return
            self._json(200, {"status": "ok", "service": "topps-session-api"})
            return
        if path == "projects":
            if not self._authorized(): self._json(401, {"code": 401, "msg": "unauthorized", "data": None}); return
            self._json(200, {"code": 0, "msg": "success", "data": [{"project": n, "display_name": a.spec.display_name, "base_path": f"/{n}", "endpoints": ["accounts", "session", "account", "refresh"]} for n, a in self.apis.items()]}); return
        parts = path.split("/")
        project = parts[0] if parts else ""
        if project not in self.apis:
            self._json(404, {"code": 404, "msg": "not found", "data": None}); return
        if not self._authorized(project): self._json(401, {"code": 401, "msg": "unauthorized", "data": None}); return
        api = self.apis[project]
        endpoint = parts[1] if len(parts) > 1 else "health"
        if endpoint == "health": self._json(200, {"status": "ok", "service": "universal-session-api", "project": project}); return
        if endpoint == "accounts":
            try:
                page = max(1, min(int(self._param("pageindex") or 1), 1_000_000)); size = max(1, min(int(self._param("pagesize") or 50), 100))
            except ValueError:
                self._json(400, {"code": 400, "msg": "pageindex/pagesize 必须是整数", "data": None}); return
            self._json(200, {"code": 0, "msg": "success", "data": api.accounts(self._param("tag"), page, size)}); return
        if endpoint in {"session", "account"}:
            ref = self._param("ref") or self._param("ID") or self._param("id"); session = api.session(ref)
            if session is None: self._json(404, {"code": 404, "msg": f"session not found: {ref}", "data": None}); return
            self._json(200, {"code": 0, "msg": "success", "data": ({"ref": ref, **session} if endpoint == "account" else session)}); return
        if endpoint == "refresh" and self.command == "POST":
            self._json(202, {"code": 0, "msg": "accepted", "data": api.refresh(self._param("ref"), self._param("tag"))}); return
        self._json(404, {"code": 404, "msg": "not found", "data": None})

    def do_GET(self): self._handle()
    def do_POST(self): self._handle()
    def log_message(self, fmt, *args): print(f"[universal-session-api] {self.command} {self.path.split('?')[0]} -> {args[0] if args else ''}")


def build_openapi(apis: dict[str, ProjectAPI]) -> dict:
    paths = {"/projects": {"get": {"summary": "列出已注册项目及统一接口"}}}
    for name in apis:
        for endpoint, method, summary in (("accounts", "get", "账号脱敏列表"), ("session", "get", "完整登录态"), ("account", "get", "按账号 ID 获取完整登录态"), ("refresh", "post", "后台触发登录续期")):
            paths[f"/{name}/{endpoint}"] = {method: {"summary": summary}}
    paths["/openapi.json"] = {"get": {"summary": "OpenAPI 描述"}}
    return {"openapi": "3.0.3", "info": {"title": "Universal Session API", "version": "2.0.0"}, "paths": paths}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="通用小程序登录态 API")
    parser.add_argument("--host", default="0.0.0.0"); parser.add_argument("--port", type=int, default=5800); parser.add_argument("--no-auth", action="store_true")
    args = parser.parse_args(argv)
    specs = project_specs(); apis = {name: ProjectAPI(spec) for name, spec in specs.items()}
    server = ThreadingHTTPServer((args.host, args.port), Handler); server.apis = apis; server.no_auth = args.no_auth or os.getenv("SESSION_API_NO_AUTH", "") == "1"; server.openapi = lambda: build_openapi(apis)  # type: ignore[attr-defined]
    print(f"[universal-session-api] listening on {args.host}:{args.port} projects={','.join(apis)} auth={'OFF' if server.no_auth else 'ON'}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    return 0


if __name__ == "__main__": raise SystemExit(main())
