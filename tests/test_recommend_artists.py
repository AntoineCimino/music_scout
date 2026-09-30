from src import db, enrich, profile, recommend_artists as ra
from tests.test_enrich import FakeSession


def graph():
    """Mode 1 (w=0.6): A (3 tracks), B (1). Mode 2 (w=0.4): C (2). Candidates X, Y, Z; B is also related (library)."""
    conn = db.connect(":memory:")
    conn.executescript(profile.MODES_SCHEMA)
    n = 0
    for aid, k in ((1, 3), (2, 1), (3, 2)):
        for _ in range(k):
            n += 1
            conn.execute("INSERT INTO tracks (key, artist, title, deezer_artist_id) VALUES (?, 'x', 't', ?)", (f"k{n}", aid))
    conn.executemany("INSERT INTO artists VALUES (?, ?, 0)", [(1, "A"), (2, "B"), (3, "C")])
    conn.executemany("INSERT INTO modes VALUES (?, ?, '', ?)", [(1, "M1", 0.6), (2, "M2", 0.4)])
    conn.executemany("INSERT INTO mode_artists VALUES (?, ?)", [(1, 1), (1, 2), (2, 3)])
    conn.executemany("INSERT INTO artist_related VALUES (?, ?, ?)",
                     [(1, 100, 1), (1, 2, 2), (1, 101, 3), (2, 100, 1), (3, 101, 1), (3, 102, 2)])
    return conn


def test_scores_exclude_library_and_follow_formula():
    s = ra.score(graph())
    assert set(s) == {100, 101, 102}                      # library artist 2 never recommended
    assert abs(s[100]["total"] - 0.6 * (0.75 / 2 + 0.25 / 2)) < 1e-9
    assert abs(s[101]["modes"][2] - 0.4 * (1.0 / 2)) < 1e-9
    assert abs(s[101]["modes"][1] - 0.6 * (0.75 / 4)) < 1e-9
    assert abs(s[102]["total"] - 0.4 / 3) < 1e-9


def test_feedback_excludes_and_reasons_and_lookup(monkeypatch):
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    conn = graph()
    conn.execute("INSERT INTO feedback VALUES ('artist', '102', 'dislike', 0)")
    sess = FakeSession({"/artist/100": {"id": 100, "name": "Xnew", "nb_fan": 5}})
    top, per_mode, modes = ra.recommend(conn, top_n=5, per_mode=1, session=sess)
    assert [r["id"] for r in top] == [101, 100]
    assert top[1]["name"] == "Xnew" and top[1]["because"] == ["A", "B"]
    assert top[0]["because"] == ["C", "A"] and top[0]["name"] is None   # 800 -> no name
    assert [r["id"] for r in per_mode[1]] == [100] and [r["id"] for r in per_mode[2]] == [101]
    assert per_mode[2][0]["because"] == ["C"]                          # per-mode reasons only
    assert conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 3  # library table untouched
    n = len(sess.calls)
    ra.recommend(conn, session=sess)
    assert len(sess.calls) == n                                        # cached / already known


def test_lookup_error_falls_back_and_keeps_earlier_lookups(monkeypatch):
    import requests
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    conn = graph()

    class Boom(FakeSession):
        def get(self, url, timeout=None):
            if "/artist/101" in url:
                raise requests.ConnectionError("down")
            return super().get(url, timeout)
    sess = Boom({"/artist/100": {"id": 100, "name": "Xnew", "nb_fan": 5}})
    top, _, _ = ra.recommend(conn, top_n=3, per_mode=0, session=sess)
    assert {r["id"]: r["name"] for r in top} == {101: None, 100: "Xnew", 102: None}
    conn.rollback()                                                    # earlier insert already committed
    assert conn.execute("SELECT name FROM candidate_artists WHERE id=100").fetchone() == ("Xnew",)


def test_non_numeric_feedback_ids_ignored():
    conn = graph()
    conn.executemany("INSERT INTO feedback VALUES ('artist', ?, 'dislike', 0)", [("abc",), ("101",)])
    assert set(ra.score(conn)) == {100, 102}
