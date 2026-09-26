# Xolby — Discord Bot (Roblox + System Scanner + TikTok Analytics + Moderation)

A comprehensive Discord slash-command bot written in Python with `discord.py` that provides:

- 🎮 **Roblox Live Trackers** (`/populargames`) — Roblox Top 20 games chart with live updating embeds
- 🤖 **System Code Scanner** (`/scan`) — Read-only code security scanner using Google Gemini REST API
- 🎵 **TikTok Analytics System** (`/tiktokconnect`, `/tiktokstats`, `/tiktoklive`, `/tiktokhistory`, `/tiktokdisconnect`) — Official TikTok API integration for user video metrics, calculated engagement, growth tracking, historical charts, and persistent live embeds
- 🛡️ **Moderation & Utility** — Full suite of moderation, server info, and utility commands (`/kick`, `/ban`, `/warn`, `/clear`, `/serverinfo`, `/remind`, `/poll`, etc.)

All features run natively in Python 3.10+, including Termux on Android ARM64 (Python 3.14), Linux, macOS, and Windows.

---

## Architecture

Xolby runs as **two cooperating processes**. TikTok OAuth is owned entirely by
the web backend on Render; the Discord bot on Termux never handles TikTok
secrets or tokens.

```text
Discord Bot (Termux)                    Xolby Web Backend (Render)                TikTok
────────────────────                    ──────────────────────────                ──────
/tiktokconnect
      │  POST /api/tiktok/oauth/start
      │  Authorization: Bearer <XOLBY_WEB_API_KEY>
      ├────────────────────────────────▶  create single-use OAuth session/state
      │                                   (stored in DATABASE_URL)
      │◀────────────────────────────────  short-lived authorization URL
      │
   Discord button ───────────────────────────────────────────────────────────▶ TikTok authorize
                                                                                     │
                                    GET /oauth/tiktok/callback ◀──────────────────────┘
                                            │  validate & consume state
                                            │  exchange code with TikTok (client_secret stays here)
                                            │  encrypt access/refresh tokens
                                            │  persist credentials (DATABASE_URL)
                                            ▼
                                    ✅ "return to Discord" page

/tiktokstats, /tiktoklive, /tiktokhistory
      │  GET /api/tiktok/videos/{discord_user_id}   (Bearer auth)
      ├────────────────────────────────▶  decrypt token server-side, refresh if needed,
      │                                   call TikTok Display API v2
      │◀────────────────────────────────  JSON metrics (never tokens)

/tiktokdisconnect
      │  POST /api/tiktok/disconnect                (Bearer auth)
      └────────────────────────────────▶  revoke + delete credentials & sessions
```

### Why the split?

- `cryptography` cannot reliably load in the Termux Python 3.14 ARM64
  environment. Moving all encryption to Render means the bot needs **zero**
  native crypto dependencies.
- The Discord bot needs no inbound port or public URL for OAuth.
- There is exactly **one** OAuth callback server, and it is the Render backend.

---

## Termux & Python 3.14 Compatibility

- **No `cryptography` required on Termux** for TikTok. `/tiktokconnect` works
  even when the `cryptography` package is missing or fails to load.
- **Zero Native Chart Dependencies**: historical growth charts (`/tiktokhistory`)
  use a pure Python standard library PNG encoder (`struct`, `zlib`, `io`).
- **No local OAuth callback server** runs inside the bot.
- **No Insecure Plaintext Storage**: TikTok tokens are never stored on Termux;
  they are encrypted at rest on Render.
- All non-TikTok commands (`/populargames`, `/scan`, moderation, info, utility)
  remain fully operational regardless of the crypto situation.

---

## Commands Summary

| Command | Category | Description |
| --- | --- | --- |
| `/populargames` | Roblox | Top 20 Roblox games by active players (with optional live tracker) |
| `/scan` | AI Scanner | Scans configured codebase directory for security issues with Gemini |
| `/tiktokconnect` | TikTok Analytics | Opens the Render-hosted official TikTok OAuth flow |
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

## Security Guarantees

- **No Password Storage**: never asks for or stores TikTok passwords.
- **No Plaintext Tokens**: access/refresh tokens are encrypted (Fernet) before
  being persisted, and only on the Render server.
- **Client Secret Isolation**: `TIKTOK_CLIENT_SECRET` lives only on Render. It is
  never shipped to Termux, Discord, the browser, or any URL.
- **Single-Use OAuth State**: cryptographically random, bound to the Discord user
  id, stored server-side, expires after 10 minutes, and consumed atomically.
- **Authenticated Bot API**: the bot authenticates with `XOLBY_WEB_API_KEY`
  (`Authorization: Bearer …`); the backend rejects unauthenticated requests and
  never exposes arbitrary Discord-user lookups.
- **No Token Logging**: tokens, refresh tokens, client secrets, encryption keys,
  authorization codes and API keys are never logged.
- **Official APIs Only**: TikTok Display API v2 via official OAuth. No TikTok
  Studio scraping and no private endpoints.
- **Unavailable metrics are labelled, not invented**: Studio-only metrics
  (watch time, retention) are shown as unavailable.

---

## Installation & Setup (Local Development)

### 1. Install dependencies

For the Discord bot (Termux/Linux/macOS/Windows):

```bash
python -m pip install -r requirements.txt
```

For the web backend that needs PostgreSQL (Render):

```bash
python -m pip install -r requirements-render.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`. See the variable tables below.

### 3. Run the Discord bot

```bash
python bot.py
```

### 4. Run the web backend (Render-compatible)

```bash
uvicorn web.app:app --host 0.0.0.0 --port $PORT
```

`/health` returns `{"status": "ok"}`. The FastAPI app starts independently of
the Discord bot.

---

## Render Deployment

1. Create a **Web Service** from this repository.
2. Build command: `pip install -r requirements-render.txt`
3. Start command: `uvicorn web.app:app --host 0.0.0.0 --port $PORT`
4. Health check path: `/health`
5. Add a managed **PostgreSQL** database (Render Postgres, Neon, etc.) and copy
   its connection string into the `DATABASE_URL` environment variable.
6. Set the server-side environment variables below in the Render dashboard.

### Required Render environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `TIKTOK_CLIENT_KEY` | Yes | TikTok app client key (developer portal) |
| `TIKTOK_CLIENT_SECRET` | Yes | TikTok app client secret (server-only) |
| `TIKTOK_REDIRECT_URI` | Yes | `https://xolby.onrender.com/oauth/tiktok/callback` |
| `TIKTOK_TOKEN_ENCRYPTION_KEY` | Yes | Long random secret used to derive the Fernet key |
| `DATABASE_URL` | Yes | PostgreSQL URL for OAuth sessions and encrypted credentials |
| `XOLBY_WEB_API_KEY` | Yes | Shared bearer secret used by the Discord bot |
| `WEB_BASE_URL` | No | `https://xolby.onrender.com` |
| `XOLBY_CONTACT_EMAIL` | No | Contact address shown on legal pages |

### TikTok Developer Portal

Set the OAuth **Redirect URI** to:

```text
https://xolby.onrender.com/oauth/tiktok/callback
```

Requested scopes: `user.info.basic`, `video.list`.

---

## Termux (Discord Bot) Environment Variables

| Variable | Required | Description |
| --- | --- | --- |
| `DISCORD_TOKEN` | Yes | Discord bot token |
| `XOLBY_WEB_BASE_URL` | Yes | Render backend base URL, e.g. `https://xolby.onrender.com` |
| `XOLBY_WEB_API_KEY` | Yes | Shared internal API key (must match Render) |
| `GEMINI_API_KEY` | For `/scan` | Google Gemini API key |
| `SCAN_DIRECTORY` | For `/scan` | Path to source directory to scan |
| `REPORT_CHANNEL_ID` | For `/scan` | Discord channel ID for scan reports |
| `ROBLOX_DISCOVERY_URL` | No | Optional custom Roblox discovery source |
| `TIKTOK_LIVE_INTERVAL_SECONDS` | No | Live TikTok embed refresh interval (default `300`) |

> The bot must **not** be given `TIKTOK_CLIENT_SECRET`, `TIKTOK_CLIENT_KEY`,
> `TIKTOK_TOKEN_ENCRYPTION_KEY`, or `DATABASE_URL`. Those are Render-only.

---

## Database Requirements

- **Render backend** persists OAuth sessions and encrypted TikTok credentials
  through `DATABASE_URL`:
  - `postgresql://…` / `postgres://…` → PostgreSQL (recommended production)
  - `sqlite:///path` or a plain path → SQLite (local development only)
- Render's filesystem is **ephemeral**, so production must use PostgreSQL;
  SQLite on Render would lose OAuth sessions and credentials on redeploy.
- **Termux bot** keeps its own local `bot_data.db` SQLite for non-sensitive data
  (warnings, reminders, Roblox trackers, TikTok snapshot history). It never
  stores TikTok tokens.

Backend schema (created automatically):

- `tiktok_oauth_sessions(state, discord_user_id, created_at, expires_at, consumed, completed_at)`
- `tiktok_credentials(discord_user_id, tiktok_open_id, display_name, access_token, refresh_token, expires_at, refresh_expires_at, created_at, updated_at)`

---

## Official API Scopes & Metrics

Uses TikTok's official Display API v2:

- **Scopes**: `user.info.basic`, `video.list`
- **Supported API Metrics**: Views, Likes, Comments, Shares, Favorites (where
  available), Title, Cover Thumbnail, Posted Time, Video URL.
- **Calculated Derived Metrics**:
  - Like Rate: `(likes / views) * 100`
  - Comment Rate: `(comments / views) * 100`
  - Share Rate: `(shares / views) * 100`
  - Total Engagement Rate: `((likes + comments + shares + favorites) / views) * 100`
- **TikTok Studio-only Metrics**: watch time, retention, and play time are
  explicitly labelled as unavailable and never fabricated.

---

## API Reference (internal bot ⇄ backend)

All endpoints require `Authorization: Bearer <XOLBY_WEB_API_KEY>`.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/tiktok/oauth/start` | Create a single-use OAuth session, return the authorization URL |
| `GET` | `/api/tiktok/account/{discord_user_id}` | Connected account metadata (no tokens) |
| `GET` | `/api/tiktok/videos/{discord_user_id}` | Recent video metrics (no tokens) |
| `POST` | `/api/tiktok/disconnect` | Revoke tokens and delete stored credentials/sessions |
| `GET` | `/oauth/tiktok/callback` | Public TikTok redirect handler (validates state, exchanges code) |
| `GET` | `/health` | Liveness probe → `{"status": "ok"}` |

---

## Testing

```bash
python -m compileall -q .
python -m unittest discover -s tests
```

Tests cover OAuth session creation, single-use/expiry/invalid-state handling,
encrypted-only storage, that tokens are never returned to the bot, that
`/tiktokconnect` works without `cryptography`, that the bot starts no local OAuth
server, that command cogs load, and that `/health` works.
