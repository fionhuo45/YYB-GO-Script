#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YONEX 微信小程序 v29 API 客户端。

默认方法只读取商品、购物车和结算预览。创建待支付订单与获取支付参数是
两个独立且显式确认的方法；本模块没有调用微信支付的实现。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import requests


APP_ID = "wx1656f93aeb347dbc"
APP_VERSION = 29
API_BASE = "https://yonexmall.surto.cn/api"
REFERER = f"https://servicewechat.com/{APP_ID}/{APP_VERSION}/page-frame.html"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; Pixel 8 Build/CP1A.260505.005; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 "
    "Mobile Safari/537.36 MicroMessenger/8.0.69 MiniProgramEnv/android"
)


class YonexAPIError(RuntimeError):
    """YONEX 传输或业务错误；异常文本不会包含请求凭据。"""

    def __init__(self, path: str, code: Any, message: str):
        self.path = path
        self.code = code
        self.message = str(message or "YONEX API request failed")
        super().__init__(f"{path}: code={code!r} message={self.message}")


def _int_if_numeric(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        try:
            return int(value)
        except ValueError:
            pass
    return value


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


class YonexClient:
    """YONEX 商城客户端，复现小程序 v29 的字段与调用顺序。"""

    CREATE_ORDER_CONFIRMATION = "CREATE_UNPAID_ORDER"
    PAYMENT_PARAMS_CONFIRMATION = "FETCH_PAYMENT_PARAMS"

    def __init__(
        self,
        session_path: Optional[Path | str] = None,
        *,
        http: Any = None,
        token: Optional[str] = None,
        oid: Optional[str] = None,
        timeout: int = 30,
        api_base: str = API_BASE,
    ):
        self.session_path = Path(session_path).expanduser() if session_path else None
        if http is None:
            http = requests.Session()
            http.trust_env = False
        self.http = http
        self.timeout = int(timeout)
        self.api_base = api_base.rstrip("/")
        self.state: dict[str, Any] = {
            "app_id": APP_ID,
            "app_version": APP_VERSION,
            "api_base": self.api_base,
            "token": "",
            "oid": "",
            "is_guest": True,
        }
        if self.session_path and self.session_path.exists():
            self.load_session()
        if token is not None:
            self.state["token"] = str(token)
        if oid is not None:
            self.state["oid"] = str(oid)

    @property
    def token(self) -> str:
        return str(self.state.get("token") or "")

    @property
    def oid(self) -> str:
        return str(self.state.get("oid") or "")

    def load_session(self) -> dict[str, Any]:
        if not self.session_path:
            raise ValueError("session_path is not configured")
        payload = json.loads(self.session_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("YONEX session must be a JSON object")
        self.state.update(payload)
        self.api_base = str(self.state.get("api_base") or self.api_base).rstrip("/")
        return self.state

    def save_session(self) -> None:
        """在同目录写临时文件后原子替换，不保存微信一次性 code。"""
        if not self.session_path:
            return
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.state.update(
            {
                "app_id": APP_ID,
                "app_version": APP_VERSION,
                "api_base": self.api_base,
                "updated_at": int(time.time()),
            }
        )
        temporary = self.session_path.with_suffix(".tmp")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(temporary, flags, 0o600)
            try:
                stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            except Exception:
                os.close(descriptor)
                raise
            with stream as handle:
                json.dump(self.state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.session_path)
            try:
                directory_fd = os.open(
                    self.session_path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def session_summary(self) -> dict[str, Any]:
        """返回不含 token、oid 原值的本地登录态摘要。"""
        return {
            "app_id": APP_ID,
            "ref": self.state.get("ref"),
            "tag": self.state.get("tag", "all"),
            "authenticated": bool(self.token)
            and not bool(self.state.get("is_guest", True)),
            "is_guest": bool(self.state.get("is_guest", True)),
            "token_present": bool(self.token),
            "oid_present": bool(self.oid),
            "updated_at": self.state.get("updated_at"),
        }

    def _headers(self, token_override: Optional[str] = None) -> dict[str, str]:
        token = self.token if token_override is None else str(token_override)
        authorization = f"Bearer {token}" if token else "Bearer"
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "charset": "utf-8",
            "Authorization": authorization,
            "Referer": REFERER,
            "User-Agent": USER_AGENT,
        }

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        token_override: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_path = "/" + path.lstrip("/")
        url = f"{self.api_base}{normalized_path}"
        kwargs: dict[str, Any] = {
            "headers": self._headers(token_override),
            "timeout": self.timeout,
        }
        if params is not None:
            kwargs["params"] = dict(params)
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            response = self.http.request(method.upper(), url, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except YonexAPIError:
            raise
        except Exception as exc:
            raise YonexAPIError(
                normalized_path, "transport", type(exc).__name__
            ) from exc
        if not isinstance(payload, dict):
            raise YonexAPIError(
                normalized_path, "invalid_json", "response is not a JSON object"
            )
        code = payload.get("code")
        if str(code) != "200":
            raise YonexAPIError(
                normalized_path, code, payload.get("message") or "business error"
            )
        return payload

    def _update_login_state(self, payload: Mapping[str, Any]) -> None:
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            return
        if data.get("oid"):
            self.state["oid"] = str(data["oid"])
        if data.get("token"):
            self.state["token"] = str(data["token"])
        if data.get("isGuest") is not None:
            self.state["is_guest"] = bool(data.get("isGuest"))
        profile_keys = {
            "xcxId": "xcx_id",
            "nickname": "nickname",
            "headimgurl": "headimgurl",
            "integration": "integration",
        }
        for source, destination in profile_keys.items():
            if data.get(source) is not None:
                self.state[destination] = data[source]
        self.save_session()

    def login_with_wechat_code(self, code: str) -> dict[str, Any]:
        """用一次性 ``wx.login`` code 换 guest/member 登录态。"""
        code = str(code or "").strip()
        if not code:
            raise ValueError("wx.login code is required")
        payload = self._call(
            "POST",
            "/sso/xcxLogin",
            json_body={"code": code, "token": ""},
            token_override="",
        )
        # 原版 apiPub.login 在 wx.login 前先清空本地 token；成功返回 guest
        # 时不能继续保留并误用旧会员 token。
        self.state["token"] = ""
        self._update_login_state(payload)
        return payload

    def login_with_phone_code(
        self,
        phone_code: str | Mapping[str, Any],
        encrypted_data: str = "",
        iv: str = "",
    ) -> dict[str, Any]:
        """用手机号授权材料升级为完整商城登录态。

        v29 的 ``code`` 是点击手机号按钮后再次调用 ``wx.login`` 得到的
        全新 code；``encryptedData``/``iv`` 才来自 ``getPhoneNumber``。
        """
        if isinstance(phone_code, Mapping):
            material = dict(phone_code)
            code = material.get("code") or material.get("phone_code")
            encrypted_data = str(
                material.get("encryptedData")
                or material.get("encrypted_data")
                or encrypted_data
                or ""
            )
            iv = str(material.get("iv") or iv or "")
        else:
            code = phone_code
        code = str(code or "").strip()
        if not code:
            raise ValueError("phone authorization code is required")
        payload = self._call(
            "POST",
            "/sso/getPhone",
            json_body={
                "code": code,
                "encryptedData": encrypted_data,
                "iv": iv,
                "token": "",
            },
            token_override="",
        )
        self._update_login_state(payload)
        return payload

    def submit_user_profile(
        self, code: str, encrypted_data: str, iv: str
    ) -> dict[str, Any]:
        """可选：提交昵称/头像授权数据；登录续期本身不依赖此步骤。"""
        payload = self._call(
            "POST",
            "/sso/xcxInfo",
            json_body={
                "code": str(code),
                "encryptedData": str(encrypted_data),
                "iv": str(iv),
                "token": self.token,
            },
        )
        self._update_login_state(payload)
        return payload

    @staticmethod
    def _normalize_sku(raw: Mapping[str, Any]) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        sp_data = raw.get("spData")
        if isinstance(sp_data, str) and sp_data.strip():
            try:
                parsed = json.loads(sp_data)
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, Mapping) and item.get("key") is not None:
                        attributes[str(item.get("key"))] = item.get("value")
        return {
            "id": _int_if_numeric(raw.get("id")),
            "product_id": _int_if_numeric(raw.get("productId")),
            "sku_code": raw.get("skuCode") or "",
            "evans_code": raw.get("evansCode") or "",
            "price": raw.get("price"),
            "stock": int(raw.get("stock") or 0),
            "locked_stock": int(raw.get("lockStock") or 0),
            "pic": raw.get("pic") or "",
            "attributes": attributes,
        }

    @classmethod
    def _normalize_product(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        sku_list = raw.get("skuList") or raw.get("skus") or []
        skus = [
            cls._normalize_sku(item) for item in sku_list if isinstance(item, Mapping)
        ]
        return {
            "id": _int_if_numeric(raw.get("id")),
            "name": raw.get("name") or "",
            "product_sn": raw.get("productSn") or raw.get("product_sn") or "",
            "price": raw.get("price"),
            "original_price": raw.get("originalPrice", raw.get("original_price")),
            "stock": int(raw.get("stock") or 0),
            "pic": raw.get("pic") or "",
            "category_id": _int_if_numeric(raw.get("productCategoryId")),
            "publish_status": raw.get("publishStatus"),
            "limit_flag": int(raw.get("limitFlag") or 0),
            "limit_num": int(raw.get("limitNum") or 0),
            "pre_flag": int(raw.get("preFlag") or 0),
            "skus": skus,
        }

    def search_products(
        self, keyword: str, page_num: int = 1, page_size: int = 6
    ) -> dict[str, Any]:
        """调用 v29 搜索接口：GET ``product/category/searchProduct``。"""
        keyword = str(keyword or "").strip()
        if not keyword:
            raise ValueError("search keyword is required")
        if page_num < 1 or page_size < 1:
            raise ValueError("page_num and page_size must be positive")
        payload = self._call(
            "GET",
            "/product/category/searchProduct",
            params={
                "name": keyword,
                "pageNum": int(page_num),
                "pageSize": int(page_size),
                "token": self.token,
            },
        )
        data = payload.get("data")
        if isinstance(data, list):
            items, metadata = data, {}
        elif isinstance(data, Mapping):
            items = data.get("list") or []
            metadata = data
        else:
            items, metadata = [], {}
        return {
            "keyword": keyword,
            "page": int(metadata.get("pageNum") or page_num),
            "page_size": int(metadata.get("pageSize") or page_size),
            "pages": int(metadata.get("pages") or metadata.get("totalPage") or 1),
            "total": metadata.get("total"),
            "items": [
                self._normalize_product(item)
                for item in items
                if isinstance(item, Mapping)
            ],
        }

    def load_products(self, product_ids: Iterable[int | str]) -> list[dict[str, Any]]:
        ids = [str(value).strip() for value in product_ids if str(value).strip()]
        if not ids:
            return []
        payload = self._call("POST", "/product/category/getDetailList", json_body=ids)
        data = payload.get("data")
        return [
            self._normalize_product(item)
            for item in data or []
            if isinstance(item, Mapping)
        ]

    def product_detail(self, product_id: int | str) -> dict[str, Any]:
        product_id = _int_if_numeric(product_id)
        payload = self._call(
            "GET",
            "/product/category/getProductDetail",
            params={"productId": product_id, "token": self.token},
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise YonexAPIError(
                "/product/category/getProductDetail", 200, "missing product data"
            )
        return self._normalize_product(data)

    def select_sku(
        self,
        product: Mapping[str, Any],
        *,
        sku_id: Optional[int | str] = None,
        sku_code: str = "",
        attributes: Optional[Mapping[str, Any]] = None,
        in_stock_only: bool = True,
    ) -> dict[str, Any]:
        raw_skus = product.get("skus") or product.get("skuList") or []
        skus = []
        for item in raw_skus:
            if not isinstance(item, Mapping):
                continue
            skus.append(dict(item) if "sku_code" in item else self._normalize_sku(item))
        candidates = skus
        if sku_id is not None:
            expected = _int_if_numeric(sku_id)
            candidates = [
                item
                for item in candidates
                if _int_if_numeric(item.get("id")) == expected
            ]
        if sku_code:
            candidates = [
                item
                for item in candidates
                if str(item.get("sku_code") or "") == sku_code
            ]
        if attributes:
            expected_attributes = {str(k): str(v) for k, v in attributes.items()}
            candidates = [
                item
                for item in candidates
                if all(
                    str((item.get("attributes") or {}).get(key)) == value
                    for key, value in expected_attributes.items()
                )
            ]
        if in_stock_only:
            candidates = [
                item for item in candidates if int(item.get("stock") or 0) > 0
            ]
        if not candidates:
            raise LookupError("no matching available SKU")
        return candidates[0]

    def add_to_cart(
        self,
        product_id: int | str,
        sku_id: int | str,
        sku_code: str,
        *,
        quantity: int = 1,
        product_line: Any = "",
    ) -> dict[str, Any]:
        if int(quantity) < 1:
            raise ValueError("quantity must be positive")
        return self._call(
            "POST",
            "/cart/add",
            json_body={
                "productId": _int_if_numeric(product_id),
                "productSkuId": _int_if_numeric(sku_id),
                "productSkuCode": str(sku_code),
                "quantity": int(quantity),
                "productLine": product_line,
                "token": self.token,
            },
        )

    def cart_list(self) -> list[dict[str, Any]]:
        payload = self._call("GET", "/cart/list", params={"token": self.token})
        data = payload.get("data")
        if isinstance(data, str):
            if not data.strip():
                return []
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return []
        if isinstance(data, Mapping):
            data = data.get("list") or []
        return [dict(item) for item in data or [] if isinstance(item, Mapping)]

    @staticmethod
    def _cart_ids(values: Iterable[int | str]) -> list[int]:
        ids = []
        for value in values:
            converted = _int_if_numeric(value)
            if not isinstance(converted, int) or converted <= 0:
                raise ValueError(f"invalid cart item id: {value!r}")
            ids.append(converted)
        if not ids:
            raise ValueError("at least one cart item id is required")
        return ids

    def check_order(self, cart_item_ids: Iterable[int | str]) -> dict[str, Any]:
        ids = self._cart_ids(cart_item_ids)
        return self._call("POST", "/order/checkOrder", json_body=ids)

    def address_list(self) -> list[dict[str, Any]]:
        payload = self._call(
            "GET", "/member/address/list", params={"token": self.token}
        )
        data = payload.get("data")
        return [dict(item) for item in data or [] if isinstance(item, Mapping)]

    def generate_confirm_order(
        self, cart_item_ids: Iterable[int | str]
    ) -> dict[str, Any]:
        ids = self._cart_ids(cart_item_ids)
        payload = self._call(
            "POST",
            "/order/generateConfirmOrder",
            json_body=[str(value) for value in ids],
        )
        data = payload.get("data")
        return dict(data) if isinstance(data, Mapping) else {}

    def checkout_preview(self, cart_item_ids: Iterable[int | str]) -> dict[str, Any]:
        """校验购物车并加载地址、金额；不创建订单。"""
        ids = self._cart_ids(cart_item_ids)
        check = self.check_order(ids)
        addresses = self.address_list()
        confirmation = self.generate_confirm_order(ids)
        return {
            "cart_item_ids": ids,
            "check_code": check.get("code"),
            "items": confirmation.get("cartPromotionItemList") or [],
            "addresses": addresses
            or confirmation.get("memberReceiveAddressList")
            or [],
            "amounts": confirmation.get("calcAmount") or {},
        }

    def create_unpaid_order(
        self,
        cart_item_ids: Iterable[int | str],
        address_id: int | str,
        *,
        confirmation: str,
    ) -> dict[str, Any]:
        """创建一个待支付订单；必须传入固定确认串，且不会请求支付参数。"""
        if confirmation != self.CREATE_ORDER_CONFIRMATION:
            raise ValueError(
                f"creating an unpaid order requires confirmation={self.CREATE_ORDER_CONFIRMATION!r}"
            )
        ids = self._cart_ids(cart_item_ids)
        address = _int_if_numeric(address_id)
        if not isinstance(address, int) or address <= 0:
            raise ValueError("address_id must be a positive integer")
        payload = self._call(
            "POST",
            "/order/generateOrder",
            json_body={
                "items": [str(value) for value in ids],
                "memberReceiveAddressId": address,
                "token": self.token,
            },
        )
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        order = data.get("order") if isinstance(data, Mapping) else {}
        order = order if isinstance(order, Mapping) else {}
        items = data.get("orderItemList") if isinstance(data, Mapping) else []
        return {
            "order_id": _int_if_numeric(order.get("id")),
            "order_sn": order.get("orderSn"),
            "status": _int_if_numeric(order.get("status")),
            "pay_amount": order.get("payAmount"),
            "items": [
                {
                    "product_id": _int_if_numeric(item.get("productId")),
                    "sku_id": _int_if_numeric(item.get("productSkuId")),
                    "quantity": _int_if_numeric(item.get("productQuantity")),
                }
                for item in items or []
                if isinstance(item, Mapping)
            ],
        }

    def payment_parameters(self, order_sn: str, *, confirmation: str) -> dict[str, Any]:
        """单独获取微信支付参数；不调用 ``wx.requestPayment``。"""
        if confirmation != self.PAYMENT_PARAMS_CONFIRMATION:
            raise ValueError(
                f"fetching payment parameters requires confirmation={self.PAYMENT_PARAMS_CONFIRMATION!r}"
            )
        order_sn = str(order_sn or "").strip()
        if not order_sn:
            raise ValueError("order_sn is required")
        payload = self._call(
            "GET",
            "/pay/payOrder",
            params={"orderSn": order_sn, "token": self.token},
        )
        data = payload.get("data")
        return dict(data) if isinstance(data, Mapping) else {}

    def order_detail(self, order_id: int | str) -> dict[str, Any]:
        payload = self._call(
            "GET",
            "/order/getDetail",
            params={"id": _int_if_numeric(order_id), "token": self.token},
        )
        data = payload.get("data")
        return dict(data) if isinstance(data, Mapping) else {}

    def verify_session(self) -> dict[str, Any]:
        if not self.token:
            raise YonexAPIError("session", 401, "token is missing")
        cart = self.cart_list()
        return {
            "authenticated": True,
            "is_guest": bool(self.state.get("is_guest", False)),
            "cart_items": len(cart),
        }


__all__ = [
    "APP_ID",
    "APP_VERSION",
    "API_BASE",
    "YonexAPIError",
    "YonexClient",
]
