"""Automated unit tests for TikTok Analytics system in Xolby."""

import asyncio
import datetime
import io
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.db import Database, TikTokSnapshotRecord
from services.tiktok import (
    TikTokAPIClient,
    TikTokRateLimitError,
    TikTokTokenExpiredError,
    calculate_engagement_metrics,
    decrypt_token,
    encrypt_token,
    get_valid_access_token,
)
from services.tiktok_analytics import (
    build_tiktok_stats_embed,
    generate_history_chart,
)
from services.tiktok_oauth import (
    build_authorization_url,
    create_oauth_state,
    verify_and_consume_state,
)


class TestTikTokDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp_db_file = tempfile.NamedTemporaryFile(delete=False)
        self.tmp_db_file.close()
        self.db = Database(db_path=self.tmp_db_file.name)

    def tearDown(self):
        if os.path.exists(self.tmp_db_file.name):
            os.remove(self.tmp_db_file.name)

    def test_account_crud(self):
        user_id = 998877
        account = self.db.save_tiktok_account(
            discord_user_id=user_id,
            tiktok_open_id="open_id_xyz",
            display_name="TestCreator",
            access_token="encrypted_access_token",
            refresh_token="encrypted_refresh_token",
            expires_at=2000000000,
            refresh_expires_at=2050000000,
        )
        self.assertIsNotNone(account)
        self.assertEqual(account.discord_user_id, user_id)
        self.assertEqual(account.display_name, "TestCreator")

        fetched = self.db.get_tiktok_account(user_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.tiktok_open_id, "open_id_xyz")

        all_accounts = self.db.get_all_tiktok_accounts()
        self.assertEqual(len(all_accounts), 1)

        deleted = self.db.delete_tiktok_account(user_id)
        self.assertTrue(deleted)
        self.assertIsNone(self.db.get_tiktok_account(user_id))

    def test_snapshots_crud(self):
        user_id = 998877
        snap1 = self.db.add_tiktok_snapshot(
            discord_user_id=user_id,
            tiktok_open_id="open_id_xyz",
            video_id="video_001",
            video_title="Video 1",
            view_count=1000,
            like_count=100,
            comment_count=10,
            share_count=5,
            favorite_count=2,
        )
        self.assertEqual(snap1.view_count, 1000)

        snap2 = self.db.add_tiktok_snapshot(
            discord_user_id=user_id,
            tiktok_open_id="open_id_xyz",
            video_id="video_001",
            video_title="Video 1 Updated",
            view_count=1200,
            like_count=120,
            comment_count=15,
            share_count=8,
            favorite_count=3,
        )

        latest = self.db.get_latest_tiktok_snapshot(user_id, "video_001")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.view_count, 1200)

        prev = self.db.get_previous_tiktok_snapshot(user_id, "video_001")
        self.assertIsNotNone(prev)
        self.assertEqual(prev.view_count, 1000)

    def test_live_tracker_crud(self):
        user_id = 998877
        tracker = self.db.add_or_update_tiktok_live_tracker(
            discord_user_id=user_id,
            guild_id=11111,
            channel_id=22222,
            message_id=33333,
        )
        self.assertEqual(tracker.channel_id, 22222)

        active = self.db.get_active_tiktok_live_trackers()
        self.assertEqual(len(active), 1)

        self.db.deactivate_tiktok_live_tracker(22222)
        active_after = self.db.get_active_tiktok_live_trackers()
        self.assertEqual(len(active_after), 0)

        self.db.remove_tiktok_live_tracker(22222)


class TestTikTokUtilities(unittest.TestCase):
    def test_token_encryption(self):
        raw_token = "act_secret_token_12345"
        enc = encrypt_token(raw_token)
        self.assertNotEqual(enc, raw_token)
        dec = decrypt_token(enc)
        self.assertEqual(dec, raw_token)

    def test_engagement_calculations(self):
        # Zero views
        zero_res = calculate_engagement_metrics(0, 100, 10, 5, 2)
        self.assertEqual(zero_res["total_engagement_rate"], 0.0)
        self.assertEqual(zero_res["like_rate"], 0.0)

        # Standard views
        res = calculate_engagement_metrics(10000, 800, 50, 100, 50)
        # (800+50+100+50) / 10000 * 100 = 1000 / 10000 * 100 = 10.0
        self.assertEqual(res["total_engagement_rate"], 10.0)
        self.assertEqual(res["like_rate"], 8.0)
        self.assertEqual(res["comment_rate"], 0.5)
        self.assertEqual(res["share_rate"], 1.0)

    def test_oauth_state(self):
        user_id = 554433
        state = create_oauth_state(user_id)
        self.assertTrue(len(state) > 10)

        auth_url = build_authorization_url(state)
        self.assertIn("client_key", auth_url)
        self.assertIn(state, auth_url)

        consumed = verify_and_consume_state(state)
        self.assertEqual(consumed, user_id)

        # Re-consuming should fail
        self.assertIsNone(verify_and_consume_state(state))

    def test_chart_generation(self):
        snaps = [
            TikTokSnapshotRecord(1, 123, "open1", "v1", "Vid 1", "2026-03-24T10:00:00+00:00", 1000, 100, 10, 5, 2),
            TikTokSnapshotRecord(2, 123, "open1", "v1", "Vid 1", "2026-03-24T11:00:00+00:00", 1500, 150, 15, 8, 4),
        ]
        chart_buf = generate_history_chart(snaps)
        self.assertIsNotNone(chart_buf)
        self.assertTrue(isinstance(chart_buf, io.BytesIO))
        self.assertTrue(len(chart_buf.getvalue()) > 500)

    def test_embed_builder(self):
        video = {
            "id": "v101",
            "title": "Test TikTok Video",
            "view_count": 5000,
            "like_count": 400,
            "comment_count": 30,
            "share_count": 20,
            "favorite_count": 10,
            "share_url": "https://www.tiktok.com/@user/video/v101",
        }
        embed = build_tiktok_stats_embed(video, account_name="TestAccount", status_tag="🟢 LIVE")
        self.assertEqual(embed.title, "📊 TikTok Analytics")
        self.assertIn("Test TikTok Video", embed.description)


if __name__ == "__main__":
    unittest.main()
