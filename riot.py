"""Riot Games API: look up Riot IDs and find finished ranked Flex games (queue 440).

Needs RIOT_API_KEY in .env (from developer.riotgames.com).
"""
import asyncio
import os
from urllib.parse import quote

FLEX_QUEUE = 440
ACCOUNT_HOST = "https://asia.api.riotgames.com"  # Riot accounts are global; any regional host works
# which match-history cluster each LoL server belongs to (Hong Kong / Taiwan = tw2 = sea)
CLUSTER = {
    "tw2": "sea", "sg2": "sea", "ph2": "sea", "th2": "sea", "vn2": "sea", "oc1": "sea",
    "kr": "asia", "jp1": "asia",
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
}
DEFAULT_SERVER = "tw2"


class RiotError(Exception):
    pass


def api_key():
    return os.environ.get("RIOT_API_KEY", "")


async def get(session, url, params=None):
    """GET a Riot API url; waits out rate limits. Returns parsed JSON, or None for 404."""
    for _ in range(5):
        async with session.get(url, params=params, headers={"X-Riot-Token": api_key()}) as r:
            if r.status == 429:  # rate limited: Riot says how long to wait
                await asyncio.sleep(int(r.headers.get("Retry-After", "5")))
                continue
            if r.status >= 500:
                await asyncio.sleep(3)
                continue
            if r.status == 404:
                return None
            if r.status != 200:
                raise RiotError(f"HTTP {r.status}: {(await r.text())[:200]}")
            return await r.json()
    raise RiotError("Riot API kept failing")


async def account(session, riot_id):
    """'名字#TAG' -> {'puuid', 'gameName', 'tagLine'}, or None if there's no such account."""
    name, _, tag = riot_id.strip().rpartition("#")
    if not name or not tag:
        return None
    return await get(session, f"{ACCOUNT_HOST}/riot/account/v1/accounts/by-riot-id/{quote(name)}/{quote(tag)}")


async def server(session, puuid):
    """The LoL server the account plays on, e.g. 'tw2'."""
    try:
        data = await get(session, f"{ACCOUNT_HOST}/riot/account/v1/region/by-game/lol/by-puuid/{puuid}")
        if data and data.get("region", "").lower() in CLUSTER:
            return data["region"].lower()
    except RiotError:
        pass
    return DEFAULT_SERVER


def host(srv):
    return f"https://{CLUSTER.get(srv, 'sea')}.api.riotgames.com"


async def flex_match_ids(session, puuid, srv, since):
    """IDs of ranked Flex games the player started after `since` (unix seconds), newest first."""
    return await get(session, f"{host(srv)}/lol/match/v5/matches/by-puuid/{puuid}/ids",
                     {"queue": FLEX_QUEUE, "startTime": since, "count": 20}) or []


async def match(session, srv, match_id):
    return await get(session, f"{host(srv)}/lol/match/v5/matches/{match_id}")
