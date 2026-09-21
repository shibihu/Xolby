# Roblox + System Scanner Discord Bot

A Discord slash-command bot written in Python with `discord.py` that provides:

- `/populargames` — Roblox current **Top Playing Now** chart in an embed
- `/scan` — read-only **System Code Scanner** that analyzes a project directory
  with Google Gemini REST API and posts a categorized report

Both features live in the same bot. `/populargames` and `/scan` work natively in a single **Termux** environment (or standard Linux/macOS/Windows) without requiring Ubuntu or proot-distro.

## Commands

| Command | Who can run it | What it does |
| --- | --- | --- |
| `/populargames` | Everyone in the server | Top 20 Roblox games by active players |
| `/scan` | Members with **Manage Server** | Reads the configured directory, sanitizes secrets, asks Gemini for issues, posts a report |

`/populargames` does **not** need Gemini. `/scan` and `/populargames` are
independent: if Gemini is unreachable, `/populargames` keeps working.

## How `/populargames` works

```text
/populargames
      ↓
Roblox Explore API
      ↓
get games + playerCount directly
      ↓
sort by playerCount
      ↓
create Discord embed
      ↓
send response
```

`/populargames` uses the Roblox Explore API directly to retrieve game titles, root place IDs, and player counts in a single efficient step, falling back to batch requests only if necessary to prevent timeout risks.

## How `/scan` works

```text
/scan
   ↓
check .env configuration
   ↓
read SCAN_DIRECTORY (read-only, on a worker thread)
   ↓
scan supported source/config/doc files
   ↓
sanitize sensitive information ([REDACTED])
   ↓
split the project into chunks if it is large
   ↓
send each chunk to Gemini REST API (aiohttp + retries + fallback model)
   ↓
validate + de-duplicate findings
   ↓
categorize CRITICAL / WARNING / INFO
   ↓
build Discord embeds
   ↓
post to REPORT_CHANNEL_ID
   ↓
ephemeral status message to the person who ran the command
```

### Read-only guarantee

The scanner follows one rule: **READ → SANITIZE → ANALYZE → REPORT.**

It never:

- modifies, deletes, renames or formats any file
- executes scanned source code or shell commands
- installs packages
- auto-fixes code
- commits or pushes anything

## Installation

### Python version

Python **3.10+** is required (`discord.py` 2.6+). Standard Termux Python works directly!

### 1. Install dependencies

```bash
python -m pip install -r requirements.txt
```

Dependencies:

- `discord.py` — Discord API
- `aiohttp` — async HTTP for Gemini REST API & Roblox API
- `python-dotenv` — loads `.env`

Notice: `google-genai` is **no longer needed** or installed.

### 2. Create `.env`

```bash
cp .env.example .env
```

Then edit `.env`:

```env
DISCORD_TOKEN=your_real_discord_bot_token
GEMINI_API_KEY=your_real_gemini_api_key
GEMINI_MODEL=gemini-3.5-flash
GEMINI_FALLBACK_MODEL=gemini-3.5-flash-lite
SCAN_DIRECTORY=/sdcard/Projects/RobloxBot
REPORT_CHANNEL_ID=123456789012345678
SCAN_MAX_FILE_BYTES=200000
SCAN_MAX_FILES=250
```

Never commit real credentials.

### 3. Run the bot directly in Termux

```bash
cd /sdcard/Projects/RobloxBot
python bot.py
```

No Ubuntu.
No proot-distro.
No special Python interpreter.

## Configuration reference

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | yes | — | Bot token |
| `GEMINI_API_KEY` | for `/scan` | — | Gemini API key |
| `GEMINI_MODEL` | no | `gemini-3.5-flash` | Primary Gemini model name |
| `GEMINI_FALLBACK_MODEL` | no | `gemini-3.5-flash-lite` | Fallback Gemini model name |
| `SCAN_DIRECTORY` | for `/scan` | — | Directory to scan |
| `REPORT_CHANNEL_ID` | for `/scan` | — | Channel that receives reports |
| `SCAN_MAX_FILE_BYTES` | no | `200000` | Max size of one file |
| `SCAN_MAX_FILES` | no | `250` | Max files per scan |
| `SCAN_MAX_TOTAL_BYTES` | no | `800000` | Max total source bytes per scan |
| `SCAN_MAX_BYTES_PER_CHUNK` | no | `180000` | Size of one Gemini request |
| `SCAN_MAX_CHUNKS` | no | `8` | Max Gemini requests per scan |
| `GEMINI_TIMEOUT_SECONDS` | no | `180` | Per-request timeout |
