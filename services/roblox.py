import asyncio
import datetime
import json
import logging
import os
import random
import time
import uuid
import aiohttp

log = logging.getLogger(__name__)

EXPLORE_SORT_CONTENT_URL = (
    "https://apis.roblox.com/explore-api/v1/get-sort-content"
)
GAMES_URL = "https://games.roblox.com/v1/games"

SESSION_ID = os.getenv("ROBLOX_SESSION_ID", str(uuid.uuid4()))

ROBLOX_TIMEOUT = aiohttp.ClientTimeout(
    total=30,
    connect=10,
    sock_connect=10,
    sock_read=25
)


def _extract_rows(payload):
    """Find game-like rows directly from Explore API response payload."""
    rows = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("universeId") is not None or value.get("universe_id") is not None:
                rows.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)

    seen = set()
    result = []
    for row in rows:
        uid = str(row.get("universeId") or row.get("universe_id"))
        if uid not in seen and uid != "None":
            seen.add(uid)
            result.append(row)
    return result


async def _fetch_json_with_retry(session, url, params, max_attempts=3):
    """Fetch JSON with retry mechanism for transient network failures."""
    backoffs = [1.5, 3.0, 6.0]
    for attempt in range(1, max_attempts + 1):
        try:
            async with session.get(
                url,
                params=params,
                timeout=ROBLOX_TIMEOUT,
            ) as response:
                response.raise_for_status()
                return await response.json()
        except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError, ValueError) as exc:
            log.warning(
                "[Roblox API] Request to %s (attempt %d/%d) failed: %s: %s",
                url, attempt, max_attempts, type(exc).__name__, exc
            )
            if attempt == max_attempts:
                raise
            delay = backoffs[attempt - 1] + random.uniform(0.0, 0.5)
            await asyncio.sleep(delay)


async def _get_current_chart(session):
    payload = await _fetch_json_with_retry(
        session,
        EXPLORE_SORT_CONTENT_URL,
        {
            "sessionId": SESSION_ID,
            "sortId": "top-playing-now",
            "device": "computer",
            "country": "all",
        },
    )
    return _extract_rows(payload)


async def _get_game_details(session, universe_ids):
    if not universe_ids:
        return []

    details = []
    for start in range(0, len(universe_ids), 100):
        chunk = universe_ids[start:start + 100]
        try:
            payload = await _fetch_json_with_retry(
                session,
                GAMES_URL,
                {"universeIds": ",".join(map(str, chunk))},
            )
            details.extend(payload.get("data", []))
        except Exception as exc:
            log.warning("[Roblox API] Could not fetch batch game details: %s", exc)

    return details


class RobloxLiveCache:
    """Centralized singleton cache for Roblox Top Games data.

    Queries the Roblox API once per cycle and provides fresh data to all Discord live message updaters.
    Handles rate limiting (HTTP 429), timeouts, backoff, and clean task cancellation.
    """

    def __init__(self, target_interval: float = 1.0) -> None:
        self.target_interval = target_interval
        self.last_games: list[dict] = []
        self.last_update_ts: float = 0.0
        self.last_success_datetime: datetime.datetime | None = None
        self._task: asyncio.Task | None = None
        self._running: bool = False
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "RobloxPopularGamesDiscordBot/2.0"}
            )
        return self._session

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._poll_loop())
            log.info("[LIVE GAMES] Started central Roblox Top Games cache polling loop.")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("[LIVE GAMES] Stopped central Roblox Top Games cache loop.")

    async def fetch_fresh_games(self, limit: int = 20) -> list[dict]:
        session = await self._get_session()
        rows = await _get_current_chart(session)

        games = []
        missing_root_place_uids = []

        for row in rows:
            uid_raw = row.get("universeId") or row.get("universe_id")
            if uid_raw is None:
                continue
            try:
                uid = int(uid_raw)
            except (ValueError, TypeError):
                continue

            playing = int(row.get("playerCount") or row.get("playing") or 0)
            root_place = row.get("rootPlaceId") or row.get("placeId") or row.get("root_place_id")
            name = row.get("name") or row.get("title") or "Unknown Game"

            if root_place:
                try:
                    games.append({
                        "id": uid,
                        "rootPlaceId": int(root_place),
                        "name": str(name),
                        "playing": playing,
                    })
                except (ValueError, TypeError):
                    missing_root_place_uids.append(uid)
            else:
                missing_root_place_uids.append(uid)

        if len(games) < limit and missing_root_place_uids:
            details = await _get_game_details(session, missing_root_place_uids)
            by_id = {int(g["id"]): g for g in details if g.get("id") is not None}

            for uid in missing_root_place_uids:
                g = by_id.get(uid)
                if not g:
                    continue
                root_place = g.get("rootPlaceId")
                if not root_place:
                    continue
                playing = int(g.get("playing") or 0)
                games.append({
                    "id": uid,
                    "rootPlaceId": int(root_place),
                    "name": g.get("name") or "Unknown Game",
                    "playing": playing,
                })

        games.sort(key=lambda game: game["playing"], reverse=True)
        return games[:limit]

    async def _poll_loop(self) -> None:
        consecutive_errors = 0
        backoff_delay = self.target_interval

        while self._running:
            start_time = time.monotonic()
            try:
                games = await self.fetch_fresh_games(limit=20)
                if games:
                    self.last_games = games
                    self.last_update_ts = time.time()
                    self.last_success_datetime = datetime.datetime.now(datetime.timezone.utc)
                    consecutive_errors = 0
                    backoff_delay = self.target_interval
                    log.debug("[LIVE GAMES] Roblox data updated (%d games)", len(games))
            except aiohttp.ClientResponseError as exc:
                consecutive_errors += 1
                if exc.status == 429:
                    backoff_delay = min(60.0, max(5.0, backoff_delay * 2))
                    log.warning(
                        "[LIVE GAMES] Roblox API rate limited (429). Backing off for %.1fs. Keep showing last valid data.",
                        backoff_delay
                    )
                else:
                    backoff_delay = min(30.0, max(2.0, backoff_delay * 1.5))
                    log.warning("[LIVE GAMES] Roblox API HTTP error %d: %s. Backing off %.1fs.", exc.status, exc, backoff_delay)
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                consecutive_errors += 1
                backoff_delay = min(30.0, max(2.0, backoff_delay * 1.5))
                log.warning("[LIVE GAMES] Roblox API network error (%s). Backing off %.1fs.", exc, backoff_delay)
            except Exception as exc:
                consecutive_errors += 1
                backoff_delay = min(30.0, max(2.0, backoff_delay * 1.5))
                log.exception("[LIVE GAMES] Unexpected error during Roblox API poll: %s", exc)

            elapsed = time.monotonic() - start_time
            sleep_time = max(0.1, backoff_delay - elapsed)
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break


roblox_cache = RobloxLiveCache()


async def get_top_games(limit=20):
    if roblox_cache.last_games:
        return roblox_cache.last_games[:limit]
    return await roblox_cache.fetch_fresh_games(limit=limit)
