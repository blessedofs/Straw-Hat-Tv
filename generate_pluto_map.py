#!/usr/bin/env python3
"""
Generate pluto-map.json for One Piece on Pluto TV.

The script:
1. Pulls Pluto's One Piece VOD catalog.
2. Pulls the numbered One Piece episode list from Jikan/MyAnimeList.
3. Matches Pluto episode titles to canonical episode numbers.
4. Writes direct Pluto episode URLs keyed by anime episode number.

Only metadata and public page IDs are used. The video stream itself is never copied.
"""

import json
import re
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import requests

PLUTO_SERIES_ID = "1550009"
PLUTO_API = f"https://api.pluto.tv/v3/vod/series/{PLUTO_SERIES_ID}/seasons"
PLUTO_SHOW_BASE = f"https://pluto.tv/us/shows/{PLUTO_SERIES_ID}/episode"
JIKAN_API = "https://api.jikan.moe/v4/anime/21/episodes"
OUT = Path("pluto-map.json")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Origin": "https://pluto.tv",
    "Referer": "https://pluto.tv/",
})

def get_json(url, params=None, retries=4, pause=2):
    last = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=30)
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
    return SequenceMatcher(None, a, b).ratio()

def fetch_jikan_titles():
    titles = {}
    page = 1
    while True:
        payload = get_json(JIKAN_API, params={"page": page})
        for ep in payload.get("data", []):
            num = ep.get("mal_id")
            if not isinstance(num, int):
                continue
            names = [
                ep.get("title"),
                ep.get("title_japanese"),
                ep.get("title_romanji"),
            ]
            titles[num] = [x for x in names if x]
        pagination = payload.get("pagination") or {}
        if not pagination.get("has_next_page"):
            break
        page += 1
        time.sleep(0.7)
    if not titles:
        raise RuntimeError("Jikan returned no One Piece episode titles.")
    return titles

def looks_like_episode(obj):
    if not isinstance(obj, dict):
        return False
    content_id = obj.get("_id") or obj.get("id") or obj.get("contentId")
    title = obj.get("name") or obj.get("title")
    has_episode_marker = any(k in obj for k in (
        "episode", "episodeNumber", "episode_number", "episodeNum", "number"
    ))
    kind = str(obj.get("type") or obj.get("kind") or "").lower()
    return bool(content_id and title and (has_episode_marker or "episode" in kind))

def walk_episode_objects(node, out, seen):
    if isinstance(node, dict):
        if looks_like_episode(node):
            cid = str(node.get("_id") or node.get("id") or node.get("contentId"))
            if cid not in seen:
                seen.add(cid)
                out.append(node)
        for value in node.values():
            walk_episode_objects(value, out, seen)
    elif isinstance(node, list):
        for value in node:
            walk_episode_objects(value, out, seen)

def fetch_pluto_episodes():
    payload = get_json(
        PLUTO_API,
        params={"includeItems": "true", "deviceType": "web"}
    )
    episodes = []
    walk_episode_objects(payload, episodes, set())
    if not episodes:
        raise RuntimeError("Pluto returned no episode objects.")
    return episodes

def best_match(title, jikan_titles, used):
    nt = normalize_title(title)
    if not nt:
        return None, 0.0

    exact = []
    for num, variants in jikan_titles.items():
        if num in used:
            continue
        if any(normalize_title(v) == nt for v in variants):
            exact.append(num)
    if len(exact) == 1:
        return exact[0], 1.0

    best_num = None
    best_score = 0.0
    for num, variants in jikan_titles.items():
        if num in used:
            continue
        score = max(title_similarity(title, v) for v in variants)
        if score > best_score:
            best_score = score
            best_num = num
    return best_num, best_score

def possible_episode_number(obj):
    for key in ("episode", "episodeNumber", "episode_number", "episodeNum", "number"):
        value = obj.get(key)
        if value is None:
            continue
        m = re.search(r"\d+", str(value))
        if m:
            return int(m.group())
    return None

def main():
    jikan_titles = fetch_jikan_titles()
    pluto_eps = fetch_pluto_episodes()

    mapped = {}
    details = {}
    used = set()
    unmatched = []

    # First pass: trust Pluto's number only when the title strongly agrees.
    leftovers = []
    for item in pluto_eps:
        title = item.get("name") or item.get("title") or ""
        cid = str(item.get("_id") or item.get("id") or item.get("contentId"))
        hinted = possible_episode_number(item)

        if hinted in jikan_titles and hinted not in used:
            score = max(title_similarity(title, v) for v in jikan_titles[hinted])
            if score >= 0.72:
                mapped[str(hinted)] = f"{PLUTO_SHOW_BASE}/{cid}"
                details[str(hinted)] = {"title": title, "matchScore": round(score, 3)}
                used.add(hinted)
                continue
        leftovers.append(item)

    # Second pass: match by title. High threshold prevents false episode links.
    for item in leftovers:
        title = item.get("name") or item.get("title") or ""
        cid = str(item.get("_id") or item.get("id") or item.get("contentId"))
        num, score = best_match(title, jikan_titles, used)

        if num is not None and score >= 0.78:
            mapped[str(num)] = f"{PLUTO_SHOW_BASE}/{cid}"
            details[str(num)] = {"title": title, "matchScore": round(score, 3)}
            used.add(num)
        else:
            unmatched.append({"title": title, "contentId": cid, "bestScore": round(score, 3)})

    mapped = dict(sorted(mapped.items(), key=lambda kv: int(kv[0])))
    details = dict(sorted(details.items(), key=lambda kv: int(kv[0])))

    output = {
        "series": "One Piece",
        "plutoSeriesId": PLUTO_SERIES_ID,
        "source": "Generated by GitHub Actions from Pluto TV public catalog metadata",
        "mappedCount": len(mapped),
        "plutoEpisodeObjectsFound": len(pluto_eps),
        "episodes": mapped,
        "details": details,
        "unmatchedCount": len(unmatched),
        "unmatchedSample": unmatched[:30],
    }

    OUT.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Found {len(pluto_eps)} Pluto episode objects.")
    print(f"Mapped {len(mapped)} episodes to direct Pluto links.")
    print(f"Unmatched: {len(unmatched)}")

    if len(mapped) < 25:
        raise RuntimeError(
            f"Only {len(mapped)} Pluto episodes mapped. "
            "Refusing to treat this as a successful catalog update."
        )

if __name__ == "__main__":
    main()
