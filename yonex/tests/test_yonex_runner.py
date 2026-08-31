import argparse
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.python.yonex_order import redact_sensitive, validate_create_request  # noqa: E402


class YonexRunnerTests(unittest.TestCase):
    def test_redaction_covers_tokens_payment_signatures_and_receiver_data(self):
        value = {
            "token": "secret",
            "Authorization": "Bearer secret",
            "receiverPhone": "13000000000",
            "receiverDetailAddress": "street",
            "paySign": "signature",
            "nested": {"name": "product", "orderSn": "ORDER-1"},
        }
        safe = redact_sensitive(value)
        text = json.dumps(safe)
        self.assertNotIn("secret", text)
        self.assertNotIn("13000000000", text)
        self.assertNotIn("street", text)
        self.assertNotIn("signature", text)
        self.assertEqual(safe["nested"]["orderSn"], "ORDER-1")

    def test_create_order_requires_flag_ack_and_interactive_confirmation(self):
        args = argparse.Namespace(create_order=False, ack_create="")
        self.assertFalse(
            validate_create_request(args, input_fn=lambda _prompt: "CREATE")
        )

        args = argparse.Namespace(create_order=True, ack_create="wrong")
        with self.assertRaises(ValueError):
            validate_create_request(args, input_fn=lambda _prompt: "CREATE")

        args = argparse.Namespace(create_order=True, ack_create="CREATE_UNPAID_ORDER")
        with self.assertRaises(ValueError):
            validate_create_request(args, input_fn=lambda _prompt: "NO")
        self.assertTrue(
            validate_create_request(args, input_fn=lambda _prompt: "CREATE")
        )


if __name__ == "__main__":
    unittest.main()
