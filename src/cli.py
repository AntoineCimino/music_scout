"""Music Scout CLI entry point."""
import argparse
import sys
from pathlib import Path

import yaml

from src import db, enrich, feedback, ingest_deezer, ingest_tree, profile, recommend_artists, recommend_curators


def load_config(path):
    if not Path(path).exists():
        sys.exit(f"Config not found: {path}. Copy config/config.example.yaml to config/config.yaml and fill it in.")
    return yaml.safe_load(Path(path).read_text()) or {}


def cmd_ingest(conn, cfg, which):
    if which in ("all", "deezer"):
        pid = str((cfg.get("deezer") or {}).get("playlist_id") or "")
        if pid:
            print(f"deezer: {ingest_deezer.ingest(conn, pid)} tracks fetched")
        else:
            print("deezer: skipped (no deezer.playlist_id in config)")
    if which in ("all", "local"):
        tree = (cfg.get("local") or {}).get("tree_file")
        if tree and Path(tree).exists():
            print(f"local: {ingest_tree.ingest(conn, tree)} files parsed")
        else:
            print("local: skipped (local.tree_file missing)")


def cmd_stats(conn):
    q = lambda sql: conn.execute(sql).fetchall()
    print(f"tracks: {q('SELECT COUNT(*) FROM tracks')[0][0]}")
    for src, n in q("SELECT source, COUNT(DISTINCT track_id) FROM track_sources GROUP BY source"):
        print(f"  source {src}: {n}")
    both = q("SELECT COUNT(*) FROM (SELECT track_id FROM track_sources GROUP BY track_id HAVING COUNT(DISTINCT source) > 1)")
    print(f"  in several sources: {both[0][0]}")
    for era, n in q("SELECT COALESCE(era, genre_hint, '(none)'), COUNT(*) FROM tracks GROUP BY 1 ORDER BY 1"):
        print(f"  era/genre {era}: {n}")
    total = q("SELECT COUNT(*), COUNT(deezer_id) FROM tracks")[0]
    print(f"resolved on Deezer: {total[1]}/{total[0]} ({100 * total[1] / max(total[0], 1):.1f}%)")
    print(f"artists enriched: {q('SELECT COUNT(*) FROM artists')[0][0]}, related edges: {q('SELECT COUNT(*) FROM artist_related')[0][0]}")
    print("top genres (tracks):")
    for g, n in q("SELECT g.genre, COUNT(DISTINCT t.id) FROM tracks t JOIN album_genres g ON g.album_id = t.deezer_album_id"
                  " GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 15"):
        print(f"  {g}: {n}")
    print("era distribution:")
    for era, n in q("SELECT COALESCE(era, '(unknown)'), COUNT(*) FROM tracks GROUP BY 1 ORDER BY 1"):
        print(f"  {era}: {n}")
    print("feedback:", ", ".join(f"{t} {v}: {n}" for t, v, n in q(
        "SELECT item_type, verdict, COUNT(*) FROM feedback GROUP BY 1, 2 ORDER BY 1, 2")) or "none")
    counts = feedback.mode_counts(conn)
    if counts and conn.execute("SELECT 1 FROM sqlite_master WHERE name='modes'").fetchone():
        print("mode multipliers:")
        for mid, name in q("SELECT mode_id, name FROM modes ORDER BY weight DESC"):
            c = counts.get(mid, {})
            print(f"  [{mid}] {name}: x{feedback.multiplier(c):.3f}  ({c.get('like', 0)} like, "
                  f"{c.get('dislike', 0)} dislike, {c.get('known', 0)} known)")


def cmd_enrich(conn, cfg, limit):
    if (cfg.get("musicbrainz") or {}).get("enabled"):
        print("musicbrainz: deferred, not implemented yet (Deezer genres used)")
    res = enrich.enrich(conn, limit=limit)
    print(", ".join(f"{k}: {v}" for k, v in res.items()))


def cmd_profile(conn, out, force):
    out = Path(out)
    if out.exists() and not force:
        sys.exit(f"{out} exists (may hold your edits). Re-run with --force to overwrite.")
    out.parent.mkdir(parents=True, exist_ok=True)  # fail before touching the DB or calling the LLM
    md, stats, summaries, names = profile.build(conn)
    out.write_text(md)
    print(f"{stats['tracks']} tracks, {stats['artists']} artists -> {len(summaries)} listening modes")
    for mid, s in summaries.items():
        print(f"  {mid}. {names[mid][0]} ({s['weight']:.0%}): {', '.join(s['top_artists'][:4])}")
    print(f"written: {out}")


def cmd_recommend(conn, cfg, args):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mode_artists'").fetchone():
        sys.exit("No listening modes yet: run `profile` first.")
    if args.kind in ("curators", "playlists"):
        return cmd_curators(conn, cfg, args)
    top, per_mode, modes = recommend_artists.recommend(conn, args.top, args.per_mode, fetch=not args.no_lookup)
    fmt = lambda r: (f"{r['name'] or '#' + str(r['id'])}  score={r['score']:.4f}"
                     + (f"  fans={r['nb_fan']}" if r["nb_fan"] is not None else "")
                     + f"  - because you like {', '.join(r['because'])}")
    print(f"Top {len(top)} artists overall:")
    for i, r in enumerate(top, 1):
        print(f"  {i:2}. {fmt(r)}")
    for mid, recs in per_mode.items():
        print(f"\n[{mid}] {modes[mid]}:")
        for r in recs:
            print(f"  - {fmt(r)}")


def cmd_curators(conn, cfg, args):
    own = str((cfg.get("deezer") or {}).get("playlist_id") or "") or None
    n = recommend_curators.run(conn, own, max_playlists=args.max_playlists)
    print(f"{n} playlists scored")
    tag = lambda ed: "editorial" if ed else "user"
    if args.kind == "playlists":
        for i, r in enumerate(recommend_curators.top_playlists(conn, args.top, args.users_only), 1):
            print(f"{i:2}. {r[1]} by {r[5]} ({tag(r[6])})  [{r[11]}]  overlap={r[8]:.0%} novelty={r[9]:.0%}"
                  f" tracks={r[3]} score={r[10]:.3f}  {r[2]}")
    else:
        for i, (cid, name, ed, n_pl, sc, title, link, ov, nov, mode) in enumerate(recommend_curators.top_curators(conn, args.top, args.users_only), 1):
            print(f"{i:2}. {name} ({tag(ed)}, {n_pl} playlists) score={sc:.3f}  [{mode}]  best: {title}"
                  f"  overlap={ov:.0%} novelty={nov:.0%}  {link}")


def cmd_feedback(conn, args):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mode_artists'").fetchone():
        sys.exit("No listening modes yet: run `profile` first.")
    if args.action == "rate":
        if not (args.item_type and args.item_id and args.verdict):
            sys.exit("usage: feedback rate <artist|curator|playlist> <id> <like|dislike|known>")
        try:
            mid = feedback.rate(conn, args.item_type, args.item_id, args.verdict, fetch=not args.no_lookup)
        except ValueError as e:
            sys.exit(str(e))
        print(f"saved {args.item_type} {int(args.item_id)} = {args.verdict} (mode {mid if mid is not None else '?'})")
    else:
        feedback.loop(conn, args.type, args.n, fetch=not args.no_lookup)


def main(argv=None):
    p = argparse.ArgumentParser(prog="music_scout", description="Taste profiler + music/curator recommender.")
    p.add_argument("--config", default="config/config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest", help="ingest liked tracks (idempotent)")
    ing.add_argument("source", nargs="?", default="all", choices=["all", "deezer", "local"])
    en = sub.add_parser("enrich", help="resolve local tracks, related artists, album genres/year (cached)")
    en.add_argument("--limit", type=int, help="max new HTTP fetches this run")
    sub.add_parser("stats", help="counts per source and era")
    pr = sub.add_parser("profile", help="taste stats + listening modes -> data/taste_profile.md")
    pr.add_argument("--out", default="data/taste_profile.md")
    pr.add_argument("--force", action="store_true", help="overwrite an existing profile file")
    rec = sub.add_parser("recommend", help="recommendations from listening modes")
    rec.add_argument("kind", choices=["artists", "curators", "playlists"])
    rec.add_argument("--top", type=int, default=20, help="overall top N")
    rec.add_argument("--per-mode", type=int, default=5, help="top K per mode")
    rec.add_argument("--max-playlists", type=int, default=150, help="curators/playlists: max candidate playlists")
    rec.add_argument("--users-only", action="store_true", help="curators/playlists: exclude Deezer editorial creators")
    rec.add_argument("--no-lookup", action="store_true", help="skip Deezer name/fan lookup")
    fb = sub.add_parser("feedback", help="rate suggestions (interactive) or `feedback rate <type> <id> <verdict>`")
    fb.add_argument("action", nargs="?", default="loop", choices=["loop", "rate"])
    fb.add_argument("item_type", nargs="?", choices=sorted(feedback.TYPES.values()))
    fb.add_argument("item_id", nargs="?")
    fb.add_argument("verdict", nargs="?", choices=sorted(feedback.VERDICTS.values()))
    fb.add_argument("--type", default="mixed", choices=["artists", "curators", "playlists", "mixed"])
    fb.add_argument("-n", type=int, default=10, help="number of suggestions to rate")
    fb.add_argument("--no-lookup", action="store_true", help="skip Deezer lookups (names, seed related artists)")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    db_path = Path(cfg.get("db_path") or "data/music_scout.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    if args.cmd == "ingest":
        cmd_ingest(conn, cfg, args.source)
    elif args.cmd == "enrich":
        cmd_enrich(conn, cfg, args.limit)
    elif args.cmd == "profile":
        cmd_profile(conn, args.out, args.force)
    elif args.cmd == "feedback":
        cmd_feedback(conn, args)
    elif args.cmd == "recommend":
        cmd_recommend(conn, cfg, args)
    else:
        cmd_stats(conn)


if __name__ == "__main__":
    main()
