"""Music Scout CLI entry point."""
import argparse
import sys
from pathlib import Path

import yaml

from src import db, ingest_deezer, ingest_tree


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


def main(argv=None):
    p = argparse.ArgumentParser(prog="music_scout", description="Taste profiler + music/curator recommender.")
    p.add_argument("--config", default="config/config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest", help="ingest liked tracks (idempotent)")
    ing.add_argument("source", nargs="?", default="all", choices=["all", "deezer", "local"])
    sub.add_parser("stats", help="counts per source and era")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    db_path = Path(cfg.get("db_path") or "data/music_scout.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    if args.cmd == "ingest":
        cmd_ingest(conn, cfg, args.source)
    else:
        cmd_stats(conn)


if __name__ == "__main__":
    main()
