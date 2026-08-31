#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YONEX 商品/购物车/结算命令行入口。

无参数时只显示本地登录态摘要。创建待支付订单需要命令行双开关和交互确认；
获取支付参数是另一个显式操作。本脚本不实现也不调用实际支付。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from yonex_client import YonexAPIError, YonexClient  # noqa: E402


SENSITIVE_KEYS = {
    "token",
    "authorization",
    "encrypteddata",
    "iv",
    "paysign",
    "noncestr",
    "packagevalue",
    "receivername",
    "receiverphone",
    "receiverpostcode",
    "receiverprovince",
    "receivercity",
    "receiverregion",
    "receiverdetailaddress",
    "phonenumber",
    "detailaddress",
    "mobile",
    "postcode",
}


def _normalized_key(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def redact_sensitive(value: Any) -> Any:
    """递归隐藏登录、收件与微信支付签名字段。"""
    if isinstance(value, Mapping):
        output = {}
        for key, item in value.items():
            output[key] = (
                "<redacted>"
                if _normalized_key(key) in SENSITIVE_KEYS
                else redact_sensitive(item)
            )
        return output
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def validate_create_request(
    args: argparse.Namespace, input_fn: Callable[[str], str] = input
) -> bool:
    if not bool(getattr(args, "create_order", False)):
        return False
    if getattr(args, "ack_create", "") != YonexClient.CREATE_ORDER_CONFIRMATION:
        raise ValueError("--create-order 还需要 --ack-create CREATE_UNPAID_ORDER")
    try:
        typed = input_fn("将创建真实待支付订单；输入 CREATE 继续：").strip()
    except EOFError as exc:
        raise ValueError("未收到交互确认 CREATE") from exc
    if typed != "CREATE":
        raise ValueError("交互确认不匹配，订单未创建")
    return True


def _csv(values: str, *, integers: bool = False) -> list[Any]:
    result = [item.strip() for item in str(values or "").split(",") if item.strip()]
    if not integers:
        return result
    try:
        return [int(item) for item in result]
    except ValueError as exc:
        raise ValueError("ID 列表必须是逗号分隔的整数") from exc


def _attributes(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("--attributes 必须是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise ValueError("--attributes 必须是 JSON 对象")
    return value


def _preview_summary(preview: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cart_item_ids": list(preview.get("cart_item_ids") or []),
        "check_code": preview.get("check_code"),
        "amounts": dict(preview.get("amounts") or {}),
        "items": [
            {
                "id": item.get("id"),
                "product_id": item.get("productId"),
                "sku_id": item.get("productSkuId"),
                "quantity": item.get("quantity"),
                "price": item.get("price"),
                "stock": item.get("stock"),
            }
            for item in preview.get("items") or []
            if isinstance(item, Mapping)
        ],
        "addresses": [
            {"id": item.get("id"), "default": item.get("defaultStatus") == 1}
            for item in preview.get("addresses") or []
            if isinstance(item, Mapping)
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="YONEX 商品、购物车与待支付订单工具（默认只读，不执行支付）"
    )
    parser.add_argument(
        "--session",
        default=os.getenv(
            "YONEX_SESSION_FILE",
            str(PROJECT_ROOT / "captures" / "sessions" / "yonex-default.json"),
        ),
        help="YONEX session JSON",
    )
    parser.add_argument("--search", metavar="KEYWORD", help="按名称搜索商品")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=6)
    parser.add_argument("--product-ids", help="批量加载商品 ID，逗号分隔")
    parser.add_argument("--product-id", type=int, help="加载单个商品详情")
    parser.add_argument("--sku-id", type=int, help="指定 SKU ID")
    parser.add_argument("--sku-code", default="", help="指定 SKU code")
    parser.add_argument(
        "--attributes", default="", help='SKU 属性 JSON，例如 {"颜色":"蓝色"}'
    )
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--add-to-cart", action="store_true", help="显式加入购物车")
    parser.add_argument("--cart", action="store_true", help="读取购物车")
    parser.add_argument("--cart-item-ids", help="结算用购物车项 ID，逗号分隔")
    parser.add_argument(
        "--preflight", action="store_true", help="只执行订单校验和结算预览"
    )
    parser.add_argument("--address-id", type=int, help="创建订单使用的收货地址 ID")
    parser.add_argument(
        "--create-order", action="store_true", help="创建真实待支付订单"
    )
    parser.add_argument(
        "--ack-create",
        default="",
        help="创建订单第二开关：CREATE_UNPAID_ORDER",
    )
    parser.add_argument(
        "--payment-params", action="store_true", help="单独获取已有订单支付参数"
    )
    parser.add_argument("--order-sn", default="", help="获取支付参数使用的订单号")
    parser.add_argument(
        "--ack-payment-params",
        default="",
        help="支付参数开关：FETCH_PAYMENT_PARAMS",
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    client_factory: Callable[..., YonexClient] = YonexClient,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> int:
    client = client_factory(session_path=Path(args.session).expanduser())
    performed = False

    def emit(label: str, payload: Any) -> None:
        output(
            json.dumps(
                {"action": label, "data": redact_sensitive(payload)},
                ensure_ascii=False,
                indent=2,
            )
        )

    if args.search:
        emit("search", client.search_products(args.search, args.page, args.page_size))
        performed = True

    if args.product_ids:
        emit("load_products", client.load_products(_csv(args.product_ids)))
        performed = True

    detail: Optional[dict[str, Any]] = None
    if args.product_id is not None:
        detail = client.product_detail(args.product_id)
        emit("product_detail", detail)
        performed = True

    if args.add_to_cart:
        if args.product_id is None:
            raise ValueError("--add-to-cart 需要 --product-id")
        detail = detail or client.product_detail(args.product_id)
        sku = client.select_sku(
            detail,
            sku_id=args.sku_id,
            sku_code=args.sku_code,
            attributes=_attributes(args.attributes),
        )
        result = client.add_to_cart(
            args.product_id,
            sku["id"],
            sku["sku_code"],
            quantity=args.quantity,
        )
        emit(
            "add_to_cart",
            {"code": result.get("code"), "cart_item_id": result.get("data")},
        )
        performed = True

    if args.cart:
        emit("cart", client.cart_list())
        performed = True

    cart_item_ids = (
        _csv(args.cart_item_ids, integers=True) if args.cart_item_ids else []
    )
    preview = None
    if args.preflight or args.create_order:
        if not cart_item_ids:
            raise ValueError("--preflight/--create-order 需要 --cart-item-ids")
        preview = client.checkout_preview(cart_item_ids)
        emit("checkout_preview", _preview_summary(preview))
        performed = True

    if args.create_order:
        if args.address_id is None:
            raise ValueError("--create-order 需要 --address-id")
        validate_create_request(args, input_fn=input_fn)
        order = client.create_unpaid_order(
            cart_item_ids,
            args.address_id,
            confirmation=YonexClient.CREATE_ORDER_CONFIRMATION,
        )
        emit("create_unpaid_order", order)
        performed = True

    if args.payment_params:
        if not args.order_sn:
            raise ValueError("--payment-params 需要 --order-sn")
        if args.ack_payment_params != YonexClient.PAYMENT_PARAMS_CONFIRMATION:
            raise ValueError(
                "--payment-params 还需要 --ack-payment-params FETCH_PAYMENT_PARAMS"
            )
        params = client.payment_parameters(
            args.order_sn,
            confirmation=YonexClient.PAYMENT_PARAMS_CONFIRMATION,
        )
        emit("payment_parameters", params)
        performed = True

    if not performed:
        emit("session_summary", client.session_summary())
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return run(args)
    except (YonexAPIError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
