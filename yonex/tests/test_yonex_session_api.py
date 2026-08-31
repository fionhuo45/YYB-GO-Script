import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.python.yonex_session_api import (  # noqa: E402
    API,
    Handler,
    load_token,
    masked_summary,
)


class YonexSessionAPITests(unittest.TestCase):
    def test_masked_accounts_never_return_token_or_oid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "yonex-account-a.json").write_text(
                json.dumps(
                    {
                        "token": "secret-token",
                        "oid": "secret-oid",
                        "ref": "account-a",
                        "tag": "main",
                        "app_id": "wx1656f93aeb347dbc",
                        "is_guest": False,
                        "updated_at": 1,
                    }
                ),
                encoding="utf-8",
            )
            api = API(root, "api-token", Path("renew.py"), root / "renew.log")

            result = api.accounts(tag="main")

            self.assertEqual(result["totalcount"], 1)
            summary = result["accounts"][0]
            self.assertEqual(summary["ref"], "account-a")
            self.assertTrue(summary["authenticated"])
            self.assertNotIn("token", summary)
            self.assertNotIn("oid", summary)
            self.assertNotIn("secret", json.dumps(summary))
            self.assertNotIn("token", masked_summary({"token": "secret"}))

    def test_full_session_lookup_requires_safe_ref(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "yonex-a.json").write_text('{"token":"secret"}', encoding="utf-8")
            api = API(root, "api-token", Path("renew.py"), root / "renew.log")
            self.assertEqual(api.session("a")["token"], "secret")
            self.assertIsNone(api.session("../a"))
            self.assertIsNone(api.session(""))

    def test_session_lookup_uses_original_ref_from_payload_without_path_joining(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "yonex-account_a-deadbeef.json").write_text(
                '{"ref":"account/a","token":"secret"}', encoding="utf-8"
            )
            api = API(root, "api-token", Path("renew.py"), root / "renew.log")
            self.assertEqual(api.session("account/a")["token"], "secret")

    def test_query_string_token_is_not_accepted_for_sidecar_auth(self):
        api = API(Path("sessions"), "api-token", Path("renew.py"), Path("renew.log"))
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(api=api)
        handler.path = "/yonex/session?ref=a&token=api-token"
        handler.headers = {}
        self.assertFalse(handler._auth())
        handler.headers = {"Authorization": "Bearer api-token"}
        self.assertTrue(handler._auth())

    def test_load_token_prefers_environment_then_file(self):
        with tempfile.TemporaryDirectory() as td:
            token_file = Path(td) / "token.txt"
            token_file.write_text("file-token\n", encoding="utf-8")
            with patch.dict(os.environ, {"YONEX_API_TOKEN": "env-token"}, clear=False):
                self.assertEqual(load_token(token_file, "YONEX_API_TOKEN"), "env-token")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    load_token(token_file, "YONEX_API_TOKEN"), "file-token"
                )

    def test_openapi_lists_yonex_routes(self):
        api = API(Path("sessions"), "api-token", Path("renew.py"), Path("renew.log"))
        paths = api.openapi()["paths"]
        self.assertIn("/yonex/accounts", paths)
        self.assertIn("/yonex/session", paths)
        self.assertIn("/yonex/refresh", paths)


if __name__ == "__main__":
    unittest.main()
