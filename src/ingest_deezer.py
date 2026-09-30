"""Fetch all tracks of a public Deezer playlist (no auth) into SQLite."""
import sys

import requests

from src.db import upsert

API = "https://api.deezer.com/playlist/{}/tracks"


def fetch_tracks(playlist_id, session=requests):
    url, params, tracks, total = API.format(playlist_id), {"index": 0, "limit": 100}, [], None
    while url:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        page = resp.json()
        if "error" in page:  # Deezer returns HTTP 200 with an error body
            raise RuntimeError(f"Deezer API error: {page['error']}")
        tracks += page.get("data", [])
        total = page.get("total", total)
        url, params = page.get("next"), None  # `next` already carries index/limit
    if total is not None and len(tracks) != total:
        # `total` can count region-unavailable tracks: warn, keep what we got
        print(f"warning: Deezer returned {len(tracks)} tracks, playlist total is {total}", file=sys.stderr)
    return tracks


def ingest(conn, playlist_id, session=requests):
    tracks = fetch_tracks(playlist_id, session)
    for t in tracks:
        upsert(conn, "deezer", t["id"], {
            "artist": t["artist"]["name"], "title": t["title"], "isrc": t.get("isrc") or None,
            "deezer_id": t["id"], "deezer_artist_id": t["artist"].get("id"),
            "album": (t.get("album") or {}).get("title"), "deezer_album_id": (t.get("album") or {}).get("id"),
            "rank": t.get("rank"), "duration": t.get("duration"), "added_at": t.get("time_add"),
        })
    conn.commit()
    return len(tracks)
