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


class ClientCredentialsTest(unittest.TestCase):
    """The grant Dev Dashboard apps actually use: one POST, 24-hour token."""

    def _config(self):
        from woo2shopify.config import ShopifyConfig

        return ShopifyConfig(
            shop_domain=SHOP, client_id=CLIENT_ID, client_secret=SECRET,
            auth_mode="client_credentials",
        )

    def test_posts_the_documented_form_body(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"access_token": "tok_1", "scope": "write_orders", "expires_in": 86399}
        with mock.patch.object(oauth.requests, "post", return_value=response) as post:
            result = oauth.fetch_client_credentials_token(SHOP, CLIENT_ID, SECRET)
        self.assertEqual(result["access_token"], "tok_1")
        self.assertEqual(result["scope"], "write_orders")
        self.assertEqual(result["expires_in"], 86399)
        args, kwargs = post.call_args
        self.assertEqual(args[0], f"https://{SHOP}/admin/oauth/access_token")
        self.assertEqual(kwargs["data"], {
            "grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": SECRET,
        })
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/x-www-form-urlencoded")

    def test_accepts_a_bare_store_handle(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"access_token": "tok_1", "scope": "", "expires_in": 86399}
        with mock.patch.object(oauth.requests, "post", return_value=response) as post:
            oauth.fetch_client_credentials_token("xzpcy1-7w", CLIENT_ID, SECRET)
        self.assertEqual(post.call_args[0][0], "https://xzpcy1-7w.myshopify.com/admin/oauth/access_token")

    def test_admin_url_is_explained_not_silently_wrong(self):
        with self.assertRaises(oauth.OAuthError) as ctx:
            oauth.normalize_shop("admin.shopify.com/store/xzpcy1-7w")
        self.assertIn("myshopify", str(ctx.exception))

    def test_refusal_explains_the_same_org_requirement(self):
        response = mock.Mock(status_code=400, text='{"error":"invalid_client"}')
        with mock.patch.object(oauth.requests, "post", return_value=response):
            with self.assertRaises(oauth.OAuthError) as ctx:
                oauth.fetch_client_credentials_token(SHOP, CLIENT_ID, SECRET)
        message = str(ctx.exception)
        self.assertIn("same Shopify organization", message)
        self.assertIn("installed on this store", message)

    def test_token_source_caches_then_re_mints_before_expiry(self):
        calls = []

        def fake_fetch(shop, cid, secret, log=None):
            calls.append(shop)
            return {"access_token": f"tok_{len(calls)}", "scope": "write_orders", "expires_in": 86399}

        source = oauth.TokenSource(self._config())
        with mock.patch.object(oauth, "fetch_client_credentials_token", side_effect=fake_fetch):
            self.assertEqual(source.token(), "tok_1")
            self.assertEqual(source.token(), "tok_1")      # cached, no second request
            self.assertEqual(len(calls), 1)
            source._minted_expires_at = 0                   # pretend 24h went by
            self.assertEqual(source.token(), "tok_2")
            self.assertEqual(source.token(force=True), "tok_3")
        self.assertEqual(len(calls), 3)

    def test_token_source_passes_through_a_pasted_token(self):
        from woo2shopify.config import ShopifyConfig

        source = oauth.TokenSource(ShopifyConfig(shop_domain=SHOP, access_token="shpat_pasted", auth_mode="token"))
        self.assertFalse(source.can_refresh)
        self.assertEqual(source.token(), "shpat_pasted")

    def test_missing_credentials_are_reported_not_crashed(self):
        from woo2shopify.config import ShopifyConfig

        source = oauth.TokenSource(ShopifyConfig(shop_domain=SHOP, auth_mode="client_credentials"))
        with self.assertRaises(oauth.OAuthError) as ctx:
            source.token()
        self.assertIn("No Shopify Admin API token", str(ctx.exception))


class RefreshTokenTest(unittest.TestCase):
    """The authorization code grant now returns a 1-hour token plus a refresh token."""

    def _config(self, **overrides):
        from woo2shopify.config import ShopifyConfig

        base = dict(shop_domain=SHOP, client_id=CLIENT_ID, client_secret=SECRET,
                    auth_mode="token", access_token="old_token", refresh_token="shprt_x")
        base.update(overrides)
        return ShopifyConfig(**base)

    def test_apply_grant_records_both_expiries(self):
        cfg = self._config(access_token="", refresh_token="")
        oauth.apply_grant(cfg, {
            "access_token": "new_token", "scope": "write_orders", "expires_in": 3600,
            "refresh_token": "shprt_new", "refresh_token_expires_in": 7776000,
        })
        self.assertEqual(cfg.access_token, "new_token")
        self.assertEqual(cfg.refresh_token, "shprt_new")
        self.assertAlmostEqual(cfg.token_expires_at - time.time(), 3600, delta=5)
        self.assertAlmostEqual(cfg.refresh_token_expires_at - time.time(), 7776000, delta=5)

    def test_apply_grant_marks_a_permanent_token_as_never_expiring(self):
        cfg = self._config(access_token="", refresh_token="")
        oauth.apply_grant(cfg, {"access_token": "forever", "scope": "", "expires_in": 0})
        self.assertEqual(cfg.token_expires_at, 0.0)
        self.assertEqual(cfg.refresh_token, "")

    def test_refresh_request_body(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"access_token": "t2", "scope": "write_orders",
                                      "expires_in": 3600, "refresh_token": "shprt_2",
                                      "refresh_token_expires_in": 7776000}
        with mock.patch.object(oauth.requests, "post", return_value=response) as post:
            oauth.refresh_access_token(SHOP, CLIENT_ID, SECRET, "shprt_1")
        self.assertEqual(post.call_args[1]["data"], {
            "client_id": CLIENT_ID, "client_secret": SECRET,
            "grant_type": "refresh_token", "refresh_token": "shprt_1",
        })

    def test_expiring_token_is_refreshed_and_persisted(self):
        cfg = self._config(token_expires_at=time.time() + 10)   # about to lapse
        saved = []
        source = oauth.TokenSource(cfg, save=lambda: saved.append(cfg.access_token))
        grant = {"access_token": "fresh", "scope": "write_orders", "expires_in": 3600,
                 "refresh_token": "shprt_rotated", "refresh_token_expires_in": 7776000}
        with mock.patch.object(oauth, "refresh_access_token", return_value=grant) as refresh:
            self.assertEqual(source.token(), "fresh")
            self.assertEqual(source.token(), "fresh")     # now cached
        refresh.assert_called_once()
        self.assertEqual(cfg.refresh_token, "shprt_rotated", "a rotated refresh token must be kept")
        self.assertEqual(saved, ["fresh"], "the new token must be written to the config file")

    def test_valid_token_is_not_refreshed(self):
        cfg = self._config(token_expires_at=time.time() + 3600)
        source = oauth.TokenSource(cfg)
        with mock.patch.object(oauth, "refresh_access_token") as refresh:
            self.assertEqual(source.token(), "old_token")
        refresh.assert_not_called()

    def test_permanent_token_with_refresh_token_is_reused(self):
        cfg = self._config(token_expires_at=0.0)
        source = oauth.TokenSource(cfg)
        with mock.patch.object(oauth, "refresh_access_token") as refresh:
            self.assertEqual(source.token(), "old_token")
        refresh.assert_not_called()

    def test_lapsed_refresh_token_says_to_redo_oauth(self):
        cfg = self._config(token_expires_at=time.time() - 1,
                           refresh_token_expires_at=time.time() - 1)
        source = oauth.TokenSource(cfg)
        with self.assertRaises(oauth.OAuthError) as ctx:
            source.token()
        self.assertIn("90 days", str(ctx.exception))

    def test_expired_token_without_refresh_token_is_reported(self):
        cfg = self._config(refresh_token="", token_expires_at=time.time() - 1)
        source = oauth.TokenSource(cfg)
        self.assertFalse(source.can_refresh)
        with self.assertRaises(oauth.OAuthError) as ctx:
            source.token()
        self.assertIn("expired", str(ctx.exception))

    def test_401_forces_a_refresh_in_this_mode_too(self):
        from woo2shopify.shopify_api import ShopifyClient

        cfg = self._config(token_expires_at=time.time() + 3600)
        client = ShopifyClient(cfg, max_retries=3)
        grant = {"access_token": "after_401", "scope": "", "expires_in": 3600,
                 "refresh_token": "shprt_x", "refresh_token_expires_in": 7776000}
        expired = mock.Mock(status_code=401, text='{"errors":"[API] Invalid API key or access token"}')
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"data": {"shop": {"name": "Test Store"}}, "extensions": {}}
        sent = []

        def fake_post(url, **kwargs):
            sent.append(client.session.headers["X-Shopify-Access-Token"])
            return expired if len(sent) == 1 else ok

        with mock.patch.object(oauth, "refresh_access_token", return_value=grant), \
             mock.patch.object(client.session, "post", side_effect=fake_post):
            client.graphql("{ shop { name } }")
        self.assertEqual(sent, ["old_token", "after_401"])


class GrantErrorTest(unittest.TestCase):
    def test_shop_not_permitted_is_explained_concretely(self):
        html = ('<!DOCTYPE html><html><head><title>400 - Oauth error shop_not_permitted'
                '</title></head><body>x</body></html>')
        response = mock.Mock(status_code=400, text=html)
        with mock.patch.object(oauth.requests, "post", return_value=response):
            with self.assertRaises(oauth.OAuthError) as ctx:
                oauth.fetch_client_credentials_token(SHOP, CLIENT_ID, SECRET)
        message = str(ctx.exception)
        self.assertIn("shop_not_permitted", message)
        self.assertIn("development stores", message)
        self.assertIn("Custom distribution", message)
        self.assertNotIn("<!DOCTYPE", message, "the HTML page must not be dumped at the user")

    def test_html_bodies_are_reduced_to_their_title(self):
        self.assertEqual(
            oauth.clean_error_body('<!DOCTYPE html><html><head><title>400 - Oauth error x</title></head>'),
            "400 - Oauth error x",
        )
        self.assertEqual(oauth.clean_error_body('{"error":"invalid_client"}'), '{"error":"invalid_client"}')


class WrongCredentialTest(unittest.TestCase):
    """Shopify answers every wrong credential type with the same opaque 401."""

    def test_automation_token_is_named(self):
        problem = oauth.describe_token_problem("atkn_9892da635306f1d23c1ccd04005")
        self.assertIn("App Automation Token", problem)
        self.assertIn("cannot call the Admin API", problem)

    def test_client_secret_is_named(self):
        self.assertIn("Client secret", oauth.describe_token_problem("shpss_64e4eef8669"))

    def test_plausible_tokens_pass(self):
        # client-credentials tokens are bare hex, so only known prefixes may be flagged
        self.assertEqual(oauth.describe_token_problem("shpat_realtoken"), "")
        self.assertEqual(oauth.describe_token_problem("f85632530bf277ec9ac6f649fc327f17"), "")
        self.assertEqual(oauth.describe_token_problem(""), "")

    def test_client_refuses_before_making_a_request(self):
        from woo2shopify.config import ShopifyConfig
        from woo2shopify.shopify_api import ShopifyClient, ShopifyError

        cfg = ShopifyConfig(shop_domain=SHOP, access_token="atkn_9892da6353", auth_mode="token")
        client = ShopifyClient(cfg, max_retries=3)
        with mock.patch.object(client.session, "post") as post:
            with self.assertRaises(ShopifyError) as ctx:
                client.graphql("{ shop { name } }")
        post.assert_not_called()
        self.assertIn("App Automation Token", str(ctx.exception))


class ClientRefreshTest(unittest.TestCase):
    """A token that expires mid-migration must not end the run."""

    def test_401_triggers_one_refresh_and_a_retry(self):
        from woo2shopify.config import ShopifyConfig
        from woo2shopify.shopify_api import ShopifyClient

        cfg = ShopifyConfig(shop_domain=SHOP, client_id=CLIENT_ID, client_secret=SECRET,
                            auth_mode="client_credentials")
        client = ShopifyClient(cfg, max_retries=3)

        minted = []

        def fake_fetch(shop, cid, secret, log=None):
            minted.append(1)
            return {"access_token": f"tok_{len(minted)}", "scope": "", "expires_in": 86399}

        expired = mock.Mock(status_code=401, text='{"errors":"[API] Invalid API key or access token"}')
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"data": {"shop": {"name": "Test Store"}}, "extensions": {}}
        sent_tokens = []

        def fake_post(url, **kwargs):
            sent_tokens.append(client.session.headers["X-Shopify-Access-Token"])
            return expired if len(sent_tokens) == 1 else ok

        with mock.patch.object(oauth, "fetch_client_credentials_token", side_effect=fake_fetch),              mock.patch.object(client.session, "post", side_effect=fake_post):
            data = client.graphql("{ shop { name } }")

        self.assertEqual(data["shop"]["name"], "Test Store")
        self.assertEqual(sent_tokens, ["tok_1", "tok_2"], "the retry must carry the new token")
        self.assertEqual(len(minted), 2)

    def test_pasted_token_401_fails_fast_with_guidance(self):
        from woo2shopify.config import ShopifyConfig
        from woo2shopify.shopify_api import ShopifyClient, ShopifyError

        cfg = ShopifyConfig(shop_domain=SHOP, access_token="shpat_stale", auth_mode="token")
        client = ShopifyClient(cfg, max_retries=3)
        expired = mock.Mock(status_code=401, text='{"errors":"[API] Invalid API key or access token"}')
        with mock.patch.object(client.session, "post", return_value=expired) as post:
            with self.assertRaises(ShopifyError) as ctx:
                client.graphql("{ shop { name } }")
        self.assertEqual(post.call_count, 1, "no point retrying a token we cannot renew")
        self.assertIn("client_credentials", str(ctx.exception))


class AuthErrorMessageTest(unittest.TestCase):
    def test_401_explains_the_token_mix_up(self):
        from woo2shopify.shopify_api import _describe_http_error

        message = _describe_http_error(401, '{"errors":"[API] Invalid API key or access token"}')
        self.assertIn("client_credentials", message)
        self.assertIn("Client secret is not an access token", message)
        self.assertIn("myshopify.com", message)
        self.assertNotIn("client_credentials", _describe_http_error(500, "boom"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
