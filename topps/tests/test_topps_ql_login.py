import importlib.util
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "jobs" / "topps_ql_login.py"
)


def load_module():
    if not SCRIPT_PATH.exists():
        raise AssertionError(f"青龙登录脚本尚未创建: {SCRIPT_PATH}")
    spec = importlib.util.spec_from_file_location("topps_ql_login", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class YybConfigurationTests(unittest.TestCase):
    def test_parse_multiple_yyb_accounts(self):
        module = load_module()

        accounts = module.parse_yyb_servers(
            "http://yyb-go:8000@1\nhttps://10.0.0.2:9000/@openid-2\n"
        )

        self.assertEqual(
            accounts,
            [
                ("http://yyb-go:8000", "1", "all"),
                ("https://10.0.0.2:9000", "openid-2", "all"),
            ],
        )

    def test_select_refresh_accounts_filters_ref_and_tag(self):
        module = load_module()
        accounts = [
            ("http://yyb:8000", "1", "all"),
            ("http://yyb:8000", "13", "lf"),
            ("http://yyb:8000", "25", "lf"),
        ]
        self.assertEqual(module.select_refresh_accounts(accounts, ref="13"), [accounts[1]])
        self.assertEqual(module.select_refresh_accounts(accounts, tag="lf"), [accounts[1], accounts[2]])
        self.assertEqual(module.select_refresh_accounts(accounts, ref="13", tag="lf"), [accounts[1]])
        self.assertEqual(module.select_refresh_accounts(accounts, ref="13", tag="gg"), [])

    def test_parse_yyb_tag_and_default_all(self):
        module = load_module()

        accounts = module.parse_yyb_servers(
            "http://yyb-go:8000@13#群A\nhttp://yyb-go:8000@1\n"
        )

        self.assertEqual(
            accounts,
            [
                ("http://yyb-go:8000", "13", "群A"),
                ("http://yyb-go:8000", "1", "all"),
            ],
        )

    def test_task_exit_code_partial_success_counts_as_success(self):
        module = load_module()

        self.assertEqual(module.task_exit_code(0, 0), 0)   # 全部成功
        self.assertEqual(module.task_exit_code(1, 1), 0)   # 部分成功
        self.assertEqual(module.task_exit_code(2, 1), 0)   # 部分成功
        self.assertEqual(module.task_exit_code(0, 1), 1)   # 全部失败
        self.assertEqual(module.task_exit_code(0, 2), 1)   # 全部失败

    def test_invalid_yyb_entry_is_rejected(self):
        module = load_module()

        with self.assertRaisesRegex(ValueError, "地址@微信账号标识"):
            module.parse_yyb_servers("http://yyb-go:8000")


class YybCodeTests(unittest.TestCase):
    def test_get_code_calls_expected_endpoint_without_proxy(self):
        module = load_module()
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "code": 0,
            "data": {"result": {"code": "wx-one-time-code"}},
        }
        response.raise_for_status.return_value = None
        session.post.return_value = response

        code = module.get_wechat_code(
            session, "http://yyb-go:8000", "1", timeout=7
        )

        session.post.assert_called_once_with(
            "http://yyb-go:8000/wxapp/getCode",
            json={"ref": "1", "app_id": "wx7f5b9b4a432faaf0"},
            timeout=7,
            proxies={"http": None, "https": None},
        )
        self.assertEqual(code, "wx-one-time-code")

    def test_get_phone_code_calls_expected_endpoint_without_proxy(self):
        module = load_module()
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "code": 0,
            "data": {"result": {"code": "wx-phone-one-time-code"}},
        }
        response.raise_for_status.return_value = None
        session.post.return_value = response

        code = module.get_wechat_phone_code(
            session, "http://yyb-go:8000", "1", timeout=7
        )

        session.post.assert_called_once_with(
            "http://yyb-go:8000/wxapp/getPhoneNumber",
            json={"ref": "1", "app_id": "wx7f5b9b4a432faaf0"},
            timeout=7,
            proxies={"http": None, "https": None},
        )
        self.assertEqual(code, "wx-phone-one-time-code")

    def test_get_crypto_material_parses_nested_yyb_result(self):
        module = load_module()
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "code": 0,
            "data": {
                "result": {
                    "err_no": 0,
                    "data": json.dumps({
                        "encrypt_key": "base64-key",
                        "iv": "1234567890abcdef",
                        "version": 2,
                        "create_time": 1000,
                        "expire_in": 3600,
                    }),
                }
            },
        }
        response.raise_for_status.return_value = None
        session.post.return_value = response

        material = module.get_wechat_crypto_material(
            session, "http://yyb-go:8000", "1", timeout=7
        )

        session.post.assert_called_once_with(
            "http://yyb-go:8000/wxapp/operateWxData",
            json={
                "ref": "1",
                "app_id": "wx7f5b9b4a432faaf0",
                "payload": {
                    "api_name": "webapi_getuserencryptkey",
                    "data": {},
                },
            },
            timeout=7,
            proxies={"http": None, "https": None},
        )
        self.assertEqual(material["encrypt_key"], "base64-key")
        self.assertEqual(material["iv"], "1234567890abcdef")
        self.assertEqual(material["version"], 2)
        self.assertEqual(material["expires_at"], 4600)


class LoginFlowTests(unittest.TestCase):
    def test_expired_refresh_token_is_not_sent_with_fresh_wechat_code(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "topps-13.json"

            def jwt(exp):
                payload = base64.urlsafe_b64encode(
                    json.dumps({"exp": exp}).encode("utf-8")
                ).decode("ascii").rstrip("=")
                return f"header.{payload}.sig"

            path.write_text(
                json.dumps({
                    "token": jwt(int(time.time()) - 3600),
                    "refresh_token": jwt(int(time.time()) - 3600),
                }),
                encoding="utf-8",
            )
            client = module.ToppsClient(path)
            response = Mock()
            response.json.return_value = {
                "code": 1,
                "data": {
                    "token": jwt(int(time.time()) + 3600),
                    "openid": "openid-13",
                },
            }
            response.cookies.get.return_value = ""
            client.s = Mock()
            client.s.cookies.get.return_value = ""
            client.s.post.return_value = response

            client.login_with_wechat_code("fresh-wx-code")

            self.assertEqual(
                client.s.post.call_args.kwargs["json"]["refresh_token"], ""
            )

    def test_v18_referer_is_used_for_topps_login(self):
        module = load_module()

        self.assertEqual(
            module.ToppsClient.__init__.__globals__["REFERER"],
            "https://servicewechat.com/wx7f5b9b4a432faaf0/18/page-frame.html",
        )

    def test_pending_cancel_response_has_actionable_message(self):
        module = load_module()

        class PendingCancelClient(module.ToppsClient):
            def __init__(self):
                self.session = {}

            def get_mobile(self, code, mtype="login"):
                return {
                    "code": 2,
                    "msg": "您有待审核的注销申请",
                    "data": {
                        "pending_cancel_apply": 1,
                        "cancel_apply_id": 123,
                    },
                }

        with self.assertRaisesRegex(RuntimeError, "继续登录"):
            PendingCancelClient().login_with_phone_code("wx-phone-code")

    def test_verified_business_session_is_committed_when_order_crypto_is_unavailable(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "topps-1.json"
            original = '{"token":"OLD","device_token":"dt_old"}'
            real.write_text(original, encoding="utf-8")
            messages = []

            class FakeClient:
                def __init__(self, path):
                    self.session_path = path
                    self.session = {}

                def _set_cookies(self):
                    pass

                def save_session(self):
                    self.session_path.write_text(
                        json.dumps(self.session), encoding="utf-8"
                    )

                def login_with_wechat_code(self, code):
                    self.session.update({"token": "OPENID"})
                    self.save_session()
                    return {"code": 1, "data": {"token": "OPENID"}}

                def login_with_phone_code(self, code):
                    self.session.update({"token": "FULL", "user_id": "u1"})
                    self.save_session()
                    return {
                        "code": 1,
                        "data": {"token": "FULL", "user_level": 1},
                    }

                def set_crypto_material(self, **kwargs):
                    self.session.update(kwargs)
                    self.save_session()

                def device_token(self):
                    self.session.setdefault("device_token", "dt_old")
                    self.save_session()
                    return self.session["device_token"]

                def user_info(self):
                    return {"code": 1, "data": {"user_level": 1}}

                def check_member_by_openid(self):
                    return {"code": 1, "data": {"user_level": 1}}

            def crypto_unavailable(*_args, **_kwargs):
                raise RuntimeError("YYB-Go did not return order crypto")

            result = module.run_account(
                server="http://yyb-go:8000",
                ref="1",
                session_path=real,
                yyb_session=Mock(),
                client_factory=FakeClient,
                code_getter=lambda *_a, **_k: "wx-code",
                phone_code_getter=lambda *_a, **_k: "wx-phone-code",
                crypto_getter=crypto_unavailable,
                output=messages.append,
            )

            saved = json.loads(real.read_text(encoding="utf-8"))
            self.assertEqual(saved["token"], "FULL")
            self.assertEqual(saved["user_id"], "u1")
            self.assertFalse(result["crypto_cached"])
            self.assertIn("订单加密材料暂不可用", "\n".join(messages))
            self.assertFalse((Path(tmp) / "topps-1.tmp").exists())

    def test_run_account_upgrades_to_full_member_token_without_printing_token(self):
        module = load_module()
        client = Mock()
        client.login_with_wechat_code.return_value = {
            "code": 1,
            "data": {"token": "secret-token", "openid": "openid-1"},
        }
        client.login_with_phone_code.return_value = {
            "code": 1,
            "data": {"token": "full-secret-token", "user_level": 1},
        }
        client.user_info.return_value = {
            "code": 1,
            "data": {"user_level": 1, "level": 1},
        }
        messages = []

        result = module.run_account(
            server="http://yyb-go:8000",
            ref="1",
            session_path=Path("unused.json"),
            yyb_session=Mock(),
            client_factory=lambda _: client,
            code_getter=lambda *_args, **_kwargs: "wx-code",
            phone_code_getter=lambda *_args, **_kwargs: "wx-phone-code",
            crypto_getter=lambda *_args, **_kwargs: {
                "encrypt_key": "base64-key",
                "iv": "1234567890abcdef",
                "version": 2,
                "expires_at": 4600,
            },
            output=messages.append,
        )

        client.login_with_wechat_code.assert_called_once_with("wx-code")
        client.login_with_phone_code.assert_called_once_with("wx-phone-code")
        client.set_crypto_material.assert_called_once_with(
            encrypt_key="base64-key",
            iv="1234567890abcdef",
            version=2,
            expires_at=4600,
        )
        client.user_info.assert_called_once_with()
        self.assertTrue(result["is_member"])
        self.assertEqual(result["ref"], "1")
        self.assertNotIn("secret-token", "\n".join(messages))
        self.assertNotIn("full-secret-token", "\n".join(messages))

    def test_session_path_is_scoped_by_account_ref(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = module.account_session_path(Path(tmp), "openid/a@b")

        self.assertEqual(path.name, "topps-openid_a_b.json")

    def test_run_account_preserves_full_session_when_phone_step_fails(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "topps-13.json"
            original = '{"token":"FULL","user_id":"u1","device_token":"d1"}'
            real.write_text(original, encoding="utf-8")
            client = Mock()
            client.login_with_wechat_code.return_value = {
                "code": 1,
                "data": {"token": "new-openid-token"},
            }
            messages = []

            def phone_raises(*_args, **_kwargs):
                raise RuntimeError("need_auth=True")

            with self.assertRaises(RuntimeError):
                module.run_account(
                    server="http://yyb-go:8000",
                    ref="13",
                    session_path=real,
                    yyb_session=Mock(),
                    client_factory=lambda _: client,
                    code_getter=lambda *_args, **_kwargs: "wx-code",
                    phone_code_getter=phone_raises,
                    crypto_getter=lambda *_args, **_kwargs: {},
                    output=messages.append,
                )

            self.assertEqual(real.read_text(encoding="utf-8"), original)
            self.assertFalse((Path(tmp) / "topps-13.tmp").exists())
            self.assertNotIn("登录态已保存", "\n".join(messages))

    def test_run_account_writes_tag_on_success(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "topps-1.json"

            class FakeClient:
                def __init__(self, path):
                    self.session = {"token": "old"}
                    self.session_path = path

                def _set_cookies(self):
                    pass

                def save_session(self):
                    with open(self.session_path, "w", encoding="utf-8") as f:
                        json.dump(self.session, f)

                def login_with_wechat_code(self, code):
                    self.session.update({"token": "new-openid"})
                    self.save_session()
                    return {"code": 1, "data": {"token": "new-openid"}}

                def login_with_phone_code(self, code):
                    self.session.update({"token": "full", "user_id": "u1"})
                    self.save_session()
                    return {"code": 1, "data": {"token": "full", "user_level": 1}}

                def set_crypto_material(self, **kwargs):
                    self.session.update(kwargs)
                    self.save_session()

                def device_token(self):
                    token = self.session.get("device_token")
                    if not token:
                        token = "dt_" + "a" * 32
                        self.session["device_token"] = token
                        self.save_session()
                    return token

                def user_info(self):
                    return {"code": 1, "data": {"user_level": 1}}

                def check_member_by_openid(self):
                    return {"code": 1, "data": {"user_level": 1}}

            result = module.run_account(
                server="http://yyb-go:8000",
                ref="1",
                session_path=real,
                yyb_session=Mock(),
                tag="群A",
                client_factory=lambda _path: FakeClient(_path),
                code_getter=lambda *_a, **_k: "wx-code",
                phone_code_getter=lambda *_a, **_k: "wx-phone-code",
                crypto_getter=lambda *_a, **_k: {
                    "encrypt_key": "key",
                    "iv": "iv",
                    "version": 2,
                    "expires_at": 4600,
                },
                output=lambda _msg: None,
            )

            saved = json.loads(real.read_text(encoding="utf-8"))
            self.assertEqual(saved["tag"], "群A")
            self.assertEqual(saved["user_id"], "u1")
            self.assertTrue(result["is_member"])
            self.assertFalse((Path(tmp) / "topps-1.tmp").exists())

    def test_run_account_initializes_device_token_for_new_account(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "topps-29.json"

            class OfflineClient(module.ToppsClient):
                def login_with_wechat_code(self, code):
                    self.session.update({
                        "token": "openid-token",
                        "refresh_token": "openid-token",
                        "openid": "openid-29",
                    })
                    self.save_session()
                    return {"code": 1, "data": {"token": "openid-token"}}

                def login_with_phone_code(self, code):
                    self.session.update({
                        "token": "full-token",
                        "refresh_token": "full-token",
                        "user_id": 61424,
                        "level": 0,
                    })
                    self.save_session()
                    return {"code": 1, "data": {"token": "full-token"}}

                def user_info(self):
                    return {"code": 1, "data": {"user_level": 0}}

            module.run_account(
                server="http://yyb-go:8000",
                ref="29",
                session_path=real,
                yyb_session=Mock(),
                client_factory=OfflineClient,
                code_getter=lambda *_a, **_k: "wx-code",
                phone_code_getter=lambda *_a, **_k: "wx-phone-code",
                crypto_getter=lambda *_a, **_k: {
                    "encrypt_key": "key",
                    "iv": "1234567890abcdef",
                    "version": 5,
                    "expires_at": 4600,
                },
                output=lambda _msg: None,
            )

            saved = json.loads(real.read_text(encoding="utf-8"))
            self.assertIn("device_token", saved)
            self.assertRegex(saved["device_token"], r"^dt_[0-9a-f]{32}$")
            self.assertFalse((Path(tmp) / "topps-29.tmp").exists())


if __name__ == "__main__":
    unittest.main()
