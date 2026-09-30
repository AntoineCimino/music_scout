from src import cli, feedback as fb, recommend_artists as ra
from tests.test_recommend_artists import graph


def test_multiplier_formula_and_clamp():
    assert fb.multiplier({}) == 1.0
    assert abs(fb.multiplier({"like": 2, "dislike": 0, "known": 1}) - (1 + 0.5 * 2.5 / 6)) < 1e-9
    assert fb.multiplier({"dislike": 1000}) > 0.3 - 1e-9
    fb.ALPHA, old = 50, fb.ALPHA
    try:
        assert fb.multiplier({"like": 5}) == 2.0 and fb.multiplier({"dislike": 5}) == 0.3
    finally:
        fb.ALPHA = old


def test_loop_persists_and_excludes(monkeypatch):
    conn = graph()
    keys = iter(["x", "y", "s", "n"])   # invalid key re-asked; 101 like, 100 skipped, 102 dislike
    out = []
    done = fb.loop(conn, "artists", 3, fetch=False, ask=lambda _: next(keys), out=out.append)
    assert done == {"like": 1, "dislike": 1}
    rows = dict(conn.execute("SELECT item_id, verdict FROM feedback WHERE item_type='artist'"))
    assert rows == {"101": "like", "102": "dislike"}
    assert "https://www.deezer.com/artist/101" in out[0]
    assert [i["id"] for i in fb.suggestions(conn, "artists", 10, fetch=False)] == [100]  # rated never re-shown


def test_quit_and_eof_stop_loop():
    conn = graph()
    assert fb.loop(conn, "artists", 3, fetch=False, ask=lambda _: "q", out=lambda s: None) == {}

    def eof(_):
        raise EOFError
    assert fb.loop(conn, "artists", 3, fetch=False, ask=eof, out=lambda s: None) == {}
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


def test_dislikes_in_a_mode_shift_ranking():
    conn = graph()
    conn.execute("INSERT INTO artist_related VALUES (3, 103, 3)")  # extra mode-2 candidate to dislike
    before = ra.score(conn)
    assert before[101]["modes"][2] > 0 and before[100]["total"] > before[102]["total"]
    fb.rate(conn, "artist", 102, "dislike", fetch=False)   # attributed to mode 2
    fb.rate(conn, "artist", "103", "dislike", fetch=False)
    assert fb.multipliers(conn)[2] < 1.0
    after = ra.score(conn)
    assert 102 not in after and 103 not in after
    assert abs(after[101]["modes"][2] / before[101]["modes"][2] - fb.multipliers(conn)[2]) < 1e-9
    assert after[101]["modes"][1] == before[101]["modes"][1]   # other mode untouched


def test_liked_artist_becomes_seed(monkeypatch):
    conn = graph()
    fb.rate(conn, "artist", 102, "like", fetch=False)           # mode 2
    conn.execute("INSERT INTO artist_related VALUES (102, 200, 1)")
    s = ra.score(conn)
    mult = fb.multipliers(conn)[2]
    assert abs(s[200]["modes"][2] - 0.5 / 1 / 2 * 0.4 * mult) < 1e-9  # seed 0.5 / 1 mode artist / (rank+1)
    assert 102 not in s


def test_known_artist_counts_as_library_via_cli(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "t.db"
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"db_path: {db_path}\n")
    src = graph()
    src.commit()
    src.backup(__import__("sqlite3").connect(db_path))
    cli.main(["--config", str(cfg), "feedback", "rate", "artist", "101", "known", "--no-lookup"])
    cli.main(["--config", str(cfg), "stats"])
    out = capsys.readouterr().out
    assert "artist known: 1" in out and "mode multipliers" in out
    conn = __import__("sqlite3").connect(db_path)
    assert fb.seeds(conn) == {2: {101: 1.0}}


def test_attribution_survives_mode_renumbering():
    conn = graph()
    fb.rate(conn, "artist", 102, "dislike", fetch=False)       # anchor = C (mode 2)
    conn.executescript("UPDATE modes SET mode_id = mode_id + 10; UPDATE mode_artists SET mode_id = mode_id + 10;")
    assert set(fb.multipliers(conn)) == {12}


def test_rerating_known_artist_keeps_mode():
    conn = graph()
    assert fb.rate(conn, "artist", 101, "known", fetch=False) == 2
    assert fb.rate(conn, "artist", 101, "like", fetch=False) == 2   # still resolvable, never nulled
    conn.execute("DELETE FROM artist_related WHERE related_id = 101")  # now unresolvable
    assert fb.rate(conn, "artist", 101, "dislike", fetch=False) == 2


def test_curator_novelty_counts_feedback_artists(monkeypatch):
    from src import enrich, recommend_curators as rc
    from tests.test_enrich import FakeSession
    from tests.test_recommend_curators import lib, t
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    conn = lib()
    fb.rate(conn, "artist", 20, "like", fetch=False)     # candidate 20 is now rated -> out of score()
    tracks = [t(1, 10)] * 6 + [t(3, 20)] * 6
    sess = FakeSession({"search/playlist": {"data": [{"id": 501, "title": "P", "link": "l", "nb_tracks": 12,
                                                      "user": {"id": 8, "name": "Bob"}}]},
                        "/playlist/501/": {"data": tracks}})
    rc.run(conn, session=sess)
    assert rc.top_playlists(conn)[0][9] == 0.5            # liked artist's new tracks still count as novelty
