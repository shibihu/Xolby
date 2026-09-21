import asyncio
import logging
import os
import random
import uuid
import aiohttp

log = logging.getLogger(__name__)

EXPLORE_SORT_CONTENT_URL = (
    "https://apis.roblox.com/explore-api/v1/get-sort-content"
)
GAMES_URL = "https://games.roblox.com/v1/games"

SESSION_ID = os.getenv("ROBLOX_SESSION_ID", str(uuid.uuid4()))

# Configure explicit timeout object per prompt: total=30, connect=10, sock_connect=10, sock_read=25
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
            # Chart items in Explore API contain universeId, playerCount, and placeId / rootPlaceId / name
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
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
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
    for start in range(0, len(universe_ids), 10):
        chunk = universe_ids[start:start + 10]
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


async def get_top_games(limit=20):
    async with aiohttp.ClientSession(
        headers={"User-Agent": "RobloxPopularGamesDiscordBot/2.0"}
    ) as session:
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

        # Only fetch details from games.roblox.com if Explore API didn't give us rootPlaceId for needed games
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
