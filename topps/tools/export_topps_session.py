#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出/补全 Topps 账号完整登录态数据。

登录态字段全集（16 项）：
    token, refresh_token, mall_session, acw_tc, openid, unionid, mobile,
    user_id, level, expires_at, user_agent, device_token,
    crypto_key, crypto_iv, crypto_version, crypto_expires_at

用法：
    盘点（默认脱敏，不输出任何凭据原文）：
        python tools/export_topps_session.py --session <session.json>

    通过 YYB-Go operateWxData 补全 v12 加密材料（不需要手机号授权码）：
        python tools/export_topps_session.py --session <session.json> \
            --fetch-crypto --yyb-server http://yyb-go:8000@13

    补全 device_token（首次业务调用会惰性生成，这里显式生成并持久化）：
        python tools/export_topps_session.py --session <session.json> \
            --add-device-token

    导出完整 JSON 到文件（含全部敏感字段，注意保管）：
        python tools/export_topps_session.py --session <session.json> --out <out.json>
"""

import argparse
import json
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
for dependency_dir in (APP_ROOT / "lib", APP_ROOT / "jobs"):
    if str(dependency_dir) not in sys.path:
        sys.path.insert(0, str(dependency_dir))

from topps_client import ToppsClient  # noqa: E402
from topps_ql_login import (  # noqa: E402
    get_wechat_crypto_material,
    parse_yyb_servers,
)


FIELDS = [
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


def load_session(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"会话文件不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_session(path: Path, session: dict) -> None:
    path.write_text(
        json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def inventory(session: dict) -> None:
    """只打印字段存在性与长度，不打印凭据原文。"""
    print("=== 登录态字段清单（脱敏） ===")
    for field in FIELDS:
        value = session.get(field)
        if value is None or value == "":
            print(f"  {field}: <absent>")
        elif isinstance(value, (int, float)):
            print(f"  {field}: {value}")
        else:
            print(f"  {field}: <len{len(str(value))}>")
    tag = session.get("tag")
    if tag in (None, ""):
        print("  tag: <默认 all>")
    else:
        print(f"  tag: {tag}")
    extra = sorted(set(session.keys()) - set(FIELDS))
    if extra:
        print("  EXTRA_KEYS:", ", ".join(extra))
    present = sum(
        1
        for f in FIELDS
        if session.get(f) not in (None, "")
    )
    print(f"=== 完整度: {present}/{len(FIELDS)} ===")


def fetch_crypto(session: dict, yyb_server: str) -> dict:
    server, ref = parse_yyb_servers(yyb_server)[0]
    from curl_cffi import requests

    yyb_session = requests.Session()
    crypto = get_wechat_crypto_material(yyb_session, server, ref)
    session.update(
        {
            "crypto_key": crypto["encrypt_key"],
            "crypto_iv": crypto["iv"],
            "crypto_version": int(crypto["version"]),
            "crypto_expires_at": int(crypto["expires_at"]),
        }
    )
    print(f"crypto fetched via YYB-Go ({server}@{ref})")
    return session


def ensure_device_token(path: Path, session: dict) -> dict:
    client = ToppsClient(path)
    client.session = session
    token = client.device_token()
    session["device_token"] = token
    print(f"device_token ensured (<len{len(token)}>)")
    return session


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 Topps 完整登录态")
    parser.add_argument("--session", required=True, help="会话 JSON 文件路径")
    parser.add_argument(
        "--fetch-crypto", action="store_true", help="通过 YYB-Go 补全 v12 加密材料"
    )
    parser.add_argument("--yyb-server", help="YYB_SERVER 单条配置，如 http://host:8000@13")
    parser.add_argument(
        "--add-device-token", action="store_true", help="显式生成并持久化 device_token"
    )
    parser.add_argument(
        "--set-tag", help="设置并持久化供应商 tag（未设置时导出默认 all）"
    )
    parser.add_argument(
        "--out", help="导出完整 JSON 到该路径（含全部敏感字段）"
    )
    args = parser.parse_args()

    path = Path(args.session).expanduser()
    session = load_session(path)
    changed = False

    if args.fetch_crypto:
        if not args.yyb_server:
            print("--fetch-crypto 需要 --yyb-server（格式：地址@ref）")
            return 2
        session = fetch_crypto(session, args.yyb_server)
        changed = True

    if args.add_device_token:
        session = ensure_device_token(path, session)
        changed = True

    if args.set_tag is not None:
        session["tag"] = args.set_tag.strip() or "all"
        changed = True

    if changed:
        save_session(path, session)
        print(f"已写回: {path}")

    inventory(session)

    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        export = dict(session)
        export.setdefault("tag", "all")
        out.write_text(
            json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"完整导出: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
