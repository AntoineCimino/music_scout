import json
import subprocess
from collections import Counter

from src import cli, db, llm, profile


def art(name, genre, tracks=1, era="2000s"):
    return {"name": name, "fans": 100, "tracks": tracks, "genres": Counter({genre: tracks}),
            "eras": Counter({era: tracks}), "ranks": [1000]}


def two_cliques():
    """Two 4-artist cliques (rock 1-4, electro 11-14) + an isolated singleton 99 tagged Electro."""
    arts = {i: art(f"R{i}", "Rock", 3) for i in (1, 2, 3, 4)}
    arts.update({i: art(f"E{i}", "Electro", 3) for i in (11, 12, 13, 14)})
    arts[99] = art("Lone", "Electro", 1)
    related = {a: {b for b in (1, 2, 3, 4) if b != a} for a in (1, 2, 3, 4)}
    related.update({a: {b for b in (11, 12, 13, 14) if b != a} for a in (11, 12, 13, 14)})
    related[99] = {500}  # only an outside neighbour
    return arts, related


def test_cluster_separates_cliques_and_attaches_singleton_by_genre():
    arts, related = two_cliques()
    modes = profile.cluster(arts, related, min_share=0.1)
    groups = sorted(modes.values())
    assert groups == [[1, 2, 3, 4], [11, 12, 13, 14, 99]]


def test_shared_neighbours_make_weak_edge():
    g = profile.build_graph([1, 2, 3], {1: {7, 8}, 2: {7, 8}, 3: {9}}, jaccard_min=0.5)
    assert g[1][2] == 1.0 and 3 not in g[1]


def fake_run(payload):
    def run(cmd, **kw):
        assert cmd[:2] == ["claude", "-p"] and "--bare" not in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"result": "```json\n" + json.dumps(payload) + "\n```"}))
    return run


SUMS = {1: {"weight": 0.6, "top_artists": ["R1"], "genres": ["Rock"], "eras": ["1970s"]},
        2: {"weight": 0.4, "top_artists": ["E1"], "genres": ["Electro"], "eras": []}}


def test_name_modes_uses_llm(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/bin/claude")
    monkeypatch.setattr(llm.subprocess, "run", fake_run({"1": {"name": "Road trip", "description": "Guitars."}}))
    names = profile.name_modes(SUMS)
    assert names[1] == ("Road trip", "Guitars.")
    assert names[2][0] == "Electro"  # missing from LLM reply -> fallback


def test_name_modes_fallback_without_cli(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: None)
    assert profile.name_modes(SUMS)[1][0] == "Rock 1970s"


def test_llm_error_returns_none(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/bin/claude")
    def boom(cmd, **kw): raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(llm.subprocess, "run", boom)
    assert llm.ask_json("x") is None


def test_cli_profile_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: None)
    conn = db.connect(tmp_path / "t.db")
    for i, (aid, genre) in enumerate([(1, "Rock"), (1, "Rock"), (2, "Rock"), (3, "Dance")]):
        conn.execute("INSERT INTO tracks (key, artist, title, deezer_artist_id, deezer_album_id, rank, era)"
                     " VALUES (?, ?, ?, ?, ?, 500, '1990s')", (f"k{i}", f"A{aid}", f"t{i}", aid, 10 + aid))
        conn.execute("INSERT OR IGNORE INTO album_genres VALUES (?, ?)", (10 + aid, genre))
    conn.execute("INSERT INTO artist_related VALUES (1, 2, 1)")
    conn.commit()
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"db_path: {tmp_path / 't.db'}\n")
    out = tmp_path / "p.md"
    cli.main(["--config", str(cfg), "profile", "--out", str(out)])
    text = out.read_text()
    assert "Listening modes" in text and "Rock 1990s" in text
    assert conn.execute("SELECT COUNT(*) FROM mode_artists").fetchone()[0] == 3
    try:
        cli.main(["--config", str(cfg), "profile", "--out", str(out)])
        assert False, "should refuse without --force"
    except SystemExit:
        pass


def test_multi_genre_album_counts_one_track(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO tracks (key, artist, title, deezer_artist_id, deezer_album_id) VALUES ('k', 'A', 't', 1, 10)")
    conn.execute("INSERT INTO tracks (key, artist, title, deezer_artist_id, deezer_album_id) VALUES ('k2', 'A', 'u', 1, 11)")
    conn.executemany("INSERT INTO album_genres VALUES (?, ?)", [(10, "Pop"), (10, "Rock"), (11, "Rock")])
    arts, _ = profile.load(conn)
    assert arts[1]["genres"] == {"Pop": 0.5, "Rock": 1.5}
    assert profile.global_stats(arts)["genres"] == [("Rock", 0.75), ("Pop", 0.25)]


def test_cli_profile_creates_out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: None)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"db_path: {tmp_path / 't.db'}\n")
    out = tmp_path / "new" / "dir" / "p.md"
    cli.main(["--config", str(cfg), "profile", "--out", str(out)])
    assert out.exists()
