import os
import uuid
import aiohttp

# Roblox's current Discover/Charts endpoint.
# Roblox deprecated the old /v1/games/list and /v1/games/sorts endpoints.
EXPLORE_SORT_CONTENT_URL = (
    "https://apis.roblox.com/explore-api/v1/get-sort-content"
)
GAMES_URL = "https://games.roblox.com/v1/games"

SESSION_ID = os.getenv("ROBLOX_SESSION_ID", str(uuid.uuid4()))


def _extract_rows(payload):
    """Find game-like rows without depending on one exact undocumented schema."""
    rows = []

    def walk(value):
        if isinstance(value, dict):
            # A current chart row normally contains universeId + playerCount.
            if value.get("universeId") is not None:
                rows.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)

    # De-duplicate while preserving the API's order.
    seen = set()
    result = []
    for row in rows:
        uid = str(row.get("universeId"))
        if uid not in seen:
            seen.add(uid)
            result.append(row)
    return result


async def _fetch_json(session, url, params):
    async with session.get(
        url,
        params=params,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        response.raise_for_status()
        return await response.json()


async def _get_current_chart(session):
    """
    Get Roblox's 'Top Playing Now' chart.

    This is the important change from the first version: we no longer use
    a hard-coded list of five games.
    """
    payload = await _fetch_json(
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
    """Fetch stable game details such as name, rootPlaceId and playing."""
    if not universe_ids:
        return []

    # Roblox has a batch-size limit; use chunks of 10.
    details = []
    for start in range(0, len(universe_ids), 10):
        chunk = universe_ids[start:start + 10]
        payload = await _fetch_json(
            session,
            GAMES_URL,
            {"universeIds": ",".join(map(str, chunk))},
        )
        details.extend(payload.get("data", []))

    return details


async def get_top_games(limit=20):
    async with aiohttp.ClientSession(
        headers={"User-Agent": "RobloxPopularGamesDiscordBot/2.0"}
    ) as session:
        rows = await _get_current_chart(session)

        # The chart itself is already ordered by current player count,
        # but we still sort by the live 'playing' value after getting
        # stable game details.
        universe_ids = []
        for row in rows:
            uid = row.get("universeId")
            if uid is not None:
                universe_ids.append(int(uid))

        details = await _get_game_details(session, universe_ids)

        by_id = {int(g["id"]): g for g in details if g.get("id") is not None}

        games = []
        for row in rows:
            try:
                uid = int(row["universeId"])
            except (KeyError, TypeError, ValueError):
                continue

            g = by_id.get(uid)
            if not g:
                continue

            # Prefer the live/stable game API value.
            playing = int(g.get("playing") or row.get("playerCount") or 0)
            root_place = g.get("rootPlaceId")

            if not root_place:
                continue

            games.append({
                "id": uid,
                "rootPlaceId": int(root_place),
                "name": g.get("name") or row.get("name") or "Unknown Game",
                "playing": playing,
            })

        games.sort(key=lambda game: game["playing"], reverse=True)
        return games[:limit]
