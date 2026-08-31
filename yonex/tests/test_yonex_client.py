import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from yonex_client import YonexAPIError, YonexClient  # noqa: E402


class StubResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class StubHTTP:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.payloads:
            raise AssertionError(f"unexpected request: {method} {url}")
        payload = self.payloads.pop(0)
        return payload if isinstance(payload, StubResponse) else StubResponse(payload)


class YonexClientTests(unittest.TestCase):
    def test_default_http_session_does_not_inherit_proxy_environment(self):
        client = YonexClient()
        self.assertFalse(client.http.trust_env)

    def test_guest_login_clears_a_stale_member_token_like_the_v29_client(self):
        http = StubHTTP(
            {"code": 200, "message": "ok", "data": {"isGuest": True, "oid": "new-oid"}}
        )
        client = YonexClient(http=http, token="stale-member-token")

        client.login_with_wechat_code("fresh-code")

        self.assertEqual(client.token, "")
        self.assertEqual(http.calls[0][2]["headers"]["Authorization"], "Bearer")

    def test_login_flow_persists_token_and_oid_without_one_time_codes(self):
        with tempfile.TemporaryDirectory() as td:
            session_path = Path(td) / "yonex-account.json"
            http = StubHTTP(
                {
                    "code": 200,
                    "message": "ok",
                    "data": {"isGuest": True, "oid": "oid-1"},
                },
                {
                    "code": 200,
                    "message": "ok",
                    "data": {"isGuest": False, "token": "member-token"},
                },
            )
            client = YonexClient(session_path=session_path, http=http)

            client.login_with_wechat_code("wx-login-code")
            client.login_with_phone_code(
                {"code": "phone-code", "encryptedData": "ciphertext", "iv": "phone-iv"}
            )

            self.assertEqual(client.state["oid"], "oid-1")
            self.assertEqual(client.state["token"], "member-token")
            self.assertFalse(client.state["is_guest"])
            saved = session_path.read_text(encoding="utf-8")
            self.assertNotIn("wx-login-code", saved)
            self.assertNotIn("phone-code", saved)
            self.assertNotIn("ciphertext", saved)
            self.assertFalse(session_path.with_suffix(".tmp").exists())

            first = http.calls[0]
            self.assertEqual(first[0], "POST")
            self.assertTrue(first[1].endswith("/api/sso/xcxLogin"))
            self.assertEqual(first[2]["json"], {"code": "wx-login-code", "token": ""})
            second = http.calls[1]
            self.assertEqual(
                second[2]["json"],
                {
                    "code": "phone-code",
                    "encryptedData": "ciphertext",
                    "iv": "phone-iv",
                    "token": "",
                },
            )

    def test_search_load_detail_and_sku_selection_match_v29_api(self):
        http = StubHTTP(
            {
                "code": 200,
                "message": "ok",
                "data": {
                    "list": [
                        {
                            "id": 1263,
                            "name": "ASTROX",
                            "price": 100.0,
                            "stock": 3,
                            "pic": "p",
                        }
                    ],
                    "pages": 2,
                },
            },
            {
                "code": 200,
                "message": "ok",
                "data": [
                    {
                        "id": 1263,
                        "name": "ASTROX",
                        "price": 100.0,
                        "stock": 3,
                        "skuList": [],
                    }
                ],
            },
            {
                "code": 200,
                "message": "ok",
                "data": {
                    "id": 1263,
                    "name": "ASTROX",
                    "productSn": "AX-1",
                    "price": 100.0,
                    "stock": 3,
                    "skuList": [
                        {
                            "id": 10641,
                            "productId": 1263,
                            "skuCode": "AX-BLUE-4U",
                            "price": 100.0,
                            "stock": 2,
                            "spData": '[{"key":"颜色","value":"蓝色"},{"key":"规格","value":"4U"}]',
                        },
                        {
                            "id": 10642,
                            "productId": 1263,
                            "skuCode": "AX-RED-3U",
                            "price": 101.0,
                            "stock": 0,
                            "spData": '[{"key":"颜色","value":"红色"},{"key":"规格","value":"3U"}]',
                        },
                    ],
                },
            },
        )
        client = YonexClient(http=http, token="member-token")

        result = client.search_products("ASTROX", page_num=2, page_size=6)
        self.assertEqual(result["items"][0]["id"], 1263)
        self.assertEqual(result["page"], 2)
        self.assertEqual(result["pages"], 2)
        method, url, kwargs = http.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/api/product/category/searchProduct"))
        self.assertEqual(
            kwargs["params"],
            {"name": "ASTROX", "pageNum": 2, "pageSize": 6, "token": "member-token"},
        )

        loaded = client.load_products([1263])
        self.assertEqual(loaded[0]["name"], "ASTROX")
        self.assertEqual(http.calls[1][2]["json"], ["1263"])

        detail = client.product_detail(1263)
        sku = client.select_sku(detail, attributes={"颜色": "蓝色", "规格": "4U"})
        self.assertEqual(sku["id"], 10641)
        self.assertEqual(sku["sku_code"], "AX-BLUE-4U")
        self.assertEqual(sku["attributes"], {"颜色": "蓝色", "规格": "4U"})

    def test_cart_checkout_and_unpaid_order_are_separate_from_payment(self):
        http = StubHTTP(
            {"code": 200, "message": "ok", "data": 50101},
            {
                "code": 200,
                "message": "ok",
                "data": [
                    {
                        "id": 50101,
                        "productId": 1263,
                        "productSkuId": 10641,
                        "productSkuCode": "AX-BLUE-4U",
                        "quantity": 1,
                        "stock": 2,
                        "status": 1,
                    }
                ],
            },
            {"code": 200, "message": ""},
            {
                "code": 200,
                "message": "ok",
                "data": [{"id": 7001, "defaultStatus": 1, "name": "masked"}],
            },
            {
                "code": 200,
                "message": "ok",
                "data": {
                    "cartPromotionItemList": [{"id": 50101, "quantity": 1}],
                    "memberReceiveAddressList": [{"id": 7001}],
                    "calcAmount": {"payAmount": 100.0},
                },
            },
            {
                "code": 200,
                "message": "ok",
                "data": {
                    "order": {
                        "id": 9001,
                        "orderSn": "ORDER-1",
                        "status": 0,
                        "payAmount": 100.0,
                    },
                    "orderItemList": [{"productId": 1263, "productSkuId": 10641}],
                },
            },
            {
                "code": 200,
                "message": "ok",
                "data": {
                    "appId": "wx1656f93aeb347dbc",
                    "timeStamp": "1",
                    "nonceStr": "n",
                    "packageValue": "prepay_id=x",
                    "signType": "MD5",
                    "paySign": "sig",
                },
            },
        )
        client = YonexClient(http=http, token="member-token")

        client.add_to_cart(1263, 10641, "AX-BLUE-4U", quantity=1)
        self.assertEqual(
            http.calls[0][2]["json"],
            {
                "productId": 1263,
                "productSkuId": 10641,
                "productSkuCode": "AX-BLUE-4U",
                "quantity": 1,
                "productLine": "",
                "token": "member-token",
            },
        )
        self.assertEqual(client.cart_list()[0]["id"], 50101)

        preview = client.checkout_preview([50101])
        self.assertEqual(preview["cart_item_ids"], [50101])
        self.assertEqual(preview["amounts"]["payAmount"], 100.0)
        self.assertEqual(http.calls[2][2]["json"], [50101])
        self.assertEqual(http.calls[4][2]["json"], ["50101"])

        with self.assertRaises(ValueError):
            client.create_unpaid_order([50101], 7001, confirmation="")
        self.assertEqual(len(http.calls), 5)

        order = client.create_unpaid_order(
            [50101], 7001, confirmation=YonexClient.CREATE_ORDER_CONFIRMATION
        )
        self.assertEqual(order["status"], 0)
        self.assertEqual(order["order_sn"], "ORDER-1")
        self.assertTrue(http.calls[5][1].endswith("/api/order/generateOrder"))
        self.assertEqual(
            http.calls[5][2]["json"],
            {
                "items": ["50101"],
                "memberReceiveAddressId": 7001,
                "token": "member-token",
            },
        )
        self.assertFalse(any("payOrder" in call[1] for call in http.calls[:6]))

        with self.assertRaises(ValueError):
            client.payment_parameters("ORDER-1", confirmation="")
        payment = client.payment_parameters(
            "ORDER-1", confirmation=YonexClient.PAYMENT_PARAMS_CONFIRMATION
        )
        self.assertEqual(payment["signType"], "MD5")
        self.assertTrue(http.calls[6][1].endswith("/api/pay/payOrder"))

    def test_business_errors_do_not_echo_token(self):
        token = "secret-member-token"
        client = YonexClient(
            http=StubHTTP({"code": 401, "message": "expired", "data": None}),
            token=token,
        )
        with self.assertRaises(YonexAPIError) as caught:
            client.cart_list()
        self.assertNotIn(token, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
