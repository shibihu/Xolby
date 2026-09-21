# Roblox + System Scanner Discord Bot

A Discord slash-command bot written in Python with `discord.py` that provides:

- `/populargames` — Roblox current **Top Playing Now** chart in an embed
- `/scan` — read-only **System Code Scanner** that analyzes a project directory
  with Google Gemini and posts a categorized report

Both features live in the same bot. `/populargames` is unchanged; the scanner
is additive.

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
top-playing-now
      ↓
Universe IDs
      ↓
Roblox Games API
      ↓
Current "playing" count
      ↓
Sort descending
      ↓
Top 20
      ↓
Discord Embed
```

Roblox deprecated the old `/v1/games/list` and `/v1/games/sorts` endpoints.
This version uses the newer `apis.roblox.com/explore-api` Discover/Charts
endpoint instead.

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
send each chunk to Gemini (structured JSON)
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

The only files it writes are none at all. It has no SQLite cache, no history
file and no state in this version.

## Installation

### Python version

Python **3.10+** is required (`discord.py` 2.6 and `google-genai` both need a
modern Python). Python 3.11/3.12 is recommended.

### 1. Get the code and create a virtual environment

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

macOS / Linux / Termux:

```bash
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

Dependencies:

- `discord.py` — Discord API
- `aiohttp` — async HTTP for the Roblox API
- `python-dotenv` — loads `.env`
- `google-genai` — official Google Gemini SDK (the modern one; the deprecated
  `google-generativeai` package is **not** used)

### 3. Create `.env`

```bash
cp .env.example .env
```

Then edit `.env`:

```env
DISCORD_TOKEN=your_real_discord_bot_token
GEMINI_API_KEY=your_real_gemini_api_key
GEMINI_MODEL=gemini-3.8-flash
SCAN_DIRECTORY=/sdcard/MyProject
REPORT_CHANNEL_ID=123456789012345678
SCAN_MAX_FILE_BYTES=200000
SCAN_MAX_FILES=250
```

`.env` is git-ignored (`.gitignore` contains `.env` and `.env.*`, with
`!.env.example` kept in the repository). Never commit real credentials.

### 4. Get a Gemini API key

1. Open <https://aistudio.google.com/apikey>.
2. Sign in and click **Create API key**.
3. Copy it into `GEMINI_API_KEY`.

`GEMINI_MODEL` must be a model your key can access. If the model name is wrong,
Gemini returns an error and `/scan` reports `Gemini request failed: ...` — fix
the value in `.env` and try again.

### 5. Configure `SCAN_DIRECTORY`

Use an absolute path to the project you want monitored:

```env
SCAN_DIRECTORY=/home/user/projects/myrepo
```

The directory must exist and be readable by the user running the bot.

### 6. Configure `REPORT_CHANNEL_ID`

1. In Discord, enable **User Settings → Advanced → Developer Mode**.
2. Right-click the target channel → **Copy Channel ID**.
3. Paste it into `REPORT_CHANNEL_ID`.

The bot must be a member of that server and needs **View Channel**,
**Send Messages** and **Embed Links** there.

### 7. Run the bot

```bash
python bot.py
```

You should see log lines like:

```text
INFO bot: Loaded /scan command
INFO discord.client: Logged in as YourBot (ID: ...)
```

## Running on Termux (Android)

```bash
pkg update && pkg upgrade
pkg install python git
termux-setup-storage          # grant storage access to /sdcard
cd /path/to/project
pip install -r requirements.txt
python bot.py
```

Notes:

- `termux-setup-storage` is required before `/sdcard/...` paths are readable.
  Without it, `/scan` fails with `SCAN_DIRECTORY is not readable` and the
  message tells you to run it.
- Paths are handled with `pathlib` and `os.walk`, so Linux/Termux, macOS and
  Windows paths all work; there is no Windows-only path logic.
- Keep the Termux session alive (`termux-wake-lock`) for long-running scans.

## Using `/scan`

1. Run `/scan` in any channel where the bot can read interactions.
2. The command replies "thinking…" immediately, then works in the background.
3. A **System Scan Report** embed plus one embed per issue appears in
   `REPORT_CHANNEL_ID`.
4. You get an ephemeral summary (`Files scanned`, `Critical/Warning/Info`).

Example main report:

```text
🛡️ System Scan Report
Directory: /sdcard/MyProject
Files scanned: 37
Critical: 2     Warning: 5     Info: 8
Files skipped: 4
Mode: Read-only (no files were modified)

AI Summary
The project appears mostly functional, but several configuration and
runtime issues were detected.
```

Example issue embeds:

```text
🚨 CRITICAL — API key exposed in source code
File: config.py:42
Description:
A credential appears to be hardcoded directly in the source code.
Suggested Fix:
Move the credential to an environment variable and load it using python-dotenv.
```

```text
⚠️ WARNING — Possible unhandled exception
File: services/api.py:87
Description:
The HTTP request does not appear to handle connection failures.
Suggested Fix:
Add timeout and exception handling.
```

```text
ℹ️ INFO — Potential code cleanup
File: utils.py:21
Description:
This function could be simplified.
Suggested Fix:
Consider extracting the repeated logic into a helper function.
```

Only members with **Manage Server** can run `/scan`. The command also has a
5-minute per-user cooldown to avoid burning Gemini quota.

### Severity levels

| Severity | Meaning |
| --- | --- |
| `CRITICAL` | Likely serious security problem, data loss, crash, or severe correctness issue |
| `WARNING` | Credible bug, reliability problem, security risk, or possibly incorrect behavior |
| `INFO` | Improvement, maintainability, minor optimization, or code-quality concern |

## What gets scanned

Supported extensions:

```text
.py .js .ts .jsx .tsx .java .kt .go .rs .c .cpp .h .hpp .cs .php .rb .swift .lua .gd
.html .css .json .yaml .yml .toml .ini .md .txt .sql .sh .ps1 .bat
```

Skipped directories include `.git`, `.hg`, `.svn`, `.venv`, `venv`,
`node_modules`, `__pycache__`, `.idea`, `.vscode`, `dist`, `build`, plus caches
such as `.mypy_cache`, `target` and `vendor`.

Never read (regardless of extension): `.env*`, `id_rsa`, `id_ed25519`,
`id_dsa`, `id_ecdsa`, `credentials*`, `secrets.*`, service account JSON files,
`.npmrc`, `.netrc`, `.pypirc`, `.git-credentials`, and `*.pem` / `*.key` /
`*.p12` / `*.keystore` files, plus anything under `.ssh`, `.aws`, `.gnupg` or a
`secrets` directory.

Binary files are detected and skipped. `SCAN_MAX_FILE_BYTES` (default 200 KB)
caps individual files and `SCAN_MAX_FILES` (default 250) caps the scan.

## Security limitations

- **Redaction is best-effort.** Anything that looks like `API_KEY=`, `TOKEN=`,
  `SECRET=`, `PASSWORD=`, `AUTH_TOKEN=`, `ACCESS_TOKEN=`, `PRIVATE_KEY=`, a
  Discord/AWS/Google/GitHub/Slack/OpenAI-style token, a JWT, a private key
  block, or `user:password@host` inside a URL is replaced with `[REDACTED]`
  before the text leaves your machine. Unusual secret formats can still slip
  through, so never point `SCAN_DIRECTORY` at a directory whose contents you
  cannot afford to send to a third-party API.
- **The code does leave your machine.** Sanitized source text is sent to
  Google's Gemini API, subject to Google's terms and retention policy. Do not
  scan client code, proprietary code, or regulated data without approval.
- **The report may still reveal structure.** File paths, function names and
  snippets quoted by the model can appear in Discord, so restrict who can read
  the report channel.
- **AI output is advisory.** Gemini can be wrong. Every finding is a hint to
  investigate, not a verified result. Nothing is auto-fixed.
- **Secrets live in `.env`.** Keep `.env` out of Git, and rotate any key you
  believe was exposed.

## Error handling

`/scan` degrades gracefully and never crashes the bot. It reports, instead of
raising, when:

- `GEMINI_API_KEY`, `SCAN_DIRECTORY`, `REPORT_CHANNEL_ID` or `DISCORD_TOKEN`
  are missing
- `REPORT_CHANNEL_ID` is not numeric
- the directory does not exist, is not a directory, or is not readable
- the project has no supported files
- files are unsupported, binary, unreadable, or larger than the limit
- Gemini times out, errors, returns invalid JSON, or returns an empty answer
- a chunk of a large project fails (the rest of the report is still delivered)
- the report channel is missing or the bot cannot post embeds in it
- anything unexpected happens (logged with a traceback, reported ephemerally)

## Large projects

Large trees are not sent in one request:

- `SCAN_MAX_FILE_BYTES` caps each file.
- `SCAN_MAX_FILES` caps the number of files.
- `SCAN_MAX_TOTAL_BYTES` (default 800 KB) caps the whole scan.
- Files are packed into chunks of about `SCAN_MAX_BYTES_PER_CHUNK` (default
  180 KB), and at most `SCAN_MAX_CHUNKS` (default 8) chunks are analyzed.
- Findings from all chunks are combined, duplicates removed, sorted by severity
  and capped at 50.
- Long reports are split into multiple Discord messages (max 10 embeds per
  message, long text safely truncated) and capped at 40 issue embeds, with a
  "Report truncated" note when anything is left out.

Files beyond the chunk budget are not analyzed; the report says how many.

## Project structure

```text
project/
├── bot.py                 # bot startup + /populargames (unchanged)
├── .env                   # real secrets (git-ignored)
├── .env.example           # template
├── requirements.txt
├── README.md
├── commands/
│   ├── __init__.py
│   └── scan.py            # /scan cog (Manage Server only)
└── services/
    ├── __init__.py
    ├── roblox.py          # /populargames data source (unchanged)
    ├── scanner.py         # read-only file scanning + secret redaction
    ├── gemini.py          # Gemini analysis, chunking, JSON validation
    └── reporter.py        # Discord embed report building/batching
```

## Configuration reference

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | yes | — | Bot token |
| `GEMINI_API_KEY` | for `/scan` | — | Gemini API key |
| `GEMINI_MODEL` | no | `gemini-3.8-flash` | Gemini model name |
| `SCAN_DIRECTORY` | for `/scan` | — | Directory to scan |
| `REPORT_CHANNEL_ID` | for `/scan` | — | Channel that receives reports |
| `SCAN_MAX_FILE_BYTES` | no | `200000` | Max size of one file |
| `SCAN_MAX_FILES` | no | `250` | Max files per scan |
| `SCAN_MAX_TOTAL_BYTES` | no | `800000` | Max total source bytes per scan |
| `SCAN_MAX_BYTES_PER_CHUNK` | no | `180000` | Size of one Gemini request |
| `SCAN_MAX_CHUNKS` | no | `8` | Max Gemini requests per scan |
| `GEMINI_TIMEOUT_SECONDS` | no | `180` | Per-request timeout |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `/scan` does not appear in Discord | Restart the bot; the command tree syncs at startup |
| Missing environment variable message | Fill the named key in `.env` and restart |
| `SCAN_DIRECTORY does not exist` | Use an absolute path; on Termux run `termux-setup-storage` |
| `SCAN_DIRECTORY is not readable` | Fix permissions/ownership of the directory |
| `Gemini request failed: ...` | Check `GEMINI_API_KEY` and `GEMINI_MODEL`, and your quota |
| `Gemini returned invalid JSON` | Retry; the parser is strict so bad answers never corrupt the report |
| Discord permission error | Grant View Channel, Send Messages, Embed Links in the report channel |
| `/scan` on cooldown | Wait for the remaining seconds shown |
| `/populargames` fails | Unrelated to the scanner: Roblox API/network issue |

## Notes

Roblox's Explore/Charts API is not a documented stable public contract, so its
response schema can change. The code extracts `universeId` values recursively to
reduce breakage if Roblox changes the response nesting. The chart is a Roblox
platform chart, not a mathematical crawl of every experience, so it is best
described as "Top Playing Now" based on the current Roblox chart.

## Planned (not implemented)

- SQLite issue history with `/scanstatus`, `/issues` and `/issue <id>`
- Issue de-duplication across scans and detection of resolved issues
- Scheduled/automatic scans and notifications on new CRITICAL findings
- Configurable include/exclude glob patterns and per-guild configuration

Automatic code modification or fixing is intentionally **not** planned for this
scanner; it stays read-only.
