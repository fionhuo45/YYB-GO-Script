#!/usr/bin/env python3
"""Restore a captured Topps member session and verify the v12 cached-order flow.

The CLI deliberately never calls payment/prepay or payment/reportPayResult and
prints only status values and one-way hashes for credential-bearing fields.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple


APP_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = APP_ROOT / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from topps_client import (  # noqa: E402
    ToppsClient,
    _generate_pre_order_id,
    _solve_slider_captcha,
)


def _decode_jwt(token: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("captured authorization is not a JWT")
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(segment))


def _json_line(stream) -> Dict[str, Any]:
    line = stream.readline()
    if not line:
        raise ValueError("capture record is missing")
    return json.loads(line)


def restore_member_session_from_capture(
    relay_path: Path, request_offset: int, source: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return a full member session only from a proven successful create pair."""
    with relay_path.open("rb") as stream:
        stream.seek(request_offset)
        request = _json_line(stream)
        response = _json_line(stream)

    body = json.loads(request.get("bodyText") or "{}")
    response_body = json.loads(response.get("bodyText") or "{}")
    if request.get("event") != "request" or request.get("url") != "/interface/order/orderBuy":
        raise ValueError("capture offset is not an orderBuy request")
    if body.get("action") != "create":
        raise ValueError("capture offset is not an order create request")
    response_data = response_body.get("data") or {}
    if response_body.get("code") != 1 or not response_data.get("order_id"):
        raise ValueError("capture create response did not succeed")

    headers = request.get("headers") or {}
    authorization = next(
        (value for key, value in headers.items() if str(key).lower() == "authorization"),
        "",
    )
    token = str(authorization).split(" ", 1)[-1].strip()
    device_token = str(body.get("device_token") or "")
    if not token or not device_token:
        raise ValueError("capture is missing authorization or device_token")

    claims = _decode_jwt(token)
    member = claims.get("data") or {}
    captured_openid = member.get("openid")
    source_openid = source.get("openid")
    if source_openid and captured_openid and source_openid != captured_openid:
        raise ValueError("identity mismatch between source session and capture")
    if not member.get("user_id") or not member.get("mobile"):
        raise ValueError("captured JWT is not a full member session")

    restored = dict(source)
    restored.update(
        {
            "token": token,
            "refresh_token": token,
            "openid": captured_openid or source_openid,
            "unionid": member.get("unionid") or source.get("unionid"),
            "mobile": member.get("mobile"),
            "user_id": member.get("user_id"),
            "expires_at": claims.get("exp"),
            "device_token": device_token,
            "user_agent": member.get("user_agent") or source.get("user_agent"),
        }
    )
    proof = {
        "capture_create_succeeded": True,
        "identity_matches": not source_openid or source_openid == captured_openid,
        "member_token_present": True,
        "device_token_present": True,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "device_token_sha256": hashlib.sha256(device_token.encode()).hexdigest(),
        "expires_at": claims.get("exp"),
        "item_ids": [item.get("item_id") for item in body.get("goods", [])],
    }
    return restored, proof


def write_session(path: Path, session: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def build_order_payload(
    *,
    action: str,
    item_id: int,
    address_id: int,
    device_token: str,
    pre_order_id: str,
    preorder_token: str = "",
    coupon_ids: Iterable[int] = (),
) -> Dict[str, Any]:
    """Build the exact attribution shape observed in the successful v12 flow."""
    return {
        "type": "",
        "action": action,
        "coupon_id": list(coupon_ids),
        "goods": [{"item_id": item_id, "num": 1}],
        "address_id": address_id,
        "use_integral": 0,
        "use_coupon": 1,
        "gdt_vid": 0,
        "device_token": device_token,
        "preorder_token": preorder_token,
        "risk_token": "",
        "pre_order_id": pre_order_id,
        "source_type": "商品详情页",
        "utm_source": "share",
        "utm_medium": "organic",
        "utm_term": "20260805",
        "utm_content": "Friends",
        "utm_campaign": "FGC006720-DB",
        "scene": "1006",
    }


def _build_trace_payload(target_x: int, track_max: int) -> str:
    """Build the trace JSON emitted by the v12 risk-slider component."""
    track_max = max(int(track_max or 300), 1)
    final_x = max(0, min(track_max, int(round(target_x))))
    duration = random.randint(650, 950)
    start_at = int(time.time() * 1000) - duration
    point_count = random.randint(24, 32)
    trace = []
    last_x = 0
    for index in range(point_count):
        progress = index / max(point_count - 1, 1)
        eased = 1 - (1 - progress) ** 3
        x = int(round(final_x * eased))
        x = max(last_x, min(final_x, x))
        elapsed = int(round(duration * progress))
        trace.append({"x": x, "y": 0, "t": elapsed})
        last_x = x
    trace[-1]["x"] = final_x
    nonce = f"{int(time.time() * 1000)}_{random.randrange(36**8):08x}"[:22]
    return json.dumps(
        {
            "trace": trace,
            "start_at": start_at,
            "end_at": start_at + duration,
            "final_x": final_x,
            "point_count": len(trace),
            "track_width": track_max,
            "nonce": nonce,
            "client_meta": {"dpr": 3, "platform": "android"},
            "noise": {
                "seed": f"{random.randrange(36**6):06x}"[:6],
                "span": [
                    random.randrange(17),
                    random.randrange(23),
                    random.randrange(31),
                ],
                "ghost": [
                    {"a": nonce[:4], "b": random.randrange(100)},
                    {"a": nonce[-4:], "b": random.randrange(100)},
                ],
            },
        },
        separators=(",", ":"),
    )


def submit_order_with_risk(
    client: ToppsClient,
    payload: Dict[str, Any],
    solve_captcha=_solve_slider_captcha,
) -> Dict[str, Any]:
    """Submit the exact payload and complete the server-requested risk challenge."""
    created = client._request("POST", "/interface/order/orderBuy", payload)
    if (created.get("data") or {}).get("code") != "NEED_RISK_TOKEN":
        return created
    if not payload.get("preorder_token"):
        return created

    challenge = client._request(
        "POST",
        "/interface/risk/challengeCreate",
        {
            "device_token": payload["device_token"],
            "preorder_token": payload["preorder_token"],
            "goods": payload["goods"],
        },
    )
    challenge_data = challenge.get("data") or {}
    if challenge.get("code") != 1 or not challenge_data.get("challenge_token"):
        return {**created, "_risk_step": "challengeCreate"}

    target_x = solve_captcha(
        client.s,
        challenge_data["captcha_bg_url"],
        challenge_data["drag_icon_url"],
    )
    verified = client._request(
        "POST",
        "/interface/risk/challengeVerify",
        {
            "device_token": payload["device_token"],
            "preorder_token": payload["preorder_token"],
            "goods": payload["goods"],
            "challenge_token": challenge_data["challenge_token"],
            "trace_payload": _build_trace_payload(
                target_x, challenge_data.get("track_max", 300)
            ),
        },
    )
    risk_token = (verified.get("data") or {}).get("risk_token")
    if verified.get("code") != 1 or not risk_token:
        return {**created, "_risk_step": "challengeVerify"}

    retry_payload = dict(payload)
    retry_payload["risk_token"] = risk_token
    return client._request("POST", "/interface/order/orderBuy", retry_payload)


def _contains_value(value: Any, expected: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_value(item, expected) for item in value)
    return str(value) == str(expected)


def preview_is_usable(preview: Dict[str, Any]) -> bool:
    """v12 permits create with an empty preorder_token for some product types."""
    return preview.get("code") == 1 and isinstance(preview.get("data"), dict)


def run_cached_flow(
    session_path: Path, goods_id: int, item_id: int, create: bool
) -> Tuple[Dict[str, Any], int]:
    os.environ.pop("YYB_SERVER", None)
    client = ToppsClient(session_path)
    summary: Dict[str, Any] = {
        "CACHE_ONLY_PROCESS": True,
        "YYB_SERVER_USED": False,
        "CACHE_EXPIRED": client.is_token_expired(),
        "PAYMENT_API_CALLED": False,
    }
    if summary["CACHE_EXPIRED"]:
        return summary, 2

    user = client.user_info()
    addresses = client.address_list()
    goods = client.goods_list(page=1, page_size=20, scene="1006")
    detail = client.goods_info(goods_id, item_id, scene="1006")
    summary.update(
        {
            "USER_QUERY_CODE": user.get("code"),
            "ADDRESS_COUNT": len(addresses),
            "GOODS_LIST_CODE": goods.get("code"),
            "DETAIL_CODE": detail.get("code"),
        }
    )
    if user.get("code") != 1 or not addresses or detail.get("code") != 1:
        return summary, 3

    address_id = addresses[0].get("id") or addresses[0].get("address_id")
    device_token = client.device_token()
    pre_order_id = _generate_pre_order_id()
    preview_payload = build_order_payload(
        action="info",
        item_id=item_id,
        address_id=address_id,
        device_token=device_token,
        pre_order_id=pre_order_id,
    )
    preview = client._request("POST", "/interface/order/orderBuy", preview_payload)
    preview_data = preview.get("data") or {}
    preorder_token = preview_data.get("preorder_token") or ""
    coupon_ids = preview_data.get("coupon_id") or []
    summary.update(
        {
            "PREVIEW_CODE": preview.get("code"),
            "PREORDER_TOKEN_PRESENT": bool(preorder_token),
            "COUPON_COUNT": len(coupon_ids),
        }
    )
    if not preview_is_usable(preview):
        return summary, 4
    if not create:
        return summary, 0

    create_payload = build_order_payload(
        action="create",
        item_id=item_id,
        address_id=address_id,
        device_token=device_token,
        pre_order_id=pre_order_id,
        preorder_token=preorder_token,
        coupon_ids=coupon_ids,
    )
    created = submit_order_with_risk(client, create_payload)
    created_data = created.get("data") or {}
    order_id = created_data.get("order_id")
    summary.update(
        {
            "CREATE_CODE": created.get("code"),
            "CREATE_INNER_CODE": created_data.get("code"),
            "CREATED_ORDER_REF_PRESENT": bool(order_id),
        }
    )
    if created.get("code") != 1 or not order_id:
        return summary, 5

    orders = client.order_list(page=1, page_size=20)
    summary.update(
        {
            "ORDER_LIST_CODE": orders.get("code"),
            "CREATED_ORDER_FOUND": _contains_value(orders.get("data"), order_id),
        }
    )
    return summary, 0 if summary["CREATED_ORDER_FOUND"] else 6


def _print_summary(summary: Dict[str, Any]) -> None:
    for key, value in summary.items():
        print(f"{key}={value}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--relay", type=Path, required=True)
    restore.add_argument("--offset", type=int, required=True)
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--session", type=Path, required=True)
    run.add_argument("--goods-id", type=int, required=True)
    run.add_argument("--item-id", type=int, required=True)
    run.add_argument("--create", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "restore":
        source = json.loads(args.source.read_text(encoding="utf-8"))
        session, proof = restore_member_session_from_capture(
            args.relay, args.offset, source
        )
        write_session(args.output, session)
        _print_summary({key.upper(): value for key, value in proof.items()})
        print(f"OUTPUT_SHA256={hashlib.sha256(args.output.read_bytes()).hexdigest()}")
        return 0

    summary, status = run_cached_flow(
        args.session, args.goods_id, args.item_id, args.create
    )
    _print_summary(summary)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
