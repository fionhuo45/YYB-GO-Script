import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import ANY, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yonex_ql_login as ql_login  # noqa: E402
from yonex_client import YonexAPIError, YonexClient  # noqa: E402
from yonex_ql_login import (  # noqa: E402
    account_session_path,
    get_wechat_code,
    get_wechat_phone_auth,
    parse_yyb_servers,
    run_account,
    select_refresh_accounts,
    task_exit_code,
)


class Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class StubHTTP:
    def __init__(self, *payloads, before_verify=None):
        self.payloads = list(payloads)
        self.before_verify = before_verify
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/cart/list") and self.before_verify:
            self.before_verify()
        if not self.payloads:
            raise AssertionError(f"unexpected request: {method} {url}")
        return Response(self.payloads.pop(0))


class YonexQLLoginTests(unittest.TestCase):
    def test_parse_and_select_multi_account_configuration(self):
        accounts = parse_yyb_servers(
            "http://yyb-go:8000@account-a#main\nhttps://yyb.example@account-b\n"
        )
        self.assertEqual(
            accounts,
            [
                ("http://yyb-go:8000", "account-a", "main"),
                ("https://yyb.example", "account-b", "all"),
            ],
        )
        self.assertEqual(select_refresh_accounts(accounts, tag="main"), [accounts[0]])
        self.assertEqual(
            select_refresh_accounts(accounts, ref="account-b"), [accounts[1]]
        )

    def test_phone_authorization_preserves_v29_code_encrypted_data_and_iv(self):
        session = Mock()
        session.post.return_value = Response(
            {
                "code": 0,
                "data": {
                    "result": {
                        "data": json.dumps(
                            {
                                "code": "phone-code",
                                "encryptedData": "ciphertext",
                                "iv": "iv-value",
                            }
                        )
                    }
                },
            }
        )

        result = get_wechat_phone_auth(session, "http://yyb-go:8000", "account-a")

        self.assertEqual(
            result,
            {"code": "phone-code", "encryptedData": "ciphertext", "iv": "iv-value"},
        )
        url = session.post.call_args.args[0]
        self.assertTrue(url.endswith("/wxapp/getPhoneNumber"))
        self.assertEqual(
            session.post.call_args.kwargs["json"],
            {"ref": "account-a", "app_id": "wx1656f93aeb347dbc"},
        )
        self.assertEqual(session.post.call_args.kwargs["timeout"], 20)
        self.assertEqual(
            session.post.call_args.kwargs["proxies"],
            {"http": None, "https": None},
        )

    def test_get_wechat_code_sends_expected_url_payload_timeout_and_proxy_policy(self):
        session = Mock()
        session.post.return_value = Response(
            {"code": 0, "data": {"result": {"code": "fresh-code"}}}
        )

        code = get_wechat_code(session, "http://yyb-go:8000", "account-a")

        self.assertEqual(code, "fresh-code")
        session.post.assert_called_once_with(
            "http://yyb-go:8000/wxapp/getCode",
            json={"ref": "account-a", "app_id": "wx1656f93aeb347dbc"},
            timeout=20,
            proxies={"http": None, "https": None},
        )

    def test_expired_phone_authorization_does_not_fall_back_to_other_endpoints(self):
        session = Mock()
        session.post.return_value = Response(
            {"code": 409, "msg": "账号已失效，请重新扫码"}, status_code=409
        )

        with self.assertRaises(RuntimeError):
            get_wechat_phone_auth(session, "http://yyb-go:8000", "account-a")

        self.assertEqual(
            session.post.call_count,
            1,
            "HTTP 409 是明确失效状态，不应继续尝试兼容手机号端点",
        )

    def test_legacy_phone_authorization_can_omit_unused_detail_code(self):
        session = Mock()
        session.post.return_value = Response(
            {
                "code": 0,
                "data": {
                    "result": {
                        "data": {
                            "encryptedData": "legacy-ciphertext",
                            "iv": "legacy-iv",
                        }
                    }
                },
            }
        )

        result = get_wechat_phone_auth(session, "http://yyb-go:8000", "account-a")

        self.assertEqual(
            result,
            {"encryptedData": "legacy-ciphertext", "iv": "legacy-iv"},
        )

    def test_run_account_uses_distinct_login_codes_for_guest_phone_and_refresh(self):
        with tempfile.TemporaryDirectory() as td:
            final_path = Path(td) / "yonex-account-a.json"
            calls = []

            class FakeClient:
                def __init__(self, session_path):
                    self.session_path = Path(session_path)
                    self.state = {}

                def login_with_wechat_code(self, code):
                    calls.append(("login", code))
                    if code == "refresh-code":
                        self.state.update(
                            {"token": "member-token", "oid": "oid-1", "is_guest": False}
                        )
                    else:
                        self.state.update({"oid": "oid-1", "is_guest": True})
                    return {"code": 200, "data": dict(self.state)}

                def login_with_phone_code(self, auth):
                    calls.append(("phone", dict(auth)))
                    self.state.update({"token": "member-token", "is_guest": False})
                    return {"code": 200, "data": {"isGuest": False}}

                def verify_session(self):
                    calls.append(("verify",))
                    return {"authenticated": True, "cart_items": 0}

                def save_session(self):
                    self.session_path.parent.mkdir(parents=True, exist_ok=True)
                    self.session_path.write_text(
                        json.dumps(self.state), encoding="utf-8"
                    )

            codes = iter(["guest-code", "phone-login-code", "refresh-code"])
            result = run_account(
                "http://yyb-go:8000",
                "account-a",
                final_path,
                yyb_session=object(),
                tag="main",
                client_factory=FakeClient,
                code_getter=lambda *_args, **_kwargs: next(codes),
                phone_auth_getter=lambda *_args, **_kwargs: {
                    "code": "phone-code",
                    "encryptedData": "ciphertext",
                    "iv": "iv-value",
                },
                output=lambda _line: None,
            )

            self.assertTrue(final_path.exists())
            state = json.loads(final_path.read_text(encoding="utf-8"))
            self.assertEqual(state["ref"], "account-a")
            self.assertEqual(state["tag"], "main")
            self.assertNotIn("phone-code", final_path.read_text(encoding="utf-8"))
            self.assertEqual(calls[0], ("login", "guest-code"))
            self.assertEqual(
                calls[1],
                (
                    "phone",
                    {
                        "code": "phone-login-code",
                        "encryptedData": "ciphertext",
                        "iv": "iv-value",
                    },
                ),
            )
            self.assertEqual(calls[2], ("login", "refresh-code"))
            self.assertEqual(calls[3], ("verify",))
            self.assertEqual(result["ref"], "account-a")
            self.assertNotIn("token", result)

    def test_run_account_does_not_write_any_session_artifact_before_verification(self):
        with tempfile.TemporaryDirectory() as td:
            final_path = Path(td) / "yonex-account-a.json"
            old_session = b'{"token":"old-token"}\n'
            final_path.write_bytes(old_session)
            pending_path = final_path.with_suffix(final_path.suffix + ".pending")
            pending_tmp = pending_path.with_suffix(".tmp")
            observed = []

            def before_verify():
                observed.append(
                    {
                        "final": final_path.read_bytes(),
                        "pending_exists": pending_path.exists(),
                        "temporary_exists": pending_tmp.exists(),
                    }
                )

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
                {
                    "code": 200,
                    "message": "ok",
                    "data": {
                        "isGuest": False,
                        "token": "member-token",
                        "oid": "oid-1",
                    },
                },
                {"code": 200, "message": "ok", "data": []},
                before_verify=before_verify,
            )
            codes = iter(["guest-code", "phone-login-code", "refresh-code"])

            run_account(
                "http://yyb-go:8000",
                "account-a",
                final_path,
                yyb_session=object(),
                client_factory=lambda path: YonexClient(path, http=http),
                code_getter=lambda *_args, **_kwargs: next(codes),
                phone_auth_getter=lambda *_args, **_kwargs: {
                    "encryptedData": "ciphertext",
                    "iv": "iv-value",
                },
                output=lambda _line: None,
            )

            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["final"], old_session)
            self.assertFalse(
                observed[0]["pending_exists"],
                "全部登录步骤和轻量验证完成前不应把中间登录态写入 pending",
            )
            self.assertFalse(observed[0]["temporary_exists"])
            self.assertNotEqual(final_path.read_bytes(), old_session)
            self.assertFalse(pending_path.exists())
            self.assertFalse(pending_tmp.exists())

    def test_verification_failure_preserves_old_session_and_cleans_temporaries(self):
        with tempfile.TemporaryDirectory() as td:
            final_path = Path(td) / "yonex-account-a.json"
            old_session = b'{"token":"known-good-old-token"}\n'
            final_path.write_bytes(old_session)
            pending_path = final_path.with_suffix(final_path.suffix + ".pending")
            pending_tmp = pending_path.with_suffix(".tmp")
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
                {
                    "code": 200,
                    "message": "ok",
                    "data": {
                        "isGuest": False,
                        "token": "member-token",
                        "oid": "oid-1",
                    },
                },
                {"code": 401, "message": "expired", "data": None},
            )
            codes = iter(["guest-code", "phone-login-code", "refresh-code"])

            with self.assertRaises(RuntimeError):
                run_account(
                    "http://yyb-go:8000",
                    "account-a",
                    final_path,
                    yyb_session=object(),
                    client_factory=lambda path: YonexClient(path, http=http),
                    code_getter=lambda *_args, **_kwargs: next(codes),
                    phone_auth_getter=lambda *_args, **_kwargs: {
                        "encryptedData": "ciphertext",
                        "iv": "iv-value",
                    },
                    output=lambda _line: None,
                )

            self.assertEqual(final_path.read_bytes(), old_session)
            self.assertFalse(pending_path.exists())
            self.assertFalse(pending_tmp.exists())

    def test_session_writer_fsyncs_and_applies_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            session_path = Path(td) / "yonex-account-a.json"
            client = YonexClient(session_path)
            client.state.update(
                {"token": "member-token", "oid": "oid-1", "is_guest": False}
            )

            with (
                patch("yonex_client.os.fsync", wraps=os.fsync) as fsync_spy,
                patch("yonex_client.os.chmod", wraps=os.chmod) as chmod_spy,
            ):
                client.save_session()

            self.assertGreaterEqual(
                fsync_spy.call_count,
                1,
                "原子替换前必须 flush 并 fsync 临时 session 文件",
            )
            chmod_spy.assert_any_call(ANY, 0o600)

    def test_session_temporary_file_is_created_owner_only_before_first_write(self):
        with tempfile.TemporaryDirectory() as td:
            session_path = Path(td) / "yonex-account-a.json"
            temporary_path = session_path.with_suffix(".tmp")
            client = YonexClient(session_path)
            client.state.update(
                {"token": "member-token", "oid": "oid-1", "is_guest": False}
            )

            with patch("yonex_client.os.open", wraps=os.open) as open_spy:
                client.save_session()

            temporary_creations = [
                call
                for call in open_spy.call_args_list
                if call.args and Path(call.args[0]) == temporary_path
            ]
            self.assertTrue(
                temporary_creations,
                "临时 session 必须通过可指定创建权限的 os.open 创建",
            )
            creation = temporary_creations[0]
            flags = creation.args[1]
            mode = (
                creation.args[2]
                if len(creation.args) >= 3
                else creation.kwargs.get("mode")
            )
            self.assertTrue(flags & os.O_CREAT)
            self.assertEqual(mode, 0o600)

    def test_session_writer_closes_descriptor_when_fdopen_fails(self):
        with tempfile.TemporaryDirectory() as td:
            session_path = Path(td) / "yonex-account-a.json"
            temporary_path = session_path.with_suffix(".tmp")
            client = YonexClient(session_path)
            client.state.update(
                {"token": "member-token", "oid": "oid-1", "is_guest": False}
            )
            descriptors = []
            real_open = os.open

            def capture_open(path, flags, mode=0o777):
                descriptor = real_open(path, flags, mode)
                if Path(path) == temporary_path:
                    descriptors.append(descriptor)
                return descriptor

            with (
                patch("yonex_client.os.open", side_effect=capture_open),
                patch(
                    "yonex_client.os.fdopen",
                    side_effect=OSError("injected fdopen failure"),
                ),
            ):
                with self.assertRaises(OSError):
                    client.save_session()

            self.assertEqual(len(descriptors), 1)
            descriptor = descriptors[0]
            try:
                try:
                    os.fstat(descriptor)
                except OSError:
                    descriptor_closed = True
                else:
                    descriptor_closed = False
                temporary_exists = temporary_path.exists()
            finally:
                if not descriptor_closed:
                    os.close(descriptor)
                temporary_path.unlink(missing_ok=True)

            self.assertEqual(
                {
                    "descriptor_closed": descriptor_closed,
                    "temporary_exists": temporary_exists,
                },
                {"descriptor_closed": True, "temporary_exists": False},
            )

    def test_account_session_path_sanitizes_ref(self):
        first = account_session_path(Path("sessions"), "account/a")
        second = account_session_path(Path("sessions"), "account?a")
        self.assertEqual(first.parent, Path("sessions"))
        self.assertEqual(second.parent, Path("sessions"))
        self.assertNotEqual(first, second)
        self.assertNotIn("/", first.name)
        self.assertTrue(first.name.startswith("yonex-account_a-"))

    def test_any_failed_account_makes_the_qinglong_task_fail(self):
        self.assertEqual(task_exit_code(2, 0), 0)
        self.assertEqual(task_exit_code(1, 1), 1)
        self.assertEqual(task_exit_code(0, 2), 1)

    def test_main_uses_qinglong_persistent_session_directory_by_default(self):
        captured_paths = []

        def fake_run_account(_server, _ref, session_path, _session, **_kwargs):
            captured_paths.append(Path(session_path))
            return {"authenticated": True}

        with (
            patch.dict(
                os.environ,
                {"YYB_SERVER": "http://yyb-go:8000@account-a#main"},
                clear=True,
            ),
            patch.object(ql_login.requests, "Session", return_value=Mock()),
            patch.object(ql_login, "run_account", side_effect=fake_run_account),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = ql_login.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(captured_paths), 1)
        self.assertEqual(
            captured_paths[0].parent,
            Path("/ql/data/config/yonex-sessions"),
        )

    def test_main_redacts_secret_sentinel_from_account_failure_log(self):
        secret = "QL_SECRET_SENTINEL_9f3d7c2a"
        stdout = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {"YYB_SERVER": "http://yyb-go:8000@account-a#main"},
                clear=True,
            ),
            patch.object(ql_login.requests, "Session", return_value=Mock()),
            patch.object(
                ql_login,
                "run_account",
                side_effect=RuntimeError(f"upstream token={secret}"),
            ),
            redirect_stdout(stdout),
        ):
            exit_code = ql_login.main()

        self.assertEqual(exit_code, 1)
        self.assertNotIn(secret, stdout.getvalue())

    def test_safe_error_text_drops_secrets_in_untrusted_upstream_formats(self):
        secret = "QL_SECRET_SENTINEL_f41a8c7e"
        cases = {
            "quoted-json-token": f'upstream {{"token":"{secret}"}}',
            "access-token": f"upstream access_token={secret}",
            "encrypted-data": f"upstream encrypted_data={secret}",
            "bare-jwt": f"eyJhbGciOiJIUzI1NiJ9.{secret}.signature",
            "userinfo-url": f"https://ql-user:{secret}@yyb.example/wxapp/getCode",
            "unlabelled-secret": f"upstream rejected material {secret}",
        }

        for name, message in cases.items():
            with self.subTest(name=name):
                sanitized = ql_login.safe_error_text(RuntimeError(message))
                self.assertNotIn(secret, sanitized)

    def test_safe_error_text_does_not_echo_long_numeric_api_code(self):
        numeric_secret = "13800138000" * 5
        error = YonexAPIError("/cart/list", numeric_secret, "upstream failure")

        sanitized = ql_login.safe_error_text(error)

        self.assertNotIn(numeric_secret, sanitized)

    def test_main_continues_other_accounts_and_returns_failure_if_any_failed(self):
        attempted_refs = []

        def fake_run_account(_server, ref, _path, _session, **_kwargs):
            attempted_refs.append(ref)
            if ref == "account-a":
                raise RuntimeError("expected test failure")
            return {"authenticated": True}

        stdout = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "YYB_SERVER": (
                        "http://yyb-go:8000@account-a#main\n"
                        "http://yyb-go:8000@account-b#main"
                    ),
                    "YONEX_SESSION_DIR": tempfile.gettempdir(),
                },
                clear=True,
            ),
            patch.object(ql_login.requests, "Session", return_value=Mock()),
            patch.object(ql_login, "run_account", side_effect=fake_run_account),
            redirect_stdout(stdout),
        ):
            exit_code = ql_login.main()

        self.assertEqual(attempted_refs, ["account-a", "account-b"])
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
