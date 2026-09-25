# Xolby — Discord Bot (Roblox + System Scanner + TikTok Analytics + Moderation)

A comprehensive Discord slash-command bot written in Python with `discord.py` that provides:

- 🎮 **Roblox Live Trackers** (`/populargames`) — Roblox Top 20 games chart with live updating embeds
- 🤖 **System Code Scanner** (`/scan`) — Read-only code security scanner using Google Gemini REST API
- 🎵 **TikTok Analytics System** (`/tiktokconnect`, `/tiktokstats`, `/tiktoklive`, `/tiktokhistory`, `/tiktokdisconnect`) — Official TikTok API integration for user video metrics, calculated engagement, growth tracking, historical charts, and persistent live embeds
- 🛡️ **Moderation & Utility** — Full suite of moderation, server info, and utility commands (`/kick`, `/ban`, `/warn`, `/clear`, `/serverinfo`, `/remind`, `/poll`, etc.)

All features run natively in Python 3.10+, including Termux on Android ARM64 (Python 3.14), Linux, macOS, and Windows.

---

## Commands Summary

| Command | Category | Description |
| --- | --- | --- |
| `/populargames` | Roblox | Top 20 Roblox games by active players (with optional live tracker) |
| `/scan` | AI Scanner | Scans configured codebase directory for security issues with Gemini |
| `/tiktokconnect` | TikTok Analytics | Secure official OAuth connection link for TikTok |
| `/tiktokstats` | TikTok Analytics | View metrics and engagement rates for your latest TikTok video |
| `/tiktoklive` | TikTok Analytics | Persistent live-updating analytics message in current channel |
| `/tiktokhistory` | TikTok Analytics | Performance history list and pure Python generated growth chart PNG |
| `/tiktokdisconnect` | TikTok Analytics | Disconnect account, revoke tokens, and delete saved credentials |
| `/clear`, `/purge` | Moderation | Purge messages in channel |
| `/kick`, `/ban`, `/unban`, `/timeout` | Moderation | User moderation actions |
| `/warn`, `/warnings` | Moderation | User warning system |
| `/slowmode`, `/lock`, `/unlock` | Moderation | Channel state controls |
| `/serverinfo`, `/userinfo`, `/avatar` | Info | Server and member information |
| `/ping`, `/uptime`, `/botinfo`, `/help` | Utility | Bot diagnostics and help directory |

---

## Termux & Python 3.14 Compatibility

Xolby is engineered to start reliably on all environments, including Termux Android ARM64:
- **Zero Native Chart Dependencies**: Historical growth charts (`/tiktokhistory`) use a pure Python standard library PNG encoder (`struct`, `zlib`, `io`) with zero C/C++ compilation requirements.
- **Graceful Security Backend Handling**: If the native `cryptography` module is unavailable or fails to load on Termux, Xolby starts up normally without crashing. All core commands (`/populargames`, `/scan`, moderation, info, utility) remain 100% operational.
- **No Insecure Plaintext Storage**: If secure encryption is unavailable, TikTok account connection is safely disabled rather than storing unencrypted tokens in SQLite.
- **Termux Cryptography Note**: To enable TikTok token storage on Termux ARM64, install native cryptography via `pkg install python-cryptography` or `pkg install tur-repo && pkg install python-cryptography`.

---

## TikTok Analytics Architecture

```text
User → /tiktokconnect → Official TikTok OAuth → Redirect Web Callback Server
                                                       ↓
                                            Tokens Encrypted at Rest
                                                       ↓
TikTok Display API v2 ← Centralized Analytics Cache ← TikTokLiveManager Async Loop
                                                       ↓
                                  Database Snapshots & Pure Python Growth Chart PNG
                                                       ↓
                                          Persistent Discord Live Message
```

### Official API Scopes & Metrics
Uses TikTok's official Display API v2:
- **Scopes**: `user.info.basic`, `video.list`
- **Supported API Metrics**: Views, Likes, Comments, Shares, Favorites (where available), Title, Cover Thumbnail, Posted Time, Video URL.
- **Calculated Derived Metrics**:
  - Like Rate: `(likes / views) * 100`
  - Comment Rate: `(comments / views) * 100`
  - Share Rate: `(shares / views) * 100`
  - Total Engagement Rate: `((likes + comments + shares + favorites) / views) * 100`
- **TikTok Studio-only Metrics**: Advanced metrics such as watch time, retention, and play time are strictly noted as TikTok Studio-only and not fabricated.

### Security Guarantees
- **No Password Storage**: Never asks for or stores TikTok passwords.
- **Token Encryption**: Access and refresh tokens are encrypted at rest using Fernet encryption (`cryptography`). Plaintext tokens are NEVER stored.
- **No Token Logging**: Tokens and client secrets are never printed in logs, embeds, or exception messages.

---

## Installation & Setup

### 1. Install Dependencies

```bash
python -m pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
DISCORD_TOKEN=your_discord_bot_token

# System Code Scanner
GEMINI_API_KEY=your_gemini_api_key
SCAN_DIRECTORY=/path/to/project

# TikTok Analytics
TIKTOK_CLIENT_KEY=your_tiktok_app_client_key
TIKTOK_CLIENT_SECRET=your_tiktok_app_client_secret
TIKTOK_REDIRECT_URI=http://localhost:8080/tiktok/callback
TIKTOK_TOKEN_ENCRYPTION_KEY=a_secure_random_secret_string
TIKTOK_CALLBACK_HOST=0.0.0.0
TIKTOK_CALLBACK_PORT=8080
TIKTOK_LIVE_INTERVAL_SECONDS=300
```

### 3. Run the Bot

```bash
python bot.py
```

---

## Configuration Reference

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Yes | — | Discord Bot Token |
| `GEMINI_API_KEY` | For `/scan` | — | Google Gemini API Key |
| `SCAN_DIRECTORY` | For `/scan` | — | Path to source code directory |
| `REPORT_CHANNEL_ID` | For `/scan` | — | Discord channel ID for scan reports |
| `TIKTOK_CLIENT_KEY` | For TikTok | — | TikTok App Client Key |
| `TIKTOK_CLIENT_SECRET` | For TikTok | — | TikTok App Client Secret |
| `TIKTOK_REDIRECT_URI` | For TikTok | `http://localhost:8080/tiktok/callback` | OAuth Redirect URI configured in TikTok Developer Portal |
| `TIKTOK_TOKEN_ENCRYPTION_KEY` | Recommended | — | Server secret key for encrypting tokens at rest |
| `TIKTOK_CALLBACK_HOST` | No | `0.0.0.0` | OAuth callback server host |
| `TIKTOK_CALLBACK_PORT` | No | `8080` | OAuth callback server port |
| `TIKTOK_LIVE_INTERVAL_SECONDS` | No | `300` | Background update interval for live TikTok embeds (seconds) |
