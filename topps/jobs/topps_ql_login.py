#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""青龙入口：通过 YYB-Go 获取微信登录凭据并刷新 Topps v18 商城登录态。

只执行登录和会员状态验证，不查询商品、不创建订单、不调用支付接口。
订单加密材料属于下单时的临时能力；获取失败不会再丢弃已验证的商城登录态。
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, List, Tuple
from urllib.parse import urlsplit


APP_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = APP_ROOT / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from curl_cffi import requests
from topps_client import APP_ID, ToppsClient


def parse_yyb_servers(raw: str) -> List[Tuple[str, str, str]]:
    """解析多行 ``服务地址@微信账号标识[#tag]`` 配置；未指定 tag 时默认 ``all``。"""
    accounts = []
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
    accounts: List[Tuple[str, str, str]], ref: str = "", tag: str = ""
) -> List[Tuple[str, str, str]]:
    """按可选账号 ref、tag 筛选本次需要续期的账号；两个条件同时提供时取交集。"""
    ref = ref.strip()
    tag = tag.strip()
    return [
        account
        for account in accounts
        if (not ref or account[1] == ref) and (not tag or account[2] == tag)
    ]


def get_wechat_code(session, server: str, ref: str, timeout: int = 20) -> str:
    """从 YYB-Go 获取一次性 wx.login code。"""
    response = session.post(
        f"{server}/wxapp/getCode",
        json={"ref": ref, "app_id": APP_ID},
        timeout=timeout,
        proxies={"http": None, "https": None},
    )
    response.raise_for_status()
    payload = response.json()
    code = ((payload.get("data") or {}).get("result") or {}).get("code")
    if payload.get("code") != 0 or not code:
        raise RuntimeError(payload.get("msg") or "YYB-Go 取码失败")
    return code


def get_wechat_phone_code(session, server: str, ref: str, timeout: int = 20) -> str:
    """从 YYB-Go 获取一次性微信手机号授权 code。"""
    response = session.post(
        f"{server}/wxapp/getPhoneNumber",
        json={"ref": ref, "app_id": APP_ID},
        timeout=timeout,
        proxies={"http": None, "https": None},
    )
    response.raise_for_status()
    payload = response.json()
    result = (payload.get("data") or {}).get("result") or {}
    raw = result.get("data") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if isinstance(raw, dict):
        code = raw.get("code") or result.get("code")
    else:
        code = result.get("code")
    if payload.get("code") != 0 or not code:
        detail = ""
        if isinstance(raw, dict) and raw.get("need_auth") is not None:
            detail = (
                f"（need_auth={raw.get('need_auth')} "
                f"allow_send_sms={raw.get('allow_send_sms')} "
                f"mobile_set={bool(raw.get('mobile'))}）"
            )
        raise RuntimeError(f"{payload.get('msg') or 'YYB-Go 手机号取码失败'}{detail}")
    return code


def get_wechat_crypto_material(
    session, server: str, ref: str, timeout: int = 20
) -> dict:
    """通过 YYB-Go 获取 ``wx.getUserCryptoManager`` 用户加密材料。"""
    response = session.post(
        f"{server}/wxapp/operateWxData",
        json={
            "ref": ref,
            "app_id": APP_ID,
            "payload": {
                "api_name": "webapi_getuserencryptkey",
                "data": {},
            },
        },
        timeout=timeout,
        proxies={"http": None, "https": None},
    )
    response.raise_for_status()
    payload = response.json()
    result = ((payload.get("data") or {}).get("result") or {})
    raw = result.get("data") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("YYB-Go 用户加密材料不是有效 JSON") from exc
    if (
        payload.get("code") != 0
        or result.get("err_no") not in {None, 0}
        or not isinstance(raw, dict)
        or not raw.get("encrypt_key")
        or not raw.get("iv")
        or not raw.get("version")
    ):
        raise RuntimeError("YYB-Go 未返回可用的用户加密材料")
    create_time = int(raw.get("create_time") or 0)
    expire_in = int(raw.get("expire_in") or 0)
    return {
        "encrypt_key": raw["encrypt_key"],
        "iv": raw["iv"],
        "version": int(raw["version"]),
        "expires_at": create_time + expire_in,
    }


def account_session_path(session_dir: Path, ref: str) -> Path:
    """为每个微信账号生成独立的 Topps 会话文件。"""
    safe_ref = re.sub(r"[^A-Za-z0-9_.-]+", "_", ref).strip("._") or "account"
    return session_dir / f"topps-{safe_ref}.json"


def task_exit_code(succeeded: int, failed: int) -> int:
    """任务退出码：全部成功或部分成功返回 0，全部失败返回 1。"""
    return 0 if succeeded > 0 or failed == 0 else 1


def run_account(
    server: str,
    ref: str,
    session_path: Path,
    yyb_session,
    tag: str = "all",
    client_factory: Callable[[Path], ToppsClient] = ToppsClient,
    code_getter: Callable = get_wechat_code,
    phone_code_getter: Callable = get_wechat_phone_code,
    crypto_getter: Callable = get_wechat_crypto_material,
    output: Callable[[str], None] = print,
):
    """刷新单个账号完整商城登录态并验证会员状态；日志不输出凭据。

    先写入临时会话文件，只有 wx.login + 手机号升级 + 轻量鉴权全部成功
    才落盘到正式会话文件，避免手机号授权未就绪时把完整会员缓存降级为
    OpenID 级会话。订单加密材料按最佳努力缓存，不阻塞已验证的登录态。
    tag（供应商标识）在成功落盘时写入会话，默认 ``all``。
    """
    output(f"[{ref}] 正在向 YYB-Go 获取 Topps 微信登录 code")
    code = code_getter(yyb_session, server, ref)
    output(f"[{ref}] YYB-Go 取码成功，正在登录 Topps")

    session_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = session_path.with_suffix(".tmp")
    client = client_factory(tmp_path)
    if session_path.exists():
        try:
            client.session = json.loads(session_path.read_text(encoding="utf-8"))
            client._set_cookies()
        except Exception:
            pass
    try:
        login_result = client.login_with_wechat_code(code)
        output(f"[{ref}] OpenID 登录成功，正在获取手机号授权 code")

        phone_code = phone_code_getter(yyb_session, server, ref)
        output(f"[{ref}] 手机号取码成功，正在升级完整商城登录态")
        full_login_result = client.login_with_phone_code(phone_code)
        output(f"[{ref}] Topps 完整商城登录成功")

        crypto_cached = False
        try:
            crypto = crypto_getter(yyb_session, server, ref)
            client.set_crypto_material(
                encrypt_key=crypto["encrypt_key"],
                iv=crypto["iv"],
                version=crypto["version"],
                expires_at=crypto["expires_at"],
            )
            crypto_cached = True
            output(f"[{ref}] 订单加密材料已缓存")
        except Exception:
            output(f"[{ref}] 订单加密材料暂不可用，继续保存已验证的商城登录态")

        # 新账号没有历史会话可继承时，初始化一次本地弱设备标识；
        # 已有账号继续复用原值，避免每次定时续期都表现为更换设备。
        client.device_token()

        member_result = client.user_info()
        if member_result.get("code") != 1 or not isinstance(member_result.get("data"), dict):
            member_result = client.check_member_by_openid()
        member_data = member_result.get("data") or {}
        level = int(member_data.get("user_level") or member_data.get("level") or 0)
        is_member = level > 0
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    if tmp_path.exists():
        client.session["tag"] = tag
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(client.session, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, session_path)
    output(f"[{ref}] 会员状态：{'已是会员' if is_member else '非会员'}（等级 {level}）")
    output(f"[{ref}] 登录态已保存：{session_path}")
    return {
        "ref": ref,
        "is_member": is_member,
        "level": level,
        "session_path": str(session_path),
        "login_code": login_result.get("code"),
        "full_login_code": full_login_result.get("code"),
        "crypto_cached": crypto_cached,
    }


def main() -> int:
    try:
        accounts = parse_yyb_servers(os.getenv("YYB_SERVER", ""))
    except ValueError as exc:
        print(f"配置错误：{exc}")
        return 2

    refresh_ref = os.getenv("TOPPS_REFRESH_REF", "").strip()
    refresh_tag = os.getenv("TOPPS_REFRESH_TAG", "").strip()
    accounts = select_refresh_accounts(accounts, refresh_ref, refresh_tag)
    if not accounts:
        print(f"未找到匹配账号：ref={refresh_ref or '*'} tag={refresh_tag or '*'}")
        return 2

    session_dir = Path(
        os.getenv("TOPPS_SESSION_DIR", str(APP_ROOT / "captures" / "sessions"))
    ).expanduser()
    yyb_session = requests.Session()
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
            print(f"[{ref}] 登录续期失败：{exc}")
    print(f"SUMMARY: total={len(accounts)} succeeded={succeeded} failed={failed}")
    # 全部成功或部分成功都视为任务成功；只有全部失败才返回失败码。
    return task_exit_code(succeeded, failed)


if __name__ == "__main__":
    raise SystemExit(main())
