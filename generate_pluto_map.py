#!/usr/bin/env python3
import datetime
import json
import re
import time
import unicodedata
import uuid
from difflib import SequenceMatcher
from pathlib import Path

import requests

PUBLIC_ROUTE_ID = "1550009"
PLUTO_SERIES_SLUG = "one-piece-eng"
PLUTO_BOOT_URL = "https://boot.pluto.tv/v4/start"
PLUTO_VOD_BASE = "https://service-vod.clusters.pluto.tv/v4/vod"
PLUTO_SHOW_BASE = f"https://pluto.tv/us/shows/{PUBLIC_ROUTE_ID}/episode"
TVMAZE_SHOW_ID = "1505"
TVMAZE_API = f"https://api.tvmaze.com/shows/{TVMAZE_SHOW_ID}/episodes"
OUT = Path("pluto-map.json")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Origin": "https://pluto.tv",
    "Referer": "https://pluto.tv/",
})

def request_json(url, params=None, headers=None, retries=3):
    last = None
    for attempt in range(retries):
        try:
            response = SESSION.get(url, params=params, headers=headers, timeout=30)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(2 * (attempt + 1))
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
    payload = request_json(TVMAZE_API)
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("TVMaze returned no One Piece episode data.")
    titles = {}
    for number, episode in enumerate(payload, start=1):
        title = episode.get("name") or f"Episode {number}"
        titles[number] = [title]
    print(f"TVMaze returned {len(titles)} numbered One Piece episodes.")
    return titles

def pluto_boot():
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
        "seriesIDs": PUBLIC_ROUTE_ID,
        "clientTime": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    payload = request_json(PLUTO_BOOT_URL, params=params)
    if not isinstance(payload, dict):
        raise RuntimeError("Pluto boot API returned no JSON object.")
    token = payload.get("sessionToken")
    if not token:
        raise RuntimeError("Pluto boot API returned no sessionToken.")
    print("Pluto boot token acquired.")
    return token, payload

def episode_object(obj):
    if not isinstance(obj, dict):
        return False
    content_id = obj.get("_id") or obj.get("id") or obj.get("contentId")
    title = obj.get("name") or obj.get("title")
    kind = str(obj.get("type") or obj.get("kind") or "").lower()
    has_marker = any(
        key in obj
        for key in ("episode", "episodeNumber", "episode_number", "episodeNum", "number")
    )
    return bool(content_id and title and (has_marker or "episode" in kind))

def collect_episode_objects(node):
    found = []
    seen = set()

    def walk(value):
        if isinstance(value, dict):
            if episode_object(value):
                cid = str(value.get("_id") or value.get("id") or value.get("contentId"))
                if cid not in seen:
                    seen.add(cid)
                    found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found

def auth_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json,text/plain,*/*",
        "Origin": "https://pluto.tv",
        "Referer": "https://pluto.tv/",
        "User-Agent": SESSION.headers["User-Agent"],
    }

def try_seasons_lookup(identifier, token):
    url = f"{PLUTO_VOD_BASE}/series/{identifier}/seasons"
    headers = auth_headers(token)
    attempts = [
        None,
        {"offset": "1000", "page": "1"},
        {"includeItems": "true", "deviceType": "web"},
    ]

    for params in attempts:
        payload = request_json(url, params=params, headers=headers)
        if payload is None:
            continue

        episodes = collect_episode_objects(payload)
        if episodes:
            print(
                f"Pluto VOD lookup succeeded with identifier "
                f"{identifier!r}: {len(episodes)} episode objects."
            )
            return episodes

    return []

def resolve_slug(token):
    url = f"{PLUTO_VOD_BASE}/slugs"
    payload = request_json(
        url,
        params={"slugs": PLUTO_SERIES_SLUG},
        headers=auth_headers(token),
    )

    if not payload:
        return None

    candidates = []

    def walk(value):
        if isinstance(value, dict):
            slug = str(value.get("slug") or "")
            item_id = (
                value.get("_id")
                or value.get("id")
                or value.get("seriesId")
                or value.get("contentId")
            )

            if item_id and (
                slug == PLUTO_SERIES_SLUG
                or "one piece" in str(value.get("name") or "").lower()
            ):
                candidates.append(str(item_id))

            for child in value.values():
                walk(child)

        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)

    for candidate in candidates:
        if candidate and candidate != PUBLIC_ROUTE_ID:
            print(f"Resolved Pluto internal series id: {candidate}")
            return candidate

    return None

def fetch_pluto_episodes():
    token, boot_payload = pluto_boot()

    boot_vod = boot_payload.get("VOD")
    if boot_vod:
        episodes = collect_episode_objects(boot_vod)
        if episodes:
            print(
                f"Pluto boot response contained "
                f"{len(episodes)} episode objects."
            )
            return episodes

    episodes = try_seasons_lookup(PLUTO_SERIES_SLUG, token)
    if episodes:
        return episodes

    internal_id = resolve_slug(token)
    if internal_id:
        episodes = try_seasons_lookup(internal_id, token)
        if episodes:
            return episodes

    episodes = try_seasons_lookup(PUBLIC_ROUTE_ID, token)
    if episodes:
        return episodes

    raise RuntimeError(
        "Pluto authentication worked, but no One Piece VOD season endpoint "
        "returned episode metadata."
    )

def possible_episode_number(obj):
    for key in ("episode", "episodeNumber", "episode_number", "episodeNum", "number"):
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

        score = max(title_similarity(title, v) for v in variants)

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
    leftovers = []
    unmatched = []

    for item in pluto_eps:
        title = item.get("name") or item.get("title") or ""
        cid = str(item.get("_id") or item.get("id") or item.get("contentId"))
        hinted = possible_episode_number(item)

        if hinted in canonical_titles and hinted not in used:
            score = max(title_similarity(title, variant) for variant in canonical_titles[hinted])
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

    for item in leftovers:
        title = item.get("name") or item.get("title") or ""
        cid = str(item.get("_id") or item.get("id") or item.get("contentId"))
        number, score = best_match(title, canonical_titles, used)

        if number is not None and score >= 0.78:
            mapped[str(number)] = make_pluto_url(cid)
            details[str(number)] = {
                "title": title,
                "matchScore": round(score, 3),
                "matchMethod": "title",
            }
            used.add(number)
        else:
            unmatched.append({
                "title": title,
                "contentId": cid,
                "bestEpisode": number,
                "bestScore": round(score, 3),
            })

    mapped = dict(sorted(mapped.items(), key=lambda item: int(item[0])))
    details = dict(sorted(details.items(), key=lambda item: int(item[0])))

    output = {
        "series": "One Piece",
        "plutoPublicRouteId": PUBLIC_ROUTE_ID,
        "plutoSeriesSlug": PLUTO_SERIES_SLUG,
        "referenceSource": "TVMaze One Piece show 1505",
        "source": "Generated by GitHub Actions from Pluto TV catalog metadata",
        "mappedCount": len(mapped),
        "plutoEpisodeObjectsFound": len(pluto_eps),
        "episodes": mapped,
        "details": details,
        "unmatchedCount": len(unmatched),
        "unmatchedSample": unmatched[:40],
    }

    OUT.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
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
