"""Architecture tests for the TikTok OAuth refactor.

These assert that:

* the Termux bot does not need ``cryptography`` for ``/tiktokconnect``,
* the bot no longer runs a local TikTok OAuth callback server,
* all existing command cogs still load.
"""

import asyncio
import inspect
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import discord
from discord.ext import commands

import commands.tiktok as tiktok_cog_module
from bot import PopularGamesBot
from commands.tiktok import TikTokCog

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TIKTOK_COMMANDS = {
    "tiktokconnect",
    "tiktokstats",
    "tiktoklive",
    "tiktokhistory",
    "tiktokdisconnect",
}

OTHER_EXTENSIONS = [
    "commands.clear",
    "commands.info",
    "commands.moderation",
    "commands.scan",
    "commands.utility",
]


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.sent = []

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeInteraction:
    def __init__(self, user_id: int = 123456789):
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.guild_id = None
        self.channel_id = None


class FakeBackend:
    """Stand-in for services.web_backend.web_backend."""

    def __init__(self):
        self.started = []

    def is_configured(self) -> bool:
        return True

    async def get_account(self, discord_user_id):
        return None

    async def start_tiktok_oauth(self, discord_user_id):
        self.started.append(discord_user_id)
        return {
            "authorization_url": "https://www.tiktok.com/v2/auth/authorize/?state=abc",
            "expires_in": 600,
        }


class TestNoLocalOAuthServer(unittest.TestCase):
    def test_local_oauth_module_removed(self):
        self.assertFalse(
            os.path.exists(os.path.join(REPO_ROOT, "services", "tiktok_oauth.py")),
            "services/tiktok_oauth.py should be removed: Render owns TikTok OAuth.",
        )

    def test_bot_does_not_start_local_oauth_server(self):
        with open(os.path.join(REPO_ROOT, "bot.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("TikTokOAuthServer", source)
        self.assertNotIn("tiktok_oauth_server", source)

    def test_bot_does_not_import_tiktok_oauth(self):
        with open(os.path.join(REPO_ROOT, "bot.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("services.tiktok_oauth", source)


class TestConnectIndependenceFromCryptography(unittest.TestCase):
    def test_tiktok_cog_does_not_reference_encryption(self):
        source = inspect.getsource(tiktok_cog_module)
        self.assertNotIn("is_encryption_available", source)
        self.assertNotIn("import cryptography", source)

    def test_tiktokconnect_works_without_cryptography(self):
        """Even with the encryption backend forced off, /tiktokconnect succeeds."""
        backend = FakeBackend()
        interaction = FakeInteraction(user_id=998877)
        cog = TikTokCog.__new__(TikTokCog)  # avoid needing a real Bot at import

        with patch.object(tiktok_cog_module, "web_backend", backend):
            with patch("services.tiktok.IS_ENCRYPTION_AVAILABLE", False):
                from services.tiktok import is_encryption_available

                self.assertFalse(is_encryption_available())

                coro = TikTokCog.tiktokconnect.callback(cog, interaction)
                asyncio.run(coro)

        self.assertEqual(backend.started, [998877])
        self.assertTrue(interaction.response.deferred)
        self.assertTrue(interaction.followup.sent, "Connect embed should be sent to the user")
        embed = interaction.followup.sent[0][1].get("embed")
        self.assertIsNotNone(embed)
        self.assertIn("TikTok", embed.title)


class TestCommandsStillLoad(unittest.TestCase):
    def _load_extensions(self):
        # Use the real bot class so /populargames (registered in __init__) is present.
        bot = PopularGamesBot()
        loaded = []

        async def load():
            for ext in ["commands.tiktok", *OTHER_EXTENSIONS]:
                await bot.load_extension(ext)
                loaded.append(ext)
            return sorted(cmd.name for cmd in bot.tree.get_commands())

        try:
            names = asyncio.run(load())
        finally:
            asyncio.run(bot.close())
        return loaded, names

    def test_all_cogs_load_and_tiktok_commands_registered(self):
        loaded, names = self._load_extensions()
        self.assertIn("commands.tiktok", loaded)
        for ext in OTHER_EXTENSIONS:
            self.assertIn(ext, loaded)
        for name in TIKTOK_COMMANDS:
            self.assertIn(name, names)

    def test_non_tiktok_commands_registered(self):
        _, names = self._load_extensions()
        for expected in {"clear", "populargames", "scan", "serverinfo", "ping", "remind"}:
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
