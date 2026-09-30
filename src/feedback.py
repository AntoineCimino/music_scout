"""Feedback loop: rate suggestions, persist verdicts, derive per-mode multipliers and seed artists.

Verdicts: like / dislike / known. Each rating is attributed to an anchor library artist (stored in `feedback_anchors`, kept apart so the
`feedback` table schema stays as is); its mode is resolved via `mode_artists` at read time, so ratings
survive `profile --force` renumbering modes.
Mode multiplier = 1 + ALPHA * (likes + KNOWN_W * known - dislikes) / (n + BETA), clamped [LO, HI].
Liked artists become seeds (weight LIKE_SEED), known ones count as library artists (weight 1.0).
"""
import sys
import time

import requests

from src import enrich

ALPHA, BETA, KNOWN_W, LO, HI, LIKE_SEED = 0.5, 3, 0.5, 0.3, 2.0, 0.5
VERDICTS = {"y": "like", "n": "dislike", "k": "known"}
TYPES = {"artists": "artist", "curators": "curator", "playlists": "playlist"}
SCHEMA = "CREATE TABLE IF NOT EXISTS feedback_anchors (item_type TEXT NOT NULL, item_id TEXT NOT NULL, anchor_id INTEGER, PRIMARY KEY (item_type, item_id));"
# rating -> mode via its anchor library artist's current mode
RATED_MODES = ("SELECT f.item_type, f.item_id, f.verdict, ma.mode_id FROM feedback f"
               " JOIN feedback_anchors a USING (item_type, item_id) JOIN mode_artists ma ON ma.artist_id = a.anchor_id")


def _table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def anchor_of(conn, item_type, item_id):
    """Library artist that best explains the suggestion (its mode = the item's mode), or None."""
    members = dict(conn.execute("SELECT artist_id, mode_id FROM mode_artists"))
    if item_type == "artist":
        from src import recommend_artists
        contrib = recommend_artists.score(conn, exclude_rated=False).get(item_id, {}).get("from") or {}
        lib = {a: v for a, v in contrib.items() if a in members}  # seeds are not in mode_artists
        return min(lib, key=lambda a: (-lib[a], a)) if lib else None
    if not _table(conn, "playlists"):
        return None
    sql = ("SELECT mode_id FROM playlists WHERE id=?" if item_type == "playlist" else
           "SELECT p.mode_id FROM curators c JOIN playlists p ON p.id = c.best_playlist_id WHERE c.id=?")
    row = conn.execute(sql, (item_id,)).fetchone()
    if not row:
        return None
    # ponytail: playlist anchor = its mode's biggest artist (by tracks); store playlist artists if too coarse
    top = conn.execute("SELECT m.artist_id FROM mode_artists m JOIN tracks t ON t.deezer_artist_id = m.artist_id"
                       " WHERE m.mode_id=? GROUP BY 1 ORDER BY COUNT(*) DESC, 1 LIMIT 1", (row[0],)).fetchone()
    return top[0] if top else None


def mode_of(conn, anchor):
    row = conn.execute("SELECT mode_id FROM mode_artists WHERE artist_id=?", (anchor,)).fetchone() if anchor else None
    return row[0] if row else None


def rate(conn, item_type, item_id, verdict, fetch=True, session=requests):
    """Persist one verdict (item_id stored as str(int(id))). Liked/known artists get their related edges fetched."""
    if verdict not in VERDICTS.values():
        raise ValueError(f"verdict must be one of {sorted(VERDICTS.values())}")
    if item_type not in TYPES.values():
        raise ValueError(f"item_type must be one of {sorted(TYPES.values())}")
    item_id = int(item_id)
    conn.executescript(SCHEMA)
    anchor = anchor_of(conn, item_type, item_id)
    conn.execute("INSERT OR REPLACE INTO feedback VALUES (?, ?, ?, ?)", (item_type, str(item_id), verdict, int(time.time())))
    # re-rating may fail to resolve (e.g. playlists table rebuilt): never erase a known anchor
    conn.execute("INSERT INTO feedback_anchors VALUES (?, ?, ?) ON CONFLICT (item_type, item_id)"
                 " DO UPDATE SET anchor_id = COALESCE(excluded.anchor_id, anchor_id)", (item_type, str(item_id), anchor))
    mid = mode_of(conn, conn.execute("SELECT anchor_id FROM feedback_anchors WHERE item_type=? AND item_id=?",
                                     (item_type, str(item_id))).fetchone()[0])
    conn.commit()
    if fetch and item_type == "artist" and verdict in ("like", "known"):
        try:
            rel = enrich.Deezer(conn, session=session).get(f"/artist/{item_id}/related")
        except (requests.RequestException, RuntimeError) as e:  # rating is saved; seed edges retried on next rating
            print(f"related lookup failed for artist {item_id}: {e}", file=sys.stderr)
            rel = None
        for rank, r in enumerate((rel or {}).get("data", []), 1):
            conn.execute("INSERT OR IGNORE INTO artist_related VALUES (?, ?, ?)", (item_id, r["id"], rank))
        conn.commit()
    return mid


def mode_counts(conn):
    """{mode_id: {"like": n, "dislike": n, "known": n}} over attributed ratings."""
    out = {}
    if not (_table(conn, "feedback_anchors") and _table(conn, "mode_artists")):
        return out
    for mid, verdict, n in conn.execute(f"SELECT mode_id, verdict, COUNT(*) FROM ({RATED_MODES}) GROUP BY 1, 2"):
        out.setdefault(mid, {"like": 0, "dislike": 0, "known": 0})[verdict] = n
    return out


def multiplier(c):
    n = sum(c.values())
    m = 1 + ALPHA * (c.get("like", 0) + KNOWN_W * c.get("known", 0) - c.get("dislike", 0)) / (n + BETA)
    return min(HI, max(LO, m))


def multipliers(conn):
    return {mid: multiplier(c) for mid, c in mode_counts(conn).items()}


def seeds(conn):
    """{mode_id: {artist_id: seed weight}} from liked (LIKE_SEED) and known (1.0) artists."""
    out = {}
    if not (_table(conn, "feedback_anchors") and _table(conn, "mode_artists")):
        return out
    for aid, verdict, mid in conn.execute(
            f"SELECT item_id, verdict, mode_id FROM ({RATED_MODES}) WHERE item_type='artist' AND verdict IN ('like', 'known')"):
        if str(aid).isdigit():
            out.setdefault(mid, {})[int(aid)] = 1.0 if verdict == "known" else LIKE_SEED
    return out


def suggestions(conn, kind, n=10, fetch=True):
    """Current top unrated suggestions as dicts: type, id, name, mode, reason, link."""
    from src import recommend_artists, recommend_curators
    items = []
    if kind in ("artists", "mixed"):
        modes = dict(conn.execute("SELECT mode_id, name FROM modes"))
        scores = recommend_artists.score(conn)
        top, _, _ = recommend_artists.recommend(conn, n, 0, fetch=fetch)
        for r in top:
            m = scores[r["id"]]["modes"]
            items.append({"type": "artist", "id": r["id"], "name": r["name"] or f"#{r['id']}",
                          "mode": modes.get(max(sorted(m), key=m.get)) if m else None,
                          "reason": "because you like " + ", ".join(r["because"]),
                          "link": f"https://www.deezer.com/artist/{r['id']}"})
    if kind in ("playlists", "mixed") and _table(conn, "playlists"):
        for r in recommend_curators.top_playlists(conn, n * 3):
            items.append({"type": "playlist", "id": r[0], "name": f"{r[1]} by {r[5]}", "mode": r[11],
                          "reason": f"overlap={r[8]:.0%} novelty={r[9]:.0%}", "link": r[2]})
    if kind in ("curators", "mixed") and _table(conn, "curators"):
        for r in recommend_curators.top_curators(conn, n * 3):
            items.append({"type": "curator", "id": r[0], "name": r[1], "mode": r[9],
                          "reason": f"best: {r[5]} overlap={r[7]:.0%} novelty={r[8]:.0%}",
                          "link": f"https://www.deezer.com/profile/{r[0]}"})
    rated = {(t, str(i)) for t, i in conn.execute("SELECT item_type, item_id FROM feedback")}
    items = [i for i in items if (i["type"], str(i["id"])) not in rated]
    if kind == "mixed":  # interleave types so artists do not crowd out curators/playlists
        by = [[i for i in items if i["type"] == t] for t in ("artist", "playlist", "curator")]
        items = [i for row in zip(*[b + [None] * (n - len(b)) for b in by]) for i in row if i]
    return items[:n]


def loop(conn, kind="mixed", n=10, fetch=True, ask=input, out=print):
    """Interactive rating loop; every verdict is saved immediately. Returns {verdict: count}."""
    items = suggestions(conn, kind, n, fetch)
    if not items:
        out("Nothing to rate: run `recommend artists` / `recommend curators` first, or everything is rated.")
    done = {}
    for i, it in enumerate(items, 1):
        out(f"\n[{i}/{len(items)}] {it['type']}: {it['name']}  (mode: {it['mode'] or '?'})\n  {it['reason']}\n  {it['link']}")
        while True:
            try:
                key = ask("  y=like n=dislike k=already know s=skip q=quit > ").strip().lower()[:1]
            except EOFError:
                key = "q"
            if key in (*VERDICTS, "s", "q"):
                break
        if key == "q":
            break
        if key == "s":
            continue
        rate(conn, it["type"], it["id"], VERDICTS[key], fetch=fetch)
        done[VERDICTS[key]] = done.get(VERDICTS[key], 0) + 1
    out(f"saved: {done or 'nothing'}")
    return done
