import importlib.util
import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(
    os.getenv(
        "GENERIC_API_MODULE",
        str(Path(__file__).resolve().parents[1] / "session_api_server_generic.py"),
    )
)

RENEWAL_OVERRIDES = {
    "TOPPS_RENEWAL_ENTRY": "/ql/data/scripts/topps/scripts/python/topps_ql_login.py",
    "WDNGM_RENEWAL_ENTRY": "/ql/data/scripts/wdngm/tools/python/wdngm_ql_login.py",
    "LN_RENEWAL_ENTRY": "/ql/data/scripts/ln/scripts/python/ln_ql_login.py",
    "YONEX_RENEWAL_ENTRY": "/ql/data/scripts/yonex/yonex_ql_login.py",
}


def load_module():
    spec = importlib.util.spec_from_file_location("session_api_server_generic", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def start_server(testcase, module, apis, *, no_auth):
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    server.apis = apis
    server.no_auth = no_auth
    server.openapi = lambda: module.build_openapi(apis)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    testcase.addCleanup(server.server_close)
    testcase.addCleanup(thread.join, 2)
    testcase.addCleanup(server.shutdown)
    return server


def request(server, method, path, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class LiNingProjectTests(unittest.TestCase):
    def test_project_specs_registers_ln_with_canonical_login_entry(self):
        module = load_module()

        def fake_entry(env_name, project):
            return Path(f"/resolved/{env_name}/{project}.py")

        with patch.dict(os.environ, {"LN_SESSION_DIR": "/sessions/ln"}):
            if hasattr(module, "renewal_entry"):
                with patch.object(module, "renewal_entry", side_effect=fake_entry):
                    spec = module.project_specs()["ln"]
                expected_renewal = Path("/resolved/LN_RENEWAL_ENTRY/lining.py")
            else:
                spec = module.project_specs()["ln"]
                expected_renewal = Path(
                    "/ql/data/scripts/ln/scripts/python/ln_ql_login.py"
                )

        self.assertEqual(spec.display_name, "李宁")
        self.assertEqual(spec.session_dir, Path("/sessions/ln"))
        self.assertEqual(spec.token_env, "LN_API_TOKEN")
        self.assertEqual(spec.renewal, expected_renewal)

    def test_ln_accounts_use_ln_files_and_never_include_login_secrets(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = {
                "appId": "wx-test",
                "saasId": "lining",
                "createdAt": "2026-08-31T01:59:00Z",
                "updatedAt": "2026-08-31T02:00:00Z",
                "loginMode": "wx_code",
                "channel": "wx",
                "ref": "1",
                "tag": "main",
                "authTokenVO": {
                    "authToken": "secret-auth-token",
                    "expireTime": "4102444800000",
                },
                "userCoreVO": {"phone": "secret-phone", "nickname": "secret-name"},
                "wxOpenid": "secret-openid",
            }
            (root / "ln-1.json").write_text(
                json.dumps(session, ensure_ascii=False),
                encoding="utf-8",
            )
            spec = module.ProjectSpec(
                "ln",
                "李宁",
                root,
                "LN_API_TOKEN",
                root / "token.txt",
                root / "ln_ql_login.py",
                root / "refresh.log",
                tuple(session),
                "updated_at",
            )
            api = module.ProjectAPI(spec)

            self.assertEqual(api.session("1")["authTokenVO"]["authToken"], "secret-auth-token")
            page = api.accounts(pageindex=1, pagesize=50)

        self.assertEqual(page["totalcount"], 1)
        account = page["accounts"][0]
        self.assertEqual(account["ref"], "1")
        self.assertEqual(account["tag"], "main")
        self.assertEqual(account["app_id"], "wx-test")
        self.assertEqual(account["channel"], "wx")
        self.assertEqual(account["login_mode"], "wx_code")
        self.assertIsInstance(account["updated_at"], int)
        self.assertEqual(account["expires_at"], 4102444800)
        self.assertFalse(account["expired"])
        serialized = json.dumps(account, ensure_ascii=False)
        self.assertNotIn("secret-auth-token", serialized)
        self.assertNotIn("secret-phone", serialized)
        self.assertNotIn("secret-name", serialized)
        self.assertNotIn("secret-openid", serialized)

    def test_ln_refresh_uses_ln_filter_environment(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            renewal = root / "scripts" / "python" / "ln_ql_login.py"
            renewal.parent.mkdir(parents=True)
            renewal.write_text("print('unused')\n", encoding="utf-8")
            spec = module.ProjectSpec(
                "ln",
                "李宁",
                root,
                "LN_API_TOKEN",
                root / "token.txt",
                renewal,
                root / "refresh.log",
                ("authTokenVO",),
                "updated_at",
            )

            with patch.object(module.subprocess, "Popen") as popen:
                accepted = module.ProjectAPI(spec).refresh("1", "main")

        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment["LN_REFRESH_REF"], "1")
        self.assertEqual(environment["LN_REFRESH_TAG"], "main")
        self.assertNotIn("LINING_REFRESH_REF", environment)
        self.assertEqual(accepted["scope"], "ref")


class YonexProjectTests(unittest.TestCase):
    def test_project_specs_registers_yonex_with_deployment_login_entry(self):
        module = load_module()
        with patch.dict(
            os.environ,
            {**RENEWAL_OVERRIDES, "YONEX_SESSION_DIR": "/sessions/yonex"},
            clear=False,
        ):
            specs = module.project_specs()

        self.assertIn("yonex", specs)
        spec = specs["yonex"]
        self.assertEqual(spec.display_name, "YONEX")
        self.assertEqual(spec.session_dir, Path("/sessions/yonex"))
        self.assertEqual(spec.token_env, "YONEX_API_TOKEN")
        self.assertEqual(
            spec.token_file,
            Path("/ql/data/config/yonex_api_token.txt"),
        )
        self.assertEqual(
            spec.renewal,
            Path(RENEWAL_OVERRIDES["YONEX_RENEWAL_ENTRY"]),
        )
        self.assertTrue(getattr(spec, "force_bearer_auth", False))

    def test_yonex_hashed_session_is_selected_by_exact_payload_ref(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ref = "account/a"
            digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:12]
            session = {
                "ref": ref,
                "tag": "main",
                "token": "member-token",
                "oid": "member-oid",
                "app_id": "wx-test",
                "is_guest": False,
                "updated_at": 1_788_140_000,
            }
            (root / f"yonex-account_a-{digest}.json").write_text(
                json.dumps(session),
                encoding="utf-8",
            )
            (root / "yonex-decoy-deadbeef0000.json").write_text(
                json.dumps({**session, "ref": "account/b", "token": "decoy"}),
                encoding="utf-8",
            )
            spec = module.ProjectSpec(
                "yonex",
                "YONEX",
                root,
                "YONEX_API_TOKEN",
                root / "token.txt",
                root / "yonex_ql_login.py",
                root / "refresh.log",
                tuple(session),
                "updated_at",
            )

            selected = module.ProjectAPI(spec).session(ref)

        self.assertIsNotNone(selected)
        self.assertEqual(selected["ref"], ref)
        self.assertEqual(selected["token"], "member-token")

    def test_yonex_session_never_uses_untrusted_ref_as_a_path(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "sessions"
            root.mkdir()
            (root / "yonex-a").mkdir()
            unsafe_ref = "a/../../escape"
            (base / "escape.json").write_text(
                json.dumps({"ref": unsafe_ref, "token": "outside-token"}),
                encoding="utf-8",
            )
            spec = module.ProjectSpec(
                "yonex",
                "YONEX",
                root,
                "YONEX_API_TOKEN",
                root / "token.txt",
                root / "yonex_ql_login.py",
                root / "refresh.log",
                ("token", "ref"),
                "updated_at",
            )

            selected = module.ProjectAPI(spec).session(unsafe_ref)

        self.assertIsNone(selected)

    def test_yonex_accounts_expose_auth_state_without_login_secrets(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = {
                "ref": "account-a",
                "tag": "main",
                "token": "member-token",
                "oid": "member-oid",
                "app_id": "wx-test",
                "is_guest": False,
                "updated_at": 1_788_140_000,
            }
            (root / "yonex-account_a-deadbeef0000.json").write_text(
                json.dumps(session),
                encoding="utf-8",
            )
            spec = module.ProjectSpec(
                "yonex",
                "YONEX",
                root,
                "YONEX_API_TOKEN",
                root / "token.txt",
                root / "yonex_ql_login.py",
                root / "refresh.log",
                tuple(session),
                "updated_at",
            )

            account = module.ProjectAPI(spec).accounts()["accounts"][0]

        self.assertEqual(account["ref"], "account-a")
        self.assertEqual(account["app_id"], "wx-test")
        self.assertTrue(account["authenticated"])
        self.assertFalse(account["is_guest"])
        self.assertTrue(account["token_present"])
        self.assertTrue(account["oid_present"])
        serialized = json.dumps(account)
        self.assertNotIn("member-token", serialized)
        self.assertNotIn("member-oid", serialized)

    def test_yonex_refresh_uses_yonex_filter_environment(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            renewal = root / "yonex" / "yonex_ql_login.py"
            renewal.parent.mkdir(parents=True)
            renewal.write_text("print('unused')\n", encoding="utf-8")
            spec = module.ProjectSpec(
                "yonex",
                "YONEX",
                root,
                "YONEX_API_TOKEN",
                root / "token.txt",
                renewal,
                root / "refresh.log",
                ("token",),
                "updated_at",
            )

            with patch.object(module.subprocess, "Popen") as popen:
                accepted = module.ProjectAPI(spec).refresh("account/a", "main")

        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment["YONEX_REFRESH_REF"], "account/a")
        self.assertEqual(environment["YONEX_REFRESH_TAG"], "main")
        self.assertEqual(accepted["scope"], "ref")

    def test_yonex_requires_bearer_even_when_server_is_no_auth(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ref = "account/a"
            digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:12]
            session = {
                "ref": ref,
                "token": "member-token",
                "oid": "member-oid",
                "app_id": "wx-test",
                "is_guest": False,
            }
            (root / f"yonex-account_a-{digest}.json").write_text(
                json.dumps(session),
                encoding="utf-8",
            )
            token_file = root / "api-token.txt"
            token_file.write_text("fixture-bearer", encoding="utf-8")
            spec = module.ProjectSpec(
                "yonex",
                "YONEX",
                root,
                "GENERIC_API_TEST_YONEX_TOKEN",
                token_file,
                root / "yonex_ql_login.py",
                root / "refresh.log",
                tuple(session),
                "updated_at",
            )
            object.__setattr__(spec, "force_bearer_auth", True)
            server = start_server(
                self,
                module,
                {"yonex": module.ProjectAPI(spec)},
                no_auth=True,
            )

            status_without_auth, _ = request(server, "GET", "/yonex/accounts")
            status_with_query, _ = request(
                server,
                "GET",
                "/yonex/accounts?token=fixture-bearer",
            )
            status_accounts, accounts_body = request(
                server,
                "GET",
                "/yonex/accounts",
                {"Authorization": "Bearer fixture-bearer"},
            )
            status_session, session_body = request(
                server,
                "GET",
                "/yonex/session?ref=account%2Fa",
                {"Authorization": "Bearer fixture-bearer"},
            )

        self.assertEqual(status_without_auth, 401)
        self.assertEqual(status_with_query, 401)
        self.assertEqual(status_accounts, 200)
        accounts = json.loads(accounts_body)["data"]["accounts"]
        self.assertEqual(accounts[0]["ref"], ref)
        self.assertNotIn("member-token", json.dumps(accounts))
        self.assertEqual(status_session, 200)
        self.assertEqual(json.loads(session_body)["data"]["ref"], ref)

    def test_no_auth_behavior_is_unchanged_for_legacy_projects(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = module.ProjectSpec(
                "topps",
                "Topps",
                root,
                "TOPPS_API_TOKEN",
                root / "token.txt",
                root / "topps_ql_login.py",
                root / "refresh.log",
                ("token",),
                "updated_at",
            )
            server = start_server(
                self,
                module,
                {"topps": module.ProjectAPI(spec)},
                no_auth=True,
            )

            status, body = request(server, "GET", "/topps/accounts")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["code"], 0)

    def test_registered_yonex_is_visible_in_projects_docs_and_openapi(self):
        module = load_module()
        with patch.dict(os.environ, RENEWAL_OVERRIDES, clear=False):
            apis = {
                name: module.ProjectAPI(spec)
                for name, spec in module.project_specs().items()
            }
        server = start_server(self, module, apis, no_auth=True)

        projects_status, projects_body = request(server, "GET", "/projects")
        docs_status, docs_body = request(server, "GET", "/docs")
        openapi_status, openapi_body = request(server, "GET", "/openapi.json")

        self.assertEqual(projects_status, 200)
        projects = {
            item["project"] for item in json.loads(projects_body)["data"]
        }
        self.assertIn("yonex", projects)
        self.assertEqual(docs_status, 200)
        docs = docs_body.decode("utf-8")
        for endpoint in ("accounts", "session", "account", "refresh"):
            self.assertIn(f"/yonex/{endpoint}", docs)
        self.assertEqual(openapi_status, 200)
        paths = json.loads(openapi_body)["paths"]
        for endpoint in ("accounts", "session", "account", "refresh"):
            self.assertIn(f"/yonex/{endpoint}", paths)


if __name__ == "__main__":
    unittest.main()
