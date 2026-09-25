#!/usr/bin/env python3
"""
Generate pluto-map.json for One Piece on Pluto TV.

Current Pluto flow:
1. Get a temporary session token from boot.pluto.tv/v4/start.
2. Query the v4 VOD seasons endpoint for One Piece.
3. Match Pluto episode titles against TVMaze's numbered One Piece list.
4. Write exact Pluto episode-page URLs into pluto-map.json.

This only uses public metadata and page IDs. It does not copy or proxy video.
"""

import datetime
import json
import re
import time
import unicodedata
import uuid
from difflib import SequenceMatcher
from pathlib import Path

import requests

PLUTO_SERIES_ID = "1550009"
PLUTO_BOOT_URL = "https://boot.pluto.tv/v4/start"
PLUTO_SEASONS_URL = (
    f"https://service-vod.clusters.pluto.tv/v4/vod/series/"
    f"{PLUTO_SERIES_ID}/seasons"
)
PLUTO_SHOW_BASE = f"https://pluto.tv/us/shows/{PLUTO_SERIES_ID}/episode"

TVMAZE_SHOW_ID = "1505"
TVMAZE_API = f"https://api.tvmaze.com/shows/{TVMAZE_SHOW_ID}/episodes"

OUT = Path("pluto-map.json")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Origin": "https://pluto.tv",
    "Referer": "https://pluto.tv/",
})

def get_json(url, params=None, headers=None, retries=4, pause=2):
    last = None
    for attempt in range(retries):
        try:
            r = SESSION.get(
                url,
                params=params,
                headers=headers,
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(pause * (attempt + 1))
    raise RuntimeError(f"Request failed: {url}: {last}")

def normalize_title(value):
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", str(value))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("&", " and ")
    s = s.replace("’", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"\bone piece\b", " ", s)
    s = re.sub(r"\bepisode\s*\d+\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

def title_similarity(a, b):
    a = normalize_title(a)
    b = normalize_title(b)

    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    if a in b or b in a:
        shorter = min(len(a), len(b))
        longer = max(len(a), len(b))
        if longer and shorter / longer >= 0.72:
            return 0.95

    return SequenceMatcher(None, a, b).ratio()

def fetch_tvmaze_titles():
    payload = get_json(TVMAZE_API)

    if not isinstance(payload, list) or not payload:
        raise RuntimeError("TVMaze returned no One Piece episode data.")

    titles = {}

    for global_number, ep in enumerate(payload, start=1):
        title = ep.get("name") or f"Episode {global_number}"
        titles[global_number] = [title]

    print(f"TVMaze returned {len(titles)} numbered One Piece episodes.")
    return titles

def fetch_pluto_token():
    params = {
        "appName": "web",
        "appVersion": "8.0.0",
        "deviceVersion": "129.0.0",
        "deviceModel": "web",
        "deviceMake": "chrome",
        "deviceType": "web",
        "clientID": str(uuid.uuid4()),
        "clientModelNumber": "1.0.0",
        "serverSideAds": "false",
        "drmCapabilities": "widevine:L3",
        "seriesIDs": PLUTO_SERIES_ID,
        "clientTime": datetime.datetime.now(datetime.UTC).isoformat(),
    }

    payload = get_json(PLUTO_BOOT_URL, params=params)
    token = payload.get("sessionToken")

    if not token:
        raise RuntimeError("Pluto boot API returned no sessionToken.")

    print("Pluto boot token acquired.")
    return token

def looks_like_episode(obj):
    if not isinstance(obj, dict):
        return False

    content_id = obj.get("_id") or obj.get("id") or obj.get("contentId")
    title = obj.get("name") or obj.get("title")
    kind = str(obj.get("type") or obj.get("kind") or "").lower()

    has_episode_marker = any(
        key in obj
        for key in (
            "episode",
            "episodeNumber",
            "episode_number",
            "episodeNum",
            "number",
        )
    )

    return bool(
        content_id
        and title
        and (has_episode_marker or "episode" in kind)
    )

def walk_episode_objects(node, out, seen):
    if isinstance(node, dict):
        if looks_like_episode(node):
            cid = str(
                node.get("_id")
                or node.get("id")
                or node.get("contentId")
            )

            if cid not in seen:
                seen.add(cid)
                out.append(node)

        for value in node.values():
            walk_episode_objects(value, out, seen)

    elif isinstance(node, list):
        for value in node:
            walk_episode_objects(value, out, seen)

def fetch_pluto_episodes():
    token = fetch_pluto_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json,text/plain,*/*",
        "Origin": "https://pluto.tv",
        "Referer": "https://pluto.tv/",
        "User-Agent": SESSION.headers["User-Agent"],
    }

    payload = get_json(
        PLUTO_SEASONS_URL,
        params={
            "offset": "1000",
            "page": "1",
        },
        headers=headers,
    )

    episodes = []
    walk_episode_objects(payload, episodes, set())

    if not episodes:
        raise RuntimeError(
            "Pluto v4 returned no episode objects. "
            "The catalog shape may have changed."
        )

    print(f"Pluto returned {len(episodes)} episode objects.")
    return episodes

def possible_episode_number(obj):
    for key in (
        "episode",
        "episodeNumber",
        "episode_number",
        "episodeNum",
        "number",
    ):
        value = obj.get(key)

        if value is None:
            continue

        match = re.search(r"\d+", str(value))

        if match:
            return int(match.group())

    return None

def best_match(title, canonical_titles, used):
    nt = normalize_title(title)

    if not nt:
        return None, 0.0

    exact = []

    for num, variants in canonical_titles.items():
        if num in used:
            continue

        if any(normalize_title(v) == nt for v in variants):
            exact.append(num)

    if len(exact) == 1:
        return exact[0], 1.0

    best_num = None
    best_score = 0.0

    for num, variants in canonical_titles.items():
        if num in used:
            continue

        score = max(
            title_similarity(title, variant)
            for variant in variants
        )

        if score > best_score:
            best_score = score
            best_num = num

    return best_num, best_score

def make_pluto_url(content_id):
    return f"{PLUTO_SHOW_BASE}/{content_id}"

def main():
    canonical_titles = fetch_tvmaze_titles()
    pluto_eps = fetch_pluto_episodes()

    mapped = {}
    details = {}
    used = set()
    unmatched = []
    leftovers = []

    # First pass: episode number + title agreement.
    for item in pluto_eps:
        title = item.get("name") or item.get("title") or ""
        cid = str(
            item.get("_id")
            or item.get("id")
            or item.get("contentId")
        )

        hinted = possible_episode_number(item)

        if hinted in canonical_titles and hinted not in used:
            score = max(
                title_similarity(title, v)
                for v in canonical_titles[hinted]
            )

            if score >= 0.72:
                mapped[str(hinted)] = make_pluto_url(cid)
                details[str(hinted)] = {
                    "title": title,
                    "matchScore": round(score, 3),
                    "matchMethod": "number+title",
                }
                used.add(hinted)
                continue

        leftovers.append(item)

    # Second pass: title matching.
    for item in leftovers:
        title = item.get("name") or item.get("title") or ""
        cid = str(
            item.get("_id")
            or item.get("id")
            or item.get("contentId")
        )

        num, score = best_match(
            title,
            canonical_titles,
            used,
        )

        if num is not None and score >= 0.78:
            mapped[str(num)] = make_pluto_url(cid)
            details[str(num)] = {
                "title": title,
                "matchScore": round(score, 3),
                "matchMethod": "title",
            }
            used.add(num)
        else:
            unmatched.append({
                "title": title,
                "contentId": cid,
                "bestEpisode": num,
                "bestScore": round(score, 3),
            })

    mapped = dict(
        sorted(
            mapped.items(),
            key=lambda kv: int(kv[0]),
        )
    )

    details = dict(
        sorted(
            details.items(),
            key=lambda kv: int(kv[0]),
        )
    )

    output = {
        "series": "One Piece",
        "plutoSeriesId": PLUTO_SERIES_ID,
        "referenceSource": "TVMaze One Piece show 1505",
        "source": "Generated by GitHub Actions from Pluto TV v4 catalog metadata",
        "mappedCount": len(mapped),
        "plutoEpisodeObjectsFound": len(pluto_eps),
        "episodes": mapped,
        "details": details,
        "unmatchedCount": len(unmatched),
        "unmatchedSample": unmatched[:40],
    }

    OUT.write_text(
        json.dumps(
            output,
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )

    print(f"Mapped {len(mapped)} direct Pluto episode links.")
    print(f"Unmatched Pluto episode objects: {len(unmatched)}")

    if len(mapped) < 25:
        raise RuntimeError(
            f"Only {len(mapped)} Pluto episodes mapped. "
            "Refusing to treat this as a successful update."
        )

if __name__ == "__main__":
    main()
