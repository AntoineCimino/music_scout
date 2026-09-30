"""Curator recommendations: public Deezer playlists (and their owners) overlapping the library yet bringing new tracks.

Deezer has no curator concept: a "curator" is the creator of one or more top-scoring public playlists.

Per playlist (first `pages` x 100 tracks):
  overlap = share of tracks already in the library, or by a library artist
  novelty = share of tracks NOT in the library by an artist in library ∪ recommended candidates
            (new music from a sound we already know we like; random unrelated tracks earn nothing)
  score   = overlap * (1 - |novelty - NOVELTY_TARGET|) * size_penalty
  size_penalty = min(1, sqrt(BIG / nb_tracks)): huge "generic" dumps rank lower.
  diversity    = min(1, distinct_artists / MIN_ARTISTS): single-artist discographies are not curation.
  feedback     = x mode multiplier from rated suggestions (see src/feedback.py).
Candidate playlists are taken round-robin across modes so the cap never starves smaller modes.
Curator score = best playlist score * (1 + 0.1 * (n_matching_playlists - 1)), bonus capped at +30%.
"""
import itertools
import math
import re
import sys
from collections import defaultdict

import requests

from src import enrich, feedback, recommend_artists

NOVELTY_TARGET, BIG, MIN_TRACKS, MIN_ARTISTS = 0.6, 200, 10, 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY, title TEXT, link TEXT, nb_tracks INTEGER, creator_id INTEGER, creator_name TEXT,
    editorial INTEGER, mode_id INTEGER, overlap REAL, novelty REAL, score REAL
);
CREATE TABLE IF NOT EXISTS curators (
    id INTEGER PRIMARY KEY, name TEXT, editorial INTEGER, n_playlists INTEGER, best_playlist_id INTEGER, score REAL
);
"""


def is_editorial(creator):
    # ponytail: Deezer editors are plain users named "<Name> - Deezer <Genre> Editor"; no API flag exists
    name = (creator.get("name") or "").lower()
    editor = r"\b[eé]dit(?:or|rice|eur|ora)\b"  # EN / FR / PT titles
    return int(bool(("deezer" in name and re.search(editor, name)) or re.search(r" - .*" + editor + "$", name)))


def queries(conn, n_artists=5, n_genres=2):
    """{mode_id: [query strings]}: mode name, its top genres and top library artists (by track count)."""
    out = {}
    for mid, name in conn.execute("SELECT mode_id, name FROM modes ORDER BY weight DESC"):
        genres = [g for g, in conn.execute(
            "SELECT g.genre FROM mode_artists m JOIN tracks t ON t.deezer_artist_id = m.artist_id"
            " JOIN album_genres g ON g.album_id = t.deezer_album_id WHERE m.mode_id=? GROUP BY 1 ORDER BY COUNT(*) DESC, 1 LIMIT ?",
            (mid, n_genres))]
        artists = [a for a, in conn.execute(
            "SELECT COALESCE(a.name, MIN(t.artist)) FROM mode_artists m JOIN tracks t ON t.deezer_artist_id = m.artist_id"
            " LEFT JOIN artists a ON a.id = m.artist_id WHERE m.mode_id=? GROUP BY m.artist_id ORDER BY COUNT(*) DESC, 1 LIMIT ?",
            (mid, n_artists))]
        out[mid] = list(dict.fromkeys(q for q in [name, *genres, *artists] if q))
    return out


def score_playlist(tracks, lib_tracks, lib_artists, known_artists):
    """Returns (overlap, novelty, raw score before size penalty)."""
    n = len(tracks) or 1
    in_lib = [t["id"] in lib_tracks or t["artist"]["id"] in lib_artists for t in tracks]
    novel = [t["id"] not in lib_tracks and t["artist"]["id"] in known_artists for t in tracks]
    overlap, novelty = sum(in_lib) / n, sum(novel) / n
    return overlap, novelty, overlap * (1 - abs(novelty - NOVELTY_TARGET))


def _safe(dz, path, params=None):
    try:
        return dz.get(path, params) or {}
    except (requests.RequestException, RuntimeError) as e:  # one bad call must not sink the run
        print(f"deezer call failed {path}: {e}", file=sys.stderr)
        return {}


def run(conn, own_playlist_id=None, max_playlists=150, per_query=25, pages=1, session=requests):
    """Search, fetch, score and store playlists + curators. Returns number of playlists scored."""
    conn.executescript(SCHEMA + "DELETE FROM playlists; DELETE FROM curators;")
    dz = enrich.Deezer(conn, session=session)
    rated = {(t, str(i)) for t, i in conn.execute("SELECT item_type, item_id FROM feedback")}
    own_user = _safe(dz, f"/playlist/{own_playlist_id}").get("creator", {}).get("id") if own_playlist_id else None
    if own_playlist_id and own_user is None:
        print("warning: could not resolve the creator of deezer.playlist_id; your own playlists may be recommended",
              file=sys.stderr)
    lib_tracks = {r[0] for r in conn.execute("SELECT deezer_id FROM tracks WHERE deezer_id IS NOT NULL")}
    lib_artists = {r[0] for r in conn.execute("SELECT DISTINCT deezer_artist_id FROM tracks WHERE deezer_artist_id IS NOT NULL")}
    # liked/known artists: excluded from candidate scoring, still "our sound" (with or without a resolved mode)
    seeds = {int(i) for i, in conn.execute("SELECT item_id FROM feedback WHERE item_type='artist'"
                                           " AND verdict IN ('like', 'known')") if str(i).isdigit()}
    known = lib_artists | set(recommend_artists.score(conn)) | seeds
    mult = feedback.multipliers(conn)

    per_mode = []
    for mid, qs in queries(conn).items():
        hits = [(p, mid) for q in qs for p in _safe(dz, "/search/playlist", {"q": q, "limit": per_query}).get("data", [])]
        per_mode.append(hits)
    cands = {}  # playlist id -> (search hit, mode_id of first query that found it), round-robin over modes
    for row in itertools.zip_longest(*per_mode):
        for c in row:
            if c:
                cands.setdefault(c[0]["id"], c)
    def excluded(pid, p):
        uid = (p.get("user") or {}).get("id")
        return ((own_user is not None and uid == own_user) or str(pid) == str(own_playlist_id)
                or ("playlist", str(pid)) in rated or ("curator", str(uid)) in rated)
    kept = [(pid, p, mid) for pid, (p, mid) in cands.items() if not excluded(pid, p)]
    scored = 0
    for pid, p, mid in kept[:max_playlists]:  # cap applied after exclusions, round-robin order kept
        creator = p.get("user") or {}
        tracks = []
        for page in range(pages):
            tracks += _safe(dz, f"/playlist/{pid}/tracks", {"index": page * 100, "limit": 100}).get("data", [])
        tracks = [t for t in tracks if (t.get("artist") or {}).get("id")]
        if len(tracks) < MIN_TRACKS:
            continue
        overlap, novelty, s = score_playlist(tracks, lib_tracks, lib_artists, known)
        nb = p.get("nb_tracks") or len(tracks)
        s *= min(1.0, math.sqrt(BIG / nb)) * min(1.0, len({t['artist']['id'] for t in tracks}) / MIN_ARTISTS) * mult.get(mid, 1.0)
        conn.execute("INSERT INTO playlists VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, p.get("title"), p.get("link"), nb, creator.get("id"), creator.get("name"),
                      is_editorial(creator), mid, overlap, novelty, s))
        scored += 1
    conn.execute(
        "INSERT INTO curators SELECT creator_id, creator_name, MAX(editorial), COUNT(*),"
        " (SELECT id FROM playlists q WHERE q.creator_id = p.creator_id ORDER BY score DESC, id LIMIT 1),"
        " MAX(score) * (1 + MIN(0.3, 0.1 * (COUNT(*) - 1))) FROM playlists p"
        " WHERE creator_id IS NOT NULL AND score > 0 GROUP BY creator_id")
    conn.commit()
    return scored


def top_playlists(conn, n=20, users_only=False):
    return conn.execute("SELECT p.*, m.name FROM playlists p LEFT JOIN modes m ON m.mode_id = p.mode_id"
                        f" {'WHERE p.editorial = 0' if users_only else ''} ORDER BY p.score DESC, p.id LIMIT ?", (n,)).fetchall()


def top_curators(conn, n=20, users_only=False):
    return conn.execute("SELECT c.id, c.name, c.editorial, c.n_playlists, c.score, p.title, p.link, p.overlap, p.novelty, m.name"
                        " FROM curators c JOIN playlists p ON p.id = c.best_playlist_id LEFT JOIN modes m ON m.mode_id = p.mode_id"
                        f" {'WHERE c.editorial = 0' if users_only else ''} ORDER BY c.score DESC, c.id LIMIT ?", (n,)).fetchall()
