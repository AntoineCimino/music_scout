"""Artist recommendations: related artists absent from the library, scored per listening mode."""
import sys
from collections import defaultdict

import requests

from src import enrich, feedback


def score(conn, exclude_rated=True):
    """Returns {candidate_id: {"total": float, "modes": {mode_id: float}, "from": {lib_id: float}, "from_mode": {mode_id: {lib_id: float}}}}.

    Per mode: sum over library artists a in the mode with edge a->c of 1/(rank+1) * share(a in mode),
    then * effective mode weight (weight x feedback multiplier). Liked/known artists attributed to a
    mode act as extra seeds there with share = seed weight / n mode artists. Library artists and
    (unless exclude_rated=False) any artist with feedback are excluded.
    """
    library = {r[0] for r in conn.execute("SELECT DISTINCT deezer_artist_id FROM tracks WHERE deezer_artist_id IS NOT NULL")}
    # feedback writers store str(int(artist_id)); anything non-numeric is ignored
    rated = {int(r[0]) for r in conn.execute("SELECT item_id FROM feedback WHERE item_type='artist'")
             if str(r[0]).strip().isdigit()} if exclude_rated else set()
    mult = feedback.multipliers(conn)
    weight = {m: w * mult.get(m, 1.0) for m, w in conn.execute("SELECT mode_id, weight FROM modes")}
    seeds = feedback.seeds(conn)
    known = {a for s in seeds.values() for a, w in s.items() if w == 1.0}
    if exclude_rated:  # attribution (exclude_rated=False) must still see known artists as candidates
        library |= known
    counts = dict(conn.execute("SELECT deezer_artist_id, COUNT(*) FROM tracks WHERE deezer_artist_id IS NOT NULL GROUP BY 1"))
    members = defaultdict(list)
    for mid, aid in conn.execute("SELECT mode_id, artist_id FROM mode_artists"):
        members[mid].append(aid)
    related = defaultdict(list)
    for a, c, rank in conn.execute("SELECT artist_id, related_id, rank FROM artist_related"):
        if c not in library and c not in rated:
            related[a].append((c, rank))
    out = defaultdict(lambda: {"total": 0.0, "modes": defaultdict(float), "from": defaultdict(float),
                               "from_mode": defaultdict(lambda: defaultdict(float))})
    for mid, arts in members.items():
        mode_tracks = sum(counts.get(a, 0) for a in arts) or 1
        for a in arts:
            share = counts.get(a, 0) / mode_tracks
            for c, rank in related[a]:
                s = share / (rank + 1) * weight.get(mid, 0.0)
                out[c]["total"] += s
                out[c]["modes"][mid] += s
                out[c]["from"][a] += s
                out[c]["from_mode"][mid][a] += s
        for a, sw in seeds.get(mid, {}).items():
            for c, rank in related[a]:
                if c == a:
                    continue
                s = sw / max(len(arts), 1) / (rank + 1) * weight.get(mid, 0.0)
                out[c]["total"] += s
                out[c]["modes"][mid] += s
                out[c]["from"][a] += s
                out[c]["from_mode"][mid][a] += s
    return out


def lookup(conn, ids, session=requests):
    """Fetch name/nb_fan for candidates not yet known (cached + throttled via enrich.Deezer)."""
    dz = enrich.Deezer(conn, session=session)
    for cid in ids:
        if conn.execute("SELECT 1 FROM candidate_artists WHERE id=?", (cid,)).fetchone():
            continue
        try:
            a = dz.get(f"/artist/{cid}")
        except (requests.RequestException, RuntimeError) as e:  # one bad lookup must not sink the run
            print(f"lookup failed for artist {cid}: {e}", file=sys.stderr)
            continue
        if a:
            conn.execute("INSERT OR REPLACE INTO candidate_artists VALUES (?, ?, ?)", (cid, a.get("name"), a.get("nb_fan")))
            conn.commit()


def recommend(conn, top_n=20, per_mode=5, fetch=True, session=requests):
    """Returns (top_overall, {mode_id: recs}, {mode_id: name}); each rec has id, name, nb_fan, score, because."""
    scores = score(conn)
    modes = dict(conn.execute("SELECT mode_id, name FROM modes ORDER BY weight DESC"))
    rank = lambda key: sorted(scores, key=lambda c: (-key(c), c))
    top = rank(lambda c: scores[c]["total"])[:top_n]
    by_mode = {m: rank(lambda c: scores[c]["modes"].get(m, 0.0))[:per_mode] for m in modes}
    by_mode = {m: [c for c in ids if scores[c]["modes"].get(m)] for m, ids in by_mode.items()}
    wanted = list(dict.fromkeys(top + [c for ids in by_mode.values() for c in ids]))
    if fetch:
        lookup(conn, wanted, session=session)
    names = {i: (n, f) for i, n, f in conn.execute("SELECT id, name, nb_fan FROM candidate_artists")}
    lib_names = {**{i: n for i, n, _ in conn.execute("SELECT * FROM candidate_artists")},
                 **dict(conn.execute("SELECT id, name FROM artists"))}

    def rec(c, s, contrib):
        src = sorted(contrib.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        name, fans = names.get(c, (None, None))
        return {"id": c, "name": name, "nb_fan": fans, "score": s,
                "because": [lib_names.get(a) or f"#{a}" for a, _ in src]}
    return ([rec(c, scores[c]["total"], scores[c]["from"]) for c in top],
            {m: [rec(c, scores[c]["modes"][m], scores[c]["from_mode"][m]) for c in ids] for m, ids in by_mode.items()}, modes)
