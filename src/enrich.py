"""Enrich tracks via the Deezer public API: resolve local-only tracks, related artists, album genres/year.

Every GET is cached in `http_cache` (url -> json), so reruns hit the network only for new rows.
MusicBrainz tags (config `musicbrainz.enabled`) are deferred: Deezer genres cover the need for now.
"""
import json
import time

import requests

from src.db import _norm, track_key

API = "https://api.deezer.com"
THROTTLE = 0.12  # Deezer allows ~50 req / 5 s


class Deezer:
    def __init__(self, conn, session=requests, throttle=THROTTLE, retries=5, limit=None):
        self.conn, self.session, self.throttle, self.retries, self.limit = conn, session, throttle, retries, limit
        self._last, self.fetched = 0.0, 0

    def exhausted(self):
        """`--limit N` caps new network fetches per run; cached calls are free."""
        return self.limit is not None and self.fetched >= self.limit

    def get(self, path, params=None):
        url = requests.Request("GET", API + path, params=params).prepare().url
        hit = self.conn.execute("SELECT json FROM http_cache WHERE url=?", (url,)).fetchone()
        if hit:
            return json.loads(hit[0])
        if self.exhausted():
            return None  # budget spent: caller must not mark the row as done
        for attempt in range(self.retries):
            time.sleep(max(0.0, self._last + self.throttle - time.monotonic()))
            self._last = time.monotonic()
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None
            if not err:
                break
            if err.get("code") == 800:  # "no data": final answer, cache it as empty
                body = {}
                break
            if err.get("code") not in (4, 700):  # 4 = quota, 700 = service busy -> back off and retry
                return None  # unknown error: not cached, caller retries on next run
            time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError(f"Deezer still erroring after {self.retries} retries: {url}")
        self.fetched += 1
        self.conn.execute("INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?)", (url, json.dumps(body), int(time.time())))
        return body


def best_match(results, artist, title):
    """Exact normalized key match first, else first result by the same primary artist."""
    key = track_key(artist, title)
    for r in results:
        if track_key(r["artist"]["name"], r["title"]) == key:
            return r
    art, words = key.split("|")[0], set(_norm(title).split())
    return next((r for r in results if track_key(r["artist"]["name"], "").split("|")[0] == art
                 and words and words <= set(_norm(r["title"]).split())), None)


def resolve(conn, dz):
    rows = conn.execute("SELECT id, artist, title FROM tracks WHERE deezer_id IS NULL AND title IS NOT NULL").fetchall()
    ok = done = 0
    for tid, artist, title in rows:
        if dz.exhausted():
            break
        done += 1
        # ponytail: plain query; Deezer's advanced artist:"X" track:"Y" syntax returned 0 hits
        # for every track (2026-09), best_match() provides the precision instead.
        q = f"{artist or ''} {title}".strip()
        body = dz.get("/search", {"q": q})
        if body is None:  # budget spent or transient error: leave for next run
            done -= 1
            continue
        m = best_match(body.get("data", []), artist or "", title)
        if m:
            dup = conn.execute("SELECT id FROM tracks WHERE deezer_id=? AND id<>?", (m["id"], tid)).fetchone()
            if dup:
                merge(conn, tid, dup[0])
                tid = dup[0]
            # isrc not copied: UNIQUE could clash with a track already in the library
            conn.execute("UPDATE tracks SET deezer_id=?, deezer_artist_id=COALESCE(deezer_artist_id, ?),"
                         " deezer_album_id=COALESCE(deezer_album_id, ?), album=COALESCE(album, ?),"
                         " rank=COALESCE(rank, ?), duration=COALESCE(duration, ?) WHERE id=?",
                         (m["id"], m["artist"]["id"], m["album"]["id"], m["album"].get("title"),
                          m.get("rank"), m.get("duration"), tid))
            ok += 1
        conn.commit()
    return ok, done - ok


def merge(conn, src, dst):
    """Fold track `src` into `dst`: move sources, fill dst's NULL columns from src, delete src."""
    cols = [c[1] for c in conn.execute("PRAGMA table_info(tracks)") if c[1] not in ("id", "key")]
    vals = conn.execute(f"SELECT {', '.join(cols)} FROM tracks WHERE id=?", (src,)).fetchone()
    conn.execute("UPDATE track_sources SET track_id=? WHERE track_id=?", (dst, src))
    conn.execute("DELETE FROM tracks WHERE id=?", (src,))  # first, so a moved isrc can't hit UNIQUE
    conn.execute(f"UPDATE tracks SET {', '.join(f'{c}=COALESCE({c}, ?)' for c in cols)} WHERE id=?", (*vals, dst))


def enrich_artists(conn, dz):
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT deezer_artist_id FROM tracks WHERE deezer_artist_id IS NOT NULL"
        " AND deezer_artist_id NOT IN (SELECT id FROM artists)")]
    n = 0
    for aid in ids:
        a, rel = dz.get(f"/artist/{aid}"), dz.get(f"/artist/{aid}/related")
        if a is None or rel is None:  # not fetched: no row, retried next run
            continue
        for rank, r in enumerate(rel.get("data", []), 1):
            conn.execute("INSERT OR IGNORE INTO artist_related VALUES (?, ?, ?)", (aid, r["id"], rank))
        conn.execute("INSERT OR REPLACE INTO artists VALUES (?, ?, ?)", (aid, a.get("name"), a.get("nb_fan")))
        conn.commit()
        n += 1
    return n


def enrich_albums(conn, dz):
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT deezer_album_id FROM tracks WHERE deezer_album_id IS NOT NULL"
        " AND deezer_album_id NOT IN (SELECT id FROM albums)")]
    n = 0
    for alid in ids:
        al = dz.get(f"/album/{alid}")
        if al is None:
            continue
        date = al.get("release_date") or ""
        year = int(date[:4]) if date[:4].isdigit() and date[:4] != "0000" else None
        conn.execute("INSERT OR REPLACE INTO albums VALUES (?, ?, ?)", (alid, al.get("title"), year))
        for g in (al.get("genres") or {}).get("data", []):
            conn.execute("INSERT OR IGNORE INTO album_genres VALUES (?, ?)", (alid, g["name"]))
        conn.commit()
        n += 1
    return n


def fill_eras(conn):
    """Era (decade) from the album year for every track still missing one."""
    conn.execute("UPDATE tracks SET era = (a.year / 10 * 10) || 's' FROM albums a"
                 " WHERE a.id = tracks.deezer_album_id AND a.year IS NOT NULL AND tracks.era IS NULL")
    conn.commit()


def enrich(conn, session=requests, limit=None, throttle=THROTTLE):
    dz = Deezer(conn, session, throttle, limit=limit)
    ok, failed = resolve(conn, dz)
    n_art = enrich_artists(conn, dz)
    n_alb = enrich_albums(conn, dz)
    fill_eras(conn)
    return {"resolved": ok, "unresolved": failed, "artists": n_art, "albums": n_alb, "http_fetched": dz.fetched}
