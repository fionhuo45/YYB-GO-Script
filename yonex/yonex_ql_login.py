#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""青龙入口：通过 YYB-Go 原子刷新 YONEX 小程序会员登录态。

配置格式：``YYB_SERVER=http://yyb-go:8000@微信账号ref[#tag]``。
脚本只执行登录与轻量验证，不查询商品、不创建订单、不获取支付参数。
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, List, Mapping, Tuple
from urllib.parse import urlsplit

import requests


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from yonex_client import APP_ID, YonexAPIError, YonexClient  # noqa: E402


class AccountExpiredError(RuntimeError):
    """YYB-Go 明确判定微信账号登录缓冲已失效。"""


def safe_error_text(error: BaseException, limit: int = 240) -> str:
    """仅按异常类型输出固定诊断，不复制不可信上游正文或 URL。"""
    if isinstance(error, AccountExpiredError):
        text = "微信账号登录态已失效，请重新扫码"
    elif isinstance(error, YonexAPIError):
        raw_code = str(error.code)
        safe_code = raw_code if raw_code.isdigit() and len(raw_code) <= 6 else "error"
        text = f"YONEX API {error.path} 失败（code={safe_code}）"
    elif isinstance(error, requests.RequestException):
        text = f"YYB-Go 网络请求失败（{type(error).__name__}）"
    else:
        text = type(error).__name__
    return text[:limit]


def parse_yyb_servers(raw: str) -> List[Tuple[str, str, str]]:
    """解析多行 ``地址@微信账号标识[#tag]`` 配置。"""
    accounts: List[Tuple[str, str, str]] = []
    for line in raw.splitlines():
        entry = line.strip()
        if not entry:
            continue
        if "@" not in entry:
            raise ValueError("YYB_SERVER 格式必须为：地址@微信账号标识[#tag]")
        server_ref, _, tag = entry.partition("#")
        server, ref = server_ref.rsplit("@", 1)
        server = server.strip().rstrip("/")
        ref = ref.strip()
        tag = tag.strip() or "all"
        parsed = urlsplit(server)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not ref:
            raise ValueError("YYB_SERVER 格式必须为：地址@微信账号标识[#tag]")
        accounts.append((server, ref, tag))
    if not accounts:
        raise ValueError("未配置 YYB_SERVER（格式：地址@微信账号标识[#tag]）")
    return accounts


def select_refresh_accounts(
    accounts: Iterable[Tuple[str, str, str]], ref: str = "", tag: str = ""
) -> List[Tuple[str, str, str]]:
    ref, tag = ref.strip(), tag.strip()
    return [
        account
        for account in accounts
        if (not ref or account[1] == ref) and (not tag or account[2] == tag)
    ]


def account_session_path(session_dir: Path, ref: str) -> Path:
    safe_ref = re.sub(r"[^A-Za-z0-9_.-]+", "_", ref).strip("._") or "account"
    digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:12]
    return session_dir / f"yonex-{safe_ref}-{digest}.json"


def _yyb_json(
    session: Any, url: str, body: dict[str, Any], timeout: int
) -> dict[str, Any]:
    response = session.post(
        url,
        json=body,
        timeout=timeout,
        proxies={"http": None, "https": None},
    )
    if getattr(response, "status_code", None) == 409:
        try:
            payload = response.json()
            detail = payload.get("msg") if isinstance(payload, Mapping) else None
        except ValueError:
            detail = None
        raise AccountExpiredError(
            detail or "YYB-Go 账号登录缓冲已过期，请在 YYB-Go 管理台重新扫码绑定"
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("YYB-Go 返回格式异常")
    return payload


def get_wechat_code(session: Any, server: str, ref: str, timeout: int = 20) -> str:
    payload = _yyb_json(
        session,
        f"{server}/wxapp/getCode",
        {"ref": ref, "app_id": APP_ID},
        timeout,
    )
    result = (payload.get("data") or {}).get("result") or {}
    code = result.get("code") if isinstance(result, Mapping) else None
    if payload.get("code") not in (None, 0) or not code:
        raise RuntimeError(payload.get("msg") or "YYB-Go 获取 wx.login code 失败")
    return str(code)


def _phone_material(payload: Mapping[str, Any]) -> dict[str, str]:
    result = (payload.get("data") or {}).get("result") or {}
    if not isinstance(result, Mapping):
        result = {}
    raw: Any = result.get("data") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if not isinstance(raw, Mapping):
        raw = {}
    code = raw.get("code") or result.get("code")
    encrypted_data = (
        raw.get("encryptedData")
        or raw.get("encrypted_data")
        or result.get("encryptedData")
        or result.get("encrypted_data")
    )
    iv = raw.get("iv") or result.get("iv")
    if not encrypted_data or not iv:
        raise RuntimeError("YYB-Go 手机号授权数据缺少 encryptedData/iv")
    material = {"encryptedData": str(encrypted_data), "iv": str(iv)}
    # 新版 getPhoneNumber 事件可能附带 detail.code，但 YONEX v29 不使用它；
    # 保留只为诊断兼容，run_account 会用全新的 wx.login code 覆盖。
    if code:
        material["code"] = str(code)
    return material


def get_wechat_phone_auth(
    session: Any, server: str, ref: str, timeout: int = 20
) -> dict[str, str]:
    """获取 YONEX v29 所需的手机号授权三元组。"""
    errors: list[str] = []
    for endpoint in (
        "/wxapp/getPhoneNumber",
        "/wxapp/getPhonenumber",
        "/wx/getphonenumber",
    ):
        try:
            payload = _yyb_json(
                session,
                f"{server}{endpoint}",
                {"ref": ref, "app_id": APP_ID},
                timeout,
            )
            if payload.get("code") not in (None, 0):
                raise RuntimeError(payload.get("msg") or "手机号授权失败")
            return _phone_material(payload)
        except AccountExpiredError:
            raise
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError(errors[-1] if errors else "YYB-Go 获取手机号授权数据失败")


def run_account(
    server: str,
    ref: str,
    session_path: Path,
    yyb_session: Any,
    *,
    tag: str = "all",
    client_factory: Callable[[Path], YonexClient] = YonexClient,
    code_getter: Callable[..., str] = get_wechat_code,
    phone_auth_getter: Callable[..., dict[str, str]] = get_wechat_phone_auth,
    output: Callable[[str], None] = print,
) -> dict[str, Any]:
    """完整刷新单个账号；全部步骤成功后才替换正式 session。"""
    session_path = Path(session_path)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path = session_path.with_suffix(session_path.suffix + ".pending")
    pending_tmp = pending_path.with_suffix(".tmp")
    for path in (pending_path, pending_tmp):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    client = client_factory(pending_path)
    # YonexClient 的通用登录方法默认会在每一步保存状态；青龙续期必须把
    # guest/手机号中间态只保留在内存中，轻量鉴权验证成功后才允许落盘。
    if hasattr(client, "session_path"):
        client.session_path = None
    try:
        output(f"[{ref}] 正在获取首个 YONEX wx.login code")
        first_code = code_getter(yyb_session, server, ref)
        client.login_with_wechat_code(first_code)
        output(f"[{ref}] OpenID 登录成功，正在获取手机号授权数据")

        phone_auth = phone_auth_getter(yyb_session, server, ref)
        # v29 组件不会把 getPhoneNumber.detail.code 传给商城；它会再次
        # wx.login，并把这个全新的登录 code 与 encryptedData/iv 一起提交。
        phone_login_code = code_getter(yyb_session, server, ref)
        client.login_with_phone_code(
            {
                "code": phone_login_code,
                "encryptedData": phone_auth["encryptedData"],
                "iv": phone_auth["iv"],
            }
        )
        output(f"[{ref}] 手机号登录成功，正在刷新完整会员资料")

        refresh_code = code_getter(yyb_session, server, ref)
        client.login_with_wechat_code(refresh_code)
        if not client.state.get("token") or client.state.get("is_guest"):
            raise RuntimeError("YONEX 未返回完整会员登录态")

        verification = client.verify_session()
        if not verification.get("authenticated"):
            raise RuntimeError("YONEX 登录态验证失败")

        client.state.update({"ref": ref, "tag": tag, "app_id": APP_ID})
        if hasattr(client, "session_path"):
            client.session_path = pending_path
        client.save_session()
        if not pending_path.exists():
            raise RuntimeError("YONEX 临时 session 未生成")
        os.replace(pending_path, session_path)
    except Exception:
        for path in (pending_path, pending_tmp):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    output(f"[{ref}] 登录态已验证并原子保存：{session_path}")
    return {
        "ref": ref,
        "tag": tag,
        "authenticated": True,
        "is_guest": False,
        "session_path": str(session_path),
    }


def task_exit_code(succeeded: int, failed: int) -> int:
    return 1 if failed > 0 else 0


def main() -> int:
    try:
        accounts = parse_yyb_servers(os.getenv("YYB_SERVER", ""))
    except ValueError as exc:
        print(f"配置错误：{exc}")
        return 2

    accounts = select_refresh_accounts(
        accounts,
        os.getenv("YONEX_REFRESH_REF", ""),
        os.getenv("YONEX_REFRESH_TAG", ""),
    )
    if not accounts:
        print("未找到匹配的 YONEX 续期账号")
        return 2

    session_dir = Path(
        os.getenv("YONEX_SESSION_DIR", "/ql/data/config/yonex-sessions")
    ).expanduser()
    yyb_session = requests.Session()
    yyb_session.trust_env = False
    succeeded = 0
    failed = 0
    for server, ref, tag in accounts:
        try:
            run_account(
                server,
                ref,
                account_session_path(session_dir, ref),
                yyb_session,
                tag=tag,
            )
            succeeded += 1
        except Exception as exc:
            failed += 1
            print(f"[{ref}] 登录续期失败：{safe_error_text(exc)}")
    print(f"SUMMARY: total={len(accounts)} succeeded={succeeded} failed={failed}")
    return task_exit_code(succeeded, failed)


if __name__ == "__main__":
    raise SystemExit(main())
