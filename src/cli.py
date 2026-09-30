"""Music Scout CLI entry point."""
import argparse
import sys
from pathlib import Path

import yaml

from src import db, enrich, ingest_deezer, ingest_tree, profile


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
    else:
        cmd_stats(conn)


if __name__ == "__main__":
    main()
