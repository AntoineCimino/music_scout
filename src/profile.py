"""Taste profile: local stats + listening modes (clusters over the related-artist graph)."""
import json
import statistics
from collections import Counter, defaultdict

from src import llm

MODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS modes (mode_id INTEGER PRIMARY KEY, name TEXT, description TEXT, weight REAL);
CREATE TABLE IF NOT EXISTS mode_artists (mode_id INTEGER NOT NULL, artist_id INTEGER NOT NULL PRIMARY KEY);
"""


def load(conn):
    """Per-artist facts from the library: name, track count, genre/era counters, nb_fan."""
    arts = {}
    rows = conn.execute(
        "SELECT t.id, t.deezer_artist_id, COALESCE(a.name, t.artist), a.nb_fan, t.rank,"
        " COALESCE(t.era, CASE WHEN al.year THEN (al.year / 10 * 10) || 's' END), g.genre,"
        " (SELECT COUNT(*) FROM album_genres WHERE album_id = t.deezer_album_id)"
        " FROM tracks t LEFT JOIN artists a ON a.id = t.deezer_artist_id"
        " LEFT JOIN albums al ON al.id = t.deezer_album_id"
        " LEFT JOIN album_genres g ON g.album_id = t.deezer_album_id"
        " WHERE t.deezer_artist_id IS NOT NULL").fetchall()
    seen = set()
    for tid, aid, name, fans, rank, era, genre, n_genres in rows:
        a = arts.setdefault(aid, {"name": name, "fans": fans, "tracks": 0, "genres": Counter(),
                                  "eras": Counter(), "ranks": []})
        if genre:
            a["genres"][genre] += 1 / n_genres  # a track spreads one unit over its album's genres
        if tid in seen:  # one row per album genre; count track-level facts once
            continue
        seen.add(tid)
        a["tracks"] += 1
        if era:
            a["eras"][era] += 1
        if rank:
            a["ranks"].append(rank)
    related = defaultdict(set)
    for aid, rid in conn.execute("SELECT artist_id, related_id FROM artist_related"):
        related[aid].add(rid)
    return arts, related


def top_genre(counter):
    return min(counter, key=lambda g: (-counter[g], g)) if counter else None


def build_graph(artist_ids, related, jaccard_min=0.15):
    """Weighted undirected graph over library artists.

    Direct related link (either direction) = 1.0; shared related neighbours = Jaccard (weak edge,
    kept only above `jaccard_min`).
    """
    ids = sorted(artist_ids)
    lib = set(ids)
    g = defaultdict(dict)
    for a in ids:
        for b in related.get(a, set()) & lib:
            if a != b:
                g[a][b] = g[b][a] = 1.0
    # ponytail: O(n^2) pairwise Jaccard, fine for a few thousand artists; index by neighbour if it grows.
    nb = {a: related.get(a, set()) for a in ids}
    for i, a in enumerate(ids):
        if not nb[a]:
            continue
        for b in ids[i + 1:]:
            if not nb[b]:
                continue
            j = len(nb[a] & nb[b]) / len(nb[a] | nb[b])
            if j >= jaccard_min:
                w = g[a].get(b, 0) + j
                g[a][b] = g[b][a] = w
    return g


def label_propagation(graph, nodes, genre_of, max_iter=50):
    """Deterministic weighted label propagation; ties broken by genre match, then smallest label."""
    labels = {n: n for n in nodes}
    for _ in range(max_iter):
        changed = False
        for n in sorted(nodes):
            if not graph.get(n):
                continue
            score = defaultdict(float)
            for m, w in graph[n].items():
                score[labels[m]] += w
            best = max(score.values())
            cands = [l for l, s in score.items() if s >= best - 1e-9]
            new = min(cands, key=lambda l: (genre_of.get(l) != genre_of.get(n), l))
            if new != labels[n]:
                labels[n], changed = new, True
        if not changed:
            break
    return labels


def cluster(arts, related, min_share=0.03, jaccard_min=0.15):
    """Return {mode_index: [artist_ids]}, sorted by track share desc.

    Clusters under `min_share` of tracks are dissolved: each artist joins the big mode where its
    dominant genre weighs most (fallback: the biggest mode).
    """
    genre_of = {a: top_genre(v["genres"]) for a, v in arts.items()}
    labels = label_propagation(build_graph(arts, related, jaccard_min), list(arts), genre_of)
    groups = defaultdict(list)
    for a, l in labels.items():
        groups[l].append(a)
    total = sum(v["tracks"] for v in arts.values()) or 1
    share = lambda members: sum(arts[a]["tracks"] for a in members) / total
    big = [m for m in groups.values() if share(m) >= min_share]
    if not big:  # degenerate library: everything is tiny, keep the largest group
        big = [max(groups.values(), key=share)]
    small = [a for m in groups.values() if not any(m is b for b in big) for a in m]
    genre_w = [Counter() for _ in big]
    for i, m in enumerate(big):
        for a in m:
            if genre_of[a]:
                genre_w[i][genre_of[a]] += arts[a]["tracks"]
    for a in small:
        g = genre_of[a]
        i = max(range(len(big)), key=lambda k: (genre_w[k][g] / (sum(genre_w[k].values()) or 1) if g else 0,
                                                share(big[k])))
        big[i].append(a)
    big.sort(key=lambda m: (-share(m), min(m)))
    return {i + 1: sorted(m) for i, m in enumerate(big)}


def summarize(arts, members, total_tracks):
    tracks = sum(arts[a]["tracks"] for a in members)
    genres, eras = Counter(), Counter()
    for a in members:
        genres.update(arts[a]["genres"])
        eras.update(arts[a]["eras"])
    top = sorted(members, key=lambda a: (-arts[a]["tracks"], arts[a]["name"] or ""))
    return {
        "weight": round(tracks / (total_tracks or 1), 3), "tracks": tracks, "n_artists": len(members),
        "top_artists": [arts[a]["name"] for a in top[:8]],
        "genres": [g for g, _ in genres.most_common(4)], "eras": [e for e, _ in eras.most_common(3)],
    }


def fallback_name(s):
    parts = [s["genres"][0] if s["genres"] else "Mixed", s["eras"][0] if s["eras"] else ""]
    return " ".join(p for p in parts if p)


def name_modes(summaries):
    """One `claude -p` call for all modes; fallback = top genre + era. Returns {mode_id: (name, desc)}."""
    payload = {mid: {k: s[k] for k in ("top_artists", "genres", "eras", "weight")} for mid, s in summaries.items()}
    prompt = (
        "Here are listening modes (clusters) from one person's music library. For each mode id, give a short "
        "evocative name (2-5 words) and a 1-2 sentence description of the mood/use. Modes are distinct; do not "
        'blend them. Answer JSON only: {"<id>": {"name": "...", "description": "..."}}.\n\n'
        + json.dumps(payload, ensure_ascii=False))
    res = llm.ask_json(prompt) if summaries else None
    out = {}
    for mid, s in summaries.items():
        r = res.get(str(mid)) if isinstance(res, dict) else None
        if isinstance(r, dict) and r.get("name"):
            out[mid] = (str(r["name"]).strip(), str(r.get("description") or "").strip())
        else:
            out[mid] = (fallback_name(s), f"Top artists: {', '.join(s['top_artists'][:4])}.")
    return out


def global_stats(arts):
    total = sum(v["tracks"] for v in arts.values())
    genres, eras = Counter(), Counter()
    for v in arts.values():
        genres.update(v["genres"])
        eras.update(v["eras"])
    ranks = [r for v in arts.values() for r in v["ranks"]]
    fans = [v["fans"] for v in arts.values() if v["fans"] is not None]
    pct = lambda xs: ({"p10": xs[len(xs) // 10], "median": statistics.median(xs), "p90": xs[len(xs) * 9 // 10]}
                      if xs else None)
    gt, et = sum(genres.values()) or 1, sum(eras.values()) or 1
    return {
        "tracks": total, "artists": len(arts),
        "genres": [(g, round(n / gt, 3)) for g, n in genres.most_common(12)],
        "eras": sorted((e, round(n / et, 3)) for e, n in eras.items()),
        "top_artists": [(v["name"], v["tracks"]) for v in sorted(arts.values(), key=lambda v: (-v["tracks"], v["name"] or ""))[:15]],
        "track_rank": pct(sorted(ranks)), "artist_fans": pct(sorted(fans)),
    }


def save_modes(conn, summaries, names, modes):
    conn.executescript(MODES_SCHEMA + "DELETE FROM modes; DELETE FROM mode_artists;")
    for mid, s in summaries.items():
        conn.execute("INSERT INTO modes VALUES (?, ?, ?, ?)", (mid, *names[mid], s["weight"]))
        conn.executemany("INSERT INTO mode_artists VALUES (?, ?)", [(mid, a) for a in modes[mid]])
    conn.commit()


def render(stats, summaries, names):
    L = ["# Taste profile", "", "_Generated by `music_scout profile`. Edit freely; regenerate with `--force`._", "",
         "## Overview", f"- Tracks (resolved on Deezer): {stats['tracks']}, artists: {stats['artists']}"]
    if stats["track_rank"]:
        L.append(f"- Track popularity (Deezer rank) p10/median/p90: {stats['track_rank']['p10']} / "
                 f"{stats['track_rank']['median']:.0f} / {stats['track_rank']['p90']}")
    if stats["artist_fans"]:
        L.append(f"- Artist fans p10/median/p90: {stats['artist_fans']['p10']} / "
                 f"{stats['artist_fans']['median']:.0f} / {stats['artist_fans']['p90']}")
    L += ["", "### Genres", *[f"- {g}: {w:.0%}" for g, w in stats["genres"]],
          "", "### Eras", *[f"- {e}: {w:.0%}" for e, w in stats["eras"]],
          "", "### Top artists (tracks)", *[f"- {n} ({c})" for n, c in stats["top_artists"]],
          "", "## Listening modes", "_Separate modes; never averaged together._"]
    for mid, s in summaries.items():
        name, desc = names[mid]
        L += ["", f"### {mid}. {name} — {s['weight']:.0%} ({s['tracks']} tracks, {s['n_artists']} artists)", desc,
              f"- Artists: {', '.join(s['top_artists'])}", f"- Genres: {', '.join(s['genres'])}",
              f"- Eras: {', '.join(s['eras'])}"]
    return "\n".join(L) + "\n"


def build(conn):
    """Compute stats + modes, name them, persist modes. Returns (markdown, stats, summaries, names)."""
    arts, related = load(conn)
    stats = global_stats(arts)
    modes = cluster(arts, related) if arts else {}
    summaries = {m: summarize(arts, members, stats["tracks"]) for m, members in modes.items()}
    names = name_modes(summaries)
    save_modes(conn, summaries, names, modes)
    return render(stats, summaries, names), stats, summaries, names
