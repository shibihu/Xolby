import os
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from web.app import app
from web.routes.tiktok_oauth import (
    get_config_diagnostics,
    mask_secret,
)
from services.tiktok import is_encryption_available, encrypt_token, decrypt_token
from services.tiktok_oauth import create_oauth_state, verify_and_consume_state


class TestWebBackend(unittest.TestCase):
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

    @patch.dict(os.environ, {"TIKTOK_CLIENT_KEY": "", "TIKTOK_CLIENT_SECRET": ""})
    def test_oauth_start_fails_safely_when_unconfigured(self):
        response = self.client.get("/oauth/tiktok", follow_redirects=False)
        self.assertEqual(response.status_code, 500)
        self.assertIn("Configuration Error", response.text)
        self.assertNotIn("TIKTOK_CLIENT_SECRET", response.text)

    @patch.dict(
        os.environ,
        {
            "TIKTOK_CLIENT_KEY": "valid_client_key_123",
            "TIKTOK_CLIENT_SECRET": "valid_client_secret_456",
            "TIKTOK_REDIRECT_URI": "https://example.com/oauth/tiktok/callback",
        },
    )
    def test_oauth_start_constructs_valid_url(self):
        response = self.client.get("/oauth/tiktok", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        redirect_url = response.headers.get("location", "")

        self.assertTrue(redirect_url.startswith("https://www.tiktok.com/v2/auth/authorize/"))
        self.assertIn("client_key=valid_client_key_123", redirect_url)
        self.assertIn("response_type=code", redirect_url)
        self.assertIn("scope=user.info.basic%2Cvideo.list", redirect_url)
        self.assertIn("redirect_uri=https%3A%2F%2Fexample.com%2Foauth%2Ftiktok%2Fcallback", redirect_url)
        self.assertIn("state=", redirect_url)

    def test_oauth_state_randomness_and_consumption(self):
        state1 = create_oauth_state(12345)
        state2 = create_oauth_state(12345)
        self.assertNotEqual(state1, state2)
        self.assertTrue(len(state1) >= 32)

        consumed = verify_and_consume_state(state1)
        self.assertEqual(consumed, 12345)

        self.assertIsNone(verify_and_consume_state(state1))

    def test_callback_rejects_invalid_state(self):
        response = self.client.get("/oauth/tiktok/callback?code=fake_code&state=invalid_state_xyz")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Session Expired", response.text)

    def test_callback_handles_tiktok_error_response(self):
        response = self.client.get(
            "/oauth/tiktok/callback?error=access_denied&error_description=User+canceled+authorization"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Authorization Canceled", response.text)
        self.assertIn("User canceled authorization", response.text)

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
            self.assertIn("1234********cdef", diag["TIKTOK_CLIENT_KEY"])
            self.assertEqual(diag["TIKTOK_CLIENT_SECRET"], "configured")
            self.assertNotIn("secret_key_abcdef123456", str(diag))

    def test_no_plaintext_token_fallback(self):
        if is_encryption_available():
            token = "secret_access_token_777"
            encrypted = encrypt_token(token)
            self.assertNotEqual(encrypted, token)
            decrypted = decrypt_token(encrypted)
            self.assertEqual(decrypted, token)

            self.assertEqual(mask_secret("1234567890"), "1234********7890")


if __name__ == "__main__":
    unittest.main()
