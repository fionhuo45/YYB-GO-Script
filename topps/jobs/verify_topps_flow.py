#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从青龙拉取指定 ref 的完整登录态并跑一遍 Topps 全流程，用于判断第三方是否改了协议/逻辑。

流程：青龙 sidecar (/topps/session) 取会话 -> 临时文件 -> user_info -> 地址 -> 商品列表
-> 商品详情 -> 结算预览(-> 可选创建未支付订单 -> 订单回查)。

用法：
    一键只读验证（默认自动挑 SKU，不创建订单）：
        python jobs/verify_topps_flow.py --ref 1

    指定商品并创建未支付订单：
        python jobs/verify_topps_flow.py --ref 1 --goods-id 144 --item-id 255 --create

    保存基线 / 与基线对比：
        python jobs/verify_topps_flow.py --ref 1 --save-baseline baseline.json
        python jobs/verify_topps_flow.py --ref 1 --baseline baseline.json

    结果落盘：
        python jobs/verify_topps_flow.py --ref 1 --out result.json

退出码：0=协议正常；1=会话获取/校验失败；2=会话过期；3..6=对应流程步骤异常（见 run_cached_flow）。
"""

import argparse
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = APP_ROOT / "lib"
ORDERS_DIR = APP_ROOT / "orders"
for dependency_dir in (LIB_DIR, ORDERS_DIR):
    if str(dependency_dir) not in sys.path:
        sys.path.insert(0, str(dependency_dir))

from topps_client import ToppsClient  # noqa: E402
from topps_v12_cached_order import run_cached_flow  # noqa: E402


DEFAULT_API = "http://106.119.161.181:5800"

# 用于判定“协议是否正常”的关键字段
PROTOCOL_FIELDS = [
    "USER_QUERY_CODE",
    "ADDRESS_COUNT",
    "GOODS_LIST_CODE",
    "DETAIL_CODE",
    "PREVIEW_CODE",
    "PREORDER_TOKEN_PRESENT",
    "COUPON_COUNT",
    "CREATE_CODE",
    "CREATED_ORDER_REF_PRESENT",
    "ORDER_LIST_CODE",
    "CREATED_ORDER_FOUND",
]


def fetch_session(api: str, ref: str, token: str = "") -> dict:
    """从青龙 sidecar 拉取完整登录态。"""
    url = f"{api.rstrip('/')}/topps/session?ref={ref}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"青龙接口 HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:200]}") from exc
    except Exception as exc:
        raise RuntimeError(f"青龙接口请求失败: {exc}") from exc
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"青龙接口返回异常: {payload}")
    return payload["data"]


def pick_sku(client: ToppsClient):
    """自动从首页/商品列表挑一个带 SKU 的商品，返回 (goods_id, item_id)。"""
    home = client.home_index()
    candidates = []
    if isinstance(home, dict):
        data = home.get("data") or {}
        for mod in data.get("recommend_list", []) or data.get("module_list", []):
            if not isinstance(mod, dict):
                continue
            for item in mod.get("list", []):
                if isinstance(item, dict) and item.get("goods_id") and item.get("item_id"):
                    candidates.append((item["goods_id"], item["item_id"]))
    if not candidates:
        goods = client.goods_list(page=1, page_size=20, scene="1006")
        if isinstance(goods, dict) and goods.get("code") in (2000, 1000):
            raise RuntimeError(
                f"登录态异常（goods_list code={goods.get('code')}），请先重新扫码续期"
            )
        items = (goods.get("data") or {}).get("list", []) if isinstance(goods.get("data"), dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("goods_id") and item.get("item_id"):
                candidates.append((item["goods_id"], item["item_id"]))
    if isinstance(home, dict) and home.get("code") in (2000, 1000):
        raise RuntimeError(
            f"登录态异常（home_index code={home.get('code')}），请先重新扫码续期"
        )
    if not candidates:
        raise RuntimeError("自动挑 SKU 失败：首页与商品列表均无带 SKU 的商品")
    return candidates[0]


def judge_protocol(summary: dict, create: bool) -> tuple:
    """判定协议是否正常，返回 (ok, 失败说明列表)。"""
    issues = []
    if summary.get("USER_QUERY_CODE") != 1:
        issues.append(f"user_info code={summary.get('USER_QUERY_CODE')}")
    if (summary.get("ADDRESS_COUNT") or 0) < 1:
        issues.append(f"address count={summary.get('ADDRESS_COUNT')}")
    if summary.get("GOODS_LIST_CODE") != 1:
        issues.append(f"goods_list code={summary.get('GOODS_LIST_CODE')}")
    if summary.get("DETAIL_CODE") != 1:
        issues.append(f"detail code={summary.get('DETAIL_CODE')}")
    if summary.get("PREVIEW_CODE") != 1:
        issues.append(f"preview code={summary.get('PREVIEW_CODE')}")
    if create:
        if summary.get("CREATE_CODE") != 1:
            issues.append(f"create code={summary.get('CREATE_CODE')}")
        if not summary.get("CREATED_ORDER_REF_PRESENT"):
            issues.append("created order ref missing")
        if summary.get("ORDER_LIST_CODE") != 1:
            issues.append(f"order_list code={summary.get('ORDER_LIST_CODE')}")
        if not summary.get("CREATED_ORDER_FOUND"):
            issues.append("created order not found in order list")
    return (len(issues) == 0, issues)


def compare_baseline(baseline: dict, current: dict) -> dict:
    """逐字段对比协议字段，返回差异。"""
    diffs = {}
    old_summary = baseline.get("summary") or {}
    new_summary = current.get("summary") or {}
    for field in PROTOCOL_FIELDS:
        old_v = old_summary.get(field)
        new_v = new_summary.get(field)
        if old_v != new_v:
            diffs[field] = {"baseline": old_v, "current": new_v}
    meta = {
        "baseline_ref": baseline.get("ref"),
        "current_ref": current.get("ref"),
        "baseline_goods": (baseline.get("goods_id"), baseline.get("item_id")),
        "current_goods": (current.get("goods_id"), current.get("item_id")),
    }
    return {"changed": bool(diffs), "diffs": diffs, "meta": meta}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="青龙 Topps 全流程协议验证")
    parser.add_argument("--api", default=DEFAULT_API, help="青龙 sidecar 基础地址")
    parser.add_argument("--ref", default="1", help="账号 ref（默认 1）")
    parser.add_argument("--token", default="", help="青龙接口 token（恢复鉴权后用）")
    parser.add_argument("--session", type=Path, help="直接用本地会话文件，跳过青龙拉取")
    parser.add_argument("--goods-id", type=int, help="商品 goods_id（缺省自动挑选）")
    parser.add_argument("--item-id", type=int, help="商品 item_id（缺省自动挑选）")
    parser.add_argument("--create", action="store_true", help="创建未支付订单并回查")
    parser.add_argument("--out", type=Path, help="结果 JSON 输出路径")
    parser.add_argument("--save-baseline", type=Path, help="把本次结果存为基线")
    parser.add_argument("--baseline", type=Path, help="与基线 JSON 对比")
    args = parser.parse_args(argv)

    started = time.time()
    tmp_session = None
    try:
        if args.session:
            session_path = args.session
        else:
            session = fetch_session(args.api, args.ref, args.token)
            tmp_dir = tempfile.mkdtemp(prefix="topps-verify-")
            tmp_session = Path(tmp_dir) / f"topps-{args.ref}.json"
            tmp_session.write_text(
                json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            session_path = tmp_session

        if args.goods_id is None or args.item_id is None:
            probe = ToppsClient(session_path)
            if probe.is_token_expired():
                print("会话已过期（token expired），请先在 YYB-Go 重新扫码后再验证")
                return 2
            goods_id, item_id = pick_sku(probe)
            print(f"auto-sku: goods_id={goods_id} item_id={item_id}")
        else:
            goods_id, item_id = args.goods_id, args.item_id

        summary, code = run_cached_flow(session_path, goods_id, item_id, args.create)
        ok, issues = judge_protocol(summary, args.create)
        taken_ms = int((time.time() - started) * 1000)
        result = {
            "ref": args.ref,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "session_source": "qinglong-sidecar" if args.session is None else "local-file",
            "goods_id": goods_id,
            "item_id": item_id,
            "create": args.create,
            "summary": summary,
            "protocol_ok": ok,
            "issues": issues,
            "exit_code": code,
            "taken_ms": taken_ms,
        }

        print("===== 全流程结果 =====")
        for k, v in summary.items():
            print(f"  {k} = {v}")
        print(f"protocol_ok = {ok}")
        if issues:
            for issue in issues:
                print(f"  ! {issue}")
        print(f"taken_ms = {taken_ms}")

        if args.save_baseline:
            args.save_baseline.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"基线已保存: {args.save_baseline}")

        if args.baseline:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            diff = compare_baseline(baseline, result)
            print("===== 与基线对比 =====")
            print(f"changed = {diff['changed']}")
            for field, values in diff["diffs"].items():
                print(f"  {field}: baseline={values['baseline']} current={values['current']}")
            result["baseline_compare"] = diff

        if args.out:
            args.out.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"结果已写入: {args.out}")

        return 0 if ok else code
    except RuntimeError as exc:
        print(f"会话获取/校验失败: {exc}")
        return 1
    finally:
        if tmp_session is not None and tmp_session.exists():
            try:
                tmp_session.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
