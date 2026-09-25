import json
import re
from pathlib import Path

from crunchyroll_api import CrunchyrollClient

SERIES_ID = "GRMG8ZQZR"  # One Piece (US Crunchyroll catalog)
OUT = Path(__file__).resolve().parents[1] / "crunchyroll-map.json"


def as_int_episode(value):
    if value is None:
        return None
    text = str(value).strip()
    m = re.fullmatch(r"0*(\d+)(?:\.0+)?", text)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 5000 else None


def season_rank(season):
    title = (getattr(season, "title", "") or "").lower()
    audio = (getattr(season, "audio_locale", "") or "").lower()
    score = 0
    # Prefer the normal Japanese-source season rows.
    if audio in {"ja-jp", "ja_jp", "ja"}:
        score += 100
    # Prefer regular catalog seasons over duplicate HD/special-edition rows.
    for bad in ("special edition", "international", "heroines"):
        if bad in title:
            score -= 50
    return score


def main():
    client = CrunchyrollClient(locale="en-US")
    client.login_anonymous()

    seasons = list(client.get_seasons(SERIES_ID))
    seasons.sort(key=season_rank, reverse=True)

    episode_map = {}
    titles = {}
    seen_seasons = 0

    for season in seasons:
        title = getattr(season, "title", "") or ""
        # Do not let obvious duplicate/special collections overwrite normal episodes.
        low = title.lower()
        if any(x in low for x in ("international", "heroines")):
            continue
        try:
            eps = client.get_episodes(season.id)
        except Exception as exc:
            print(f"Skipping season {title!r}: {exc}")
            continue

        seen_seasons += 1
        for ep in eps:
            number = as_int_episode(getattr(ep, "episode_number", None))
            if number is None:
                continue
            ep_id = getattr(ep, "id", None)
            if not ep_id:
                continue
            # First good match wins because seasons were ranked by preference.
            if str(number) not in episode_map:
                episode_map[str(number)] = f"https://www.crunchyroll.com/watch/{ep_id}"
                titles[str(number)] = getattr(ep, "title", "") or ""

    # Numeric ordering makes diffs readable.
    episode_map = dict(sorted(episode_map.items(), key=lambda kv: int(kv[0])))
    titles = dict(sorted(titles.items(), key=lambda kv: int(kv[0])))

    payload = {
        "series": "One Piece",
        "seriesId": SERIES_ID,
        "source": "Crunchyroll public catalog metadata (anonymous)",
        "mappedCount": len(episode_map),
        "seasonsScanned": seen_seasons,
        "episodes": episode_map,
        "titles": titles,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(episode_map)} direct episode links to {OUT}")


if __name__ == "__main__":
    main()
