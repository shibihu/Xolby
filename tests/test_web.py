"""Tests for the Render web backend: pages, health, OAuth sessions and the
authenticated internal bot API.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from web import oauth_store as oauth_store_module
from web.app import app
from web.oauth_store import OAuthStore
from web.routes import tiktok_oauth as routes_module
from web.routes.tiktok_oauth import get_config_diagnostics, mask_secret

API_KEY = "test-internal-api-key"


def fake_encrypt(plaintext: str) -> str:
    """Deterministic stand-in for Fernet encryption in tests."""
    return "enc::" + plaintext if plaintext else ""


def fake_decrypt(ciphertext: str) -> str:
    return ciphertext[len("enc::"):] if ciphertext and ciphertext.startswith("enc::") else ciphertext

CONFIGURED_ENV = {
    "XOLBY_WEB_API_KEY": API_KEY,
    "TIKTOK_CLIENT_KEY": "valid_client_key_123",
    "TIKTOK_CLIENT_SECRET": "valid_client_secret_456",
    "TIKTOK_REDIRECT_URI": "https://xolby.onrender.com/oauth/tiktok/callback",
    "TIKTOK_TOKEN_ENCRYPTION_KEY": "unit-test-encryption-secret",
}


class TestWebPages(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_homepage_returns_200(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Xolby", response.text)
        self.assertIn("Privacy Policy", response.text)
        self.assertIn("Terms of Service", response.text)

    def test_privacy_page_returns_200(self):
        response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Privacy Policy", response.text)

    def test_terms_page_returns_200(self):
        response = self.client.get("/terms")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Terms of Service", response.text)

    def test_health_check_returns_ok(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_tiktok_verification_file_at_root(self):
        response = self.client.get("/tiktokwVtAGGjV61utxKvtT5HcHCXtGSNUGZ67.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.text.strip(),
            "tiktok-developers-site-verification=wVtAGGjV61utxKvtT5HcHCXtGSNUGZ67",
        )
        self.assertIn("text/plain", response.headers.get("content-type", ""))

    def test_oauth_entry_page_points_to_discord(self):
        response = self.client.get("/oauth/tiktok")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/tiktokconnect", response.text)


class TestInternalApiAuth(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_start_rejected_without_api_key(self):
        with patch.dict(os.environ, CONFIGURED_ENV, clear=False):
            response = self.client.post("/api/tiktok/oauth/start", json={"discord_user_id": 123})
        self.assertEqual(response.status_code, 401)

    def test_start_rejected_with_wrong_api_key(self):
        with patch.dict(os.environ, CONFIGURED_ENV, clear=False):
            response = self.client.post(
                "/api/tiktok/oauth/start",
                json={"discord_user_id": 123},
                headers={"Authorization": "Bearer wrong-key"},
            )
        self.assertEqual(response.status_code, 401)

    def test_account_endpoint_requires_api_key(self):
        with patch.dict(os.environ, CONFIGURED_ENV, clear=False):
            response = self.client.get("/api/tiktok/account/123")
        self.assertEqual(response.status_code, 401)


class TestOAuthSessions(unittest.TestCase):
    """OAuth state is created, validated, single-use and short-lived."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.store = OAuthStore(database_url=f"sqlite:///{self.tmp.name}")
        self.addCleanup(lambda: os.path.exists(self.tmp.name) and os.remove(self.tmp.name))

    def test_create_session_binds_discord_user(self):
        session = self.store.create_session(12345)
        self.assertTrue(len(session.state) >= 32)
        self.assertEqual(session.discord_user_id, 12345)
        self.assertFalse(session.consumed)
        self.assertFalse(session.expires_at <= session.created_at)

    def test_consume_session_is_single_use(self):
        session = self.store.create_session(12345)
        consumed = self.store.consume_session(session.state)
        self.assertIsNotNone(consumed)
        self.assertEqual(consumed.discord_user_id, 12345)
        # Re-consuming the same state must fail.
        self.assertIsNone(self.store.consume_session(session.state))

    def test_invalid_state_is_rejected(self):
        self.assertIsNone(self.store.consume_session("not-a-real-state"))
        self.assertIsNone(self.store.consume_session(""))

    def test_expired_state_is_rejected(self):
        session = self.store.create_session(12345, ttl_seconds=-1)
        self.assertIsNone(self.store.consume_session(session.state))

    def test_server_creates_oauth_session_for_bot(self):
        with patch.object(oauth_store_module, "oauth_store", self.store):
            with patch.dict(os.environ, CONFIGURED_ENV, clear=False):
                with patch.object(routes_module, "is_encryption_available", lambda: True):
                    client = TestClient(app)
                    response = client.post(
                        "/api/tiktok/oauth/start",
                        json={"discord_user_id": 555},
                        headers={"Authorization": f"Bearer {API_KEY}"},
                    )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("authorization_url", body)
        self.assertTrue(body["authorization_url"].startswith("https://www.tiktok.com/v2/auth/authorize/"))
        self.assertIn("state=", body["authorization_url"])
        self.assertEqual(body["expires_in"], 600)

    def test_start_rejected_when_unconfigured(self):
        with patch.object(oauth_store_module, "oauth_store", self.store):
            with patch.dict(
                os.environ,
                {"XOLBY_WEB_API_KEY": API_KEY, "TIKTOK_CLIENT_KEY": "", "TIKTOK_CLIENT_SECRET": ""},
                clear=False,
            ):
                with patch.object(routes_module, "is_encryption_available", lambda: True):
                    client = TestClient(app)
                    response = client.post(
                        "/api/tiktok/oauth/start",
                        json={"discord_user_id": 555},
                        headers={"Authorization": f"Bearer {API_KEY}"},
                    )
        self.assertEqual(response.status_code, 503)

    def test_start_rejected_when_encryption_unavailable(self):
        """A server without the encryption backend must refuse to start OAuth."""
        with patch.object(oauth_store_module, "oauth_store", self.store):
            with patch.dict(os.environ, CONFIGURED_ENV, clear=False):
                with patch.object(routes_module, "is_encryption_available", lambda: False):
                    client = TestClient(app)
                    response = client.post(
                        "/api/tiktok/oauth/start",
                        json={"discord_user_id": 555},
                        headers={"Authorization": f"Bearer {API_KEY}"},
                    )
        self.assertEqual(response.status_code, 503)


class TestOAuthCallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.store = OAuthStore(database_url=f"sqlite:///{self.tmp.name}")
        self.addCleanup(lambda: os.path.exists(self.tmp.name) and os.remove(self.tmp.name))
        self.client = TestClient(app)

    def test_callback_rejects_invalid_state(self):
        with patch.object(oauth_store_module, "oauth_store", self.store):
            with patch.object(routes_module, "is_encryption_available", lambda: True):
                response = self.client.get(
                    "/oauth/tiktok/callback?code=fake_code&state=invalid_state_xyz"
                )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Session Expired", response.text)

    def test_callback_handles_tiktok_error_response(self):
        response = self.client.get(
            "/oauth/tiktok/callback?error=access_denied&error_description=User+canceled+authorization"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Authorization Canceled", response.text)
        self.assertIn("User canceled authorization", response.text)

    def test_callback_missing_params(self):
        response = self.client.get("/oauth/tiktok/callback?state=only_state")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid Request", response.text)

    def test_callback_stores_only_encrypted_tokens(self):
        session = self.store.create_session(4242)

        class FakeTikTokClient:
            def __init__(self, *args, **kwargs):
                pass

            async def exchange_code(self, code):
                return {
                    "access_token": "plaintext-access-token",
                    "refresh_token": "plaintext-refresh-token",
                    "open_id": "open-id-abc",
                    "expires_in": 86400,
                    "refresh_expires_in": 31536000,
                }

            async def get_user_info(self, access_token):
                return {"display_name": "Tester"}

            async def close(self):
                pass

        with patch.object(oauth_store_module, "oauth_store", self.store):
            with patch.dict(os.environ, CONFIGURED_ENV, clear=False):
                with patch.object(routes_module, "is_encryption_available", lambda: True):
                    with patch.object(routes_module, "encrypt_token", fake_encrypt):
                        with patch("services.tiktok.TikTokAPIClient", FakeTikTokClient):
                            response = self.client.get(
                                f"/oauth/tiktok/callback?code=test_code&state={session.state}"
                            )

        self.assertEqual(response.status_code, 200)

        credential = self.store.get_credentials(4242)
        self.assertIsNotNone(credential)
        self.assertEqual(credential.tiktok_open_id, "open-id-abc")
        self.assertEqual(credential.display_name, "Tester")

        # Never persisted in plaintext.
        self.assertNotEqual(credential.access_token, "plaintext-access-token")
        self.assertNotEqual(credential.refresh_token, "plaintext-refresh-token")
        # ...but recoverable server-side via the encryption backend.
        self.assertEqual(fake_decrypt(credential.access_token), "plaintext-access-token")
        self.assertEqual(fake_decrypt(credential.refresh_token), "plaintext-refresh-token")

    def test_callback_state_single_use(self):
        session = self.store.create_session(4242)

        class FakeTikTokClient:
            def __init__(self, *args, **kwargs):
                pass

            async def exchange_code(self, code):
                return {"access_token": "t", "open_id": "o"}

            async def get_user_info(self, access_token):
                return {"display_name": "Tester"}

            async def close(self):
                pass

        with patch.object(oauth_store_module, "oauth_store", self.store):
            with patch.dict(os.environ, CONFIGURED_ENV, clear=False):
                with patch.object(routes_module, "is_encryption_available", lambda: True):
                    with patch.object(routes_module, "encrypt_token", fake_encrypt):
                        with patch("services.tiktok.TikTokAPIClient", FakeTikTokClient):
                            first = self.client.get(f"/oauth/tiktok/callback?code=c&state={session.state}")
                            second = self.client.get(f"/oauth/tiktok/callback?code=c&state={session.state}")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)


class TestTokensNeverReturned(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.store = OAuthStore(database_url=f"sqlite:///{self.tmp.name}")
        self.addCleanup(lambda: os.path.exists(self.tmp.name) and os.remove(self.tmp.name))
        self.client = TestClient(app)

    def test_account_endpoint_has_no_tokens(self):
        self.store.save_credentials(
            discord_user_id=777,
            tiktok_open_id="open-777",
            display_name="Seven",
            access_token=fake_encrypt("secret-access"),
            refresh_token=fake_encrypt("secret-refresh"),
            expires_at=9999999999,
        )
        with patch.object(oauth_store_module, "oauth_store", self.store):
            with patch.dict(os.environ, CONFIGURED_ENV, clear=False):
                response = self.client.get(
                    "/api/tiktok/account/777",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["connected"])
        self.assertNotIn("access_token", body["account"])
        self.assertNotIn("refresh_token", body["account"])

    def test_videos_endpoint_has_no_tokens(self):
        self.store.save_credentials(
            discord_user_id=777,
            tiktok_open_id="open-777",
            display_name="Seven",
            access_token=fake_encrypt("secret-access"),
            refresh_token=fake_encrypt("secret-refresh"),
            expires_at=9999999999,
        )

        class FakeTikTokClient:
            def __init__(self, *args, **kwargs):
                pass

            async def get_user_videos(self, access_token, max_count=10):
                return [{"id": "v1", "title": "Hi", "view_count": 3}]

            async def close(self):
                pass

        with patch.object(oauth_store_module, "oauth_store", self.store):
            with patch.dict(os.environ, CONFIGURED_ENV, clear=False):
                with patch("web.tiktok_service.is_encryption_available", lambda: True):
                    with patch("web.tiktok_service.decrypt_token", fake_decrypt):
                        with patch("web.tiktok_service.TikTokAPIClient", FakeTikTokClient):
                            response = self.client.get(
                                "/api/tiktok/videos/777",
                                headers={"Authorization": f"Bearer {API_KEY}"},
                            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertNotIn("secret-access", response.text)
        self.assertNotIn("secret-refresh", response.text)
        self.assertNotIn("access_token", response.text)
        self.assertNotIn("refresh_token", response.text)


class TestConfigDiagnostics(unittest.TestCase):
    def test_config_diagnostics_masks_secrets(self):
        with patch.dict(
            os.environ,
            {
                "TIKTOK_CLIENT_KEY": "1234567890abcdef",
                "TIKTOK_CLIENT_SECRET": "secret_key_abcdef123456",
                "TIKTOK_REDIRECT_URI": "https://example.com/oauth/tiktok/callback",
            },
        ):
            diag = get_config_diagnostics()
            self.assertEqual(diag["TIKTOK_CLIENT_KEY"], "1234********cdef")
            self.assertEqual(diag["TIKTOK_CLIENT_SECRET"], "configured")
            self.assertNotIn("secret_key_abcdef123456", str(diag))

    def test_mask_secret(self):
        self.assertEqual(mask_secret("1234567890"), "1234********7890")
        self.assertEqual(mask_secret("short"), "********")


if __name__ == "__main__":
    unittest.main()
