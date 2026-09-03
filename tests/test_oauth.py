"""The Dev Dashboard token exchange, driven against a fake callback."""

from __future__ import annotations

import hashlib
import hmac
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from woo2shopify import oauth  # noqa: E402

SHOP = "demo-store.myshopify.com"
CLIENT_ID = "eb43e61e71bf2e0ff5b898f6f42ba845"
SECRET = "shpss_testsecret"
PORT = 3789


def signed_callback(secret: str, state: str, code: str = "authcode123") -> str:
    params = {"code": code, "shop": SHOP, "state": state, "timestamp": "1757000000"}
    message = "&".join(sorted(f"{k}={v}" for k, v in params.items()))
    params["hmac"] = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"http://localhost:{PORT}{oauth.CALLBACK_PATH}?" + urlencode(params)


class FakeResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"access_token": "shpat_abc123", "scope": oauth.DEFAULT_SCOPES}


class OAuthTest(unittest.TestCase):
    def _run_flow(self, callback_secret: str):
        """Start the flow, answer its callback, return (result, error)."""
        outcome = {}

        def worker():
            try:
                outcome["result"] = oauth.fetch_offline_token(
                    SHOP, CLIENT_ID, SECRET, port=PORT, timeout=15, open_browser=False
                )
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and not oauth._CallbackHandler.expected_state:
            time.sleep(0.02)
        state = oauth._CallbackHandler.expected_state
        response = requests.get(signed_callback(callback_secret, state), timeout=5)
        thread.join(timeout=15)
        return outcome, response

    def setUp(self):
        oauth._CallbackHandler.expected_state = ""

    def test_happy_path_returns_token(self):
        with mock.patch.object(oauth.requests, "post", return_value=FakeResponse()) as post:
            outcome, response = self._run_flow(SECRET)
        self.assertNotIn("error", outcome, str(outcome.get("error")))
        self.assertEqual(outcome["result"]["access_token"], "shpat_abc123")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Connected", response.text)
        url, kwargs = post.call_args[0][0], post.call_args[1]
        self.assertEqual(url, f"https://{SHOP}/admin/oauth/access_token")
        self.assertEqual(kwargs["json"]["code"], "authcode123")
        self.assertEqual(kwargs["json"]["client_secret"], SECRET)

    def test_wrong_secret_is_rejected(self):
        outcome, response = self._run_flow("the-wrong-secret")
        self.assertIn("error", outcome)
        self.assertIn("HMAC", str(outcome["error"]))
        self.assertEqual(response.status_code, 400)

    def test_non_myshopify_domain_is_refused(self):
        with self.assertRaises(oauth.OAuthError) as ctx:
            oauth.fetch_offline_token("mystore.com", CLIENT_ID, SECRET, open_browser=False)
        self.assertIn("myshopify", str(ctx.exception))

    def test_redirect_uri_shape(self):
        self.assertEqual(oauth.redirect_uri(3456), "http://localhost:3456/callback")


class AuthErrorMessageTest(unittest.TestCase):
    def test_401_explains_the_token_mix_up(self):
        from woo2shopify.shopify_api import _describe_http_error

        message = _describe_http_error(401, '{"errors":"[API] Invalid API key or access token"}')
        self.assertIn("shpat_", message)
        self.assertIn("Develop apps", message)
        self.assertIn("oauth", message)
        self.assertNotIn("shpat_", _describe_http_error(500, "boom"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
