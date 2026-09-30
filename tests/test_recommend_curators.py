from src import db, enrich, profile, recommend_curators as rc
from tests.test_enrich import FakeSession


def t(tid, aid):
    return {"id": tid, "artist": {"id": aid}}


def lib():
    conn = db.connect(":memory:")
    conn.executescript(profile.MODES_SCHEMA)
    conn.execute("INSERT INTO tracks (key, artist, title, deezer_id, deezer_artist_id) VALUES ('a|x', 'A', 'x', 1, 10)")
    conn.execute("INSERT INTO artists VALUES (10, 'A', 0)")
    conn.execute("INSERT INTO artist_related VALUES (10, 20, 1)")  # 20 = recommended candidate
    conn.execute("INSERT INTO modes VALUES (1, 'Mode', '', 1.0)")
    conn.execute("INSERT INTO mode_artists VALUES (1, 10)")
    return conn


def test_score_formula():
    tracks = [t(1, 10), t(2, 10), t(3, 20), t(4, 99)]    # lib track, lib artist new track, candidate, unknown
    ov, nov, s = rc.score_playlist(tracks, {1}, {10}, {10, 20})
    assert (ov, nov) == (0.5, 0.5) and abs(s - 0.5 * 0.9) < 1e-9


def test_run_skips_own_and_feedback_labels_and_aggregates(monkeypatch):
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    conn = lib()
    conn.execute("INSERT INTO feedback VALUES ('playlist', '503', 'dislike', 0)")
    good = [t(1, 10)] * 4 + [t(2, 10)] * 3 + [t(3, 20)] * 3 + [t(4, 99)] * 2
    mono = [t(1, 10)] * 12
    pl = lambda pid, uid, name, nb=12: {"id": pid, "title": f"P{pid}", "link": "l", "nb_tracks": nb,
                                        "user": {"id": uid, "name": name}}
    sess = FakeSession({
        "/playlist/999": {"id": 999, "creator": {"id": 7}},
        "search/playlist": {"data": [pl(500, 7, "Me"), pl(501, 8, "Bob"), pl(502, 8, "Bob"),
                                     pl(503, 9, "X"), pl(505, 5, "Mono"), pl(504, 6, "Ann - Deezer Jazz Editor", nb=800)]},
        "/playlist/501/": {"data": good},
        "/playlist/502/": {"data": good[:10]},
        "/playlist/504/": {"data": good},
        "/playlist/505/": {"data": mono},
    })
    assert rc.run(conn, "999", session=sess) == 4            # own (500) and rated (503) skipped
    assert not any("/playlist/500/" in u or "/playlist/503/" in u for u in sess.calls)
    rows = {r[0]: r for r in rc.top_playlists(conn)}
    assert rows[504][6] == 1 and rows[501][6] == 0           # editorial label
    assert rows[504][10] < rows[501][10]                     # huge playlist penalized
    assert rows[505][10] < rows[501][10]                     # single-artist playlist penalized
    cur = rc.top_curators(conn)
    assert cur[0][0] == 8 and cur[0][3] == 2
    assert abs(cur[0][4] - max(rows[501][10], rows[502][10]) * 1.1) < 1e-9


def test_failed_call_does_not_abort(monkeypatch):
    import requests
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)

    class Boom(FakeSession):
        def get(self, url, timeout=None):
            if "/playlist/1/" in url:
                raise requests.ConnectionError("down")
            return super().get(url, timeout)
    good = [t(1, 10)] * 6 + [t(3, 20)] * 6
    sess = Boom({"search/playlist": {"data": [{"id": 1, "user": {"id": 5}}, {"id": 2, "user": {"id": 6}}]},
                 "/playlist/2/": {"data": good}})
    assert rc.run(lib(), session=sess) == 1


def test_is_editorial():
    ed = lambda n: rc.is_editorial({"name": n})
    assert ed("Rod - Deezer Rock Editor") and ed("Deezer UK & Ireland Editor") and ed("Alexandre - Pop & Hits Editor")
    assert ed("Laeti - Deezer Editrice Variété Française") and ed("Narjes - Deezer Rap & R&B Éditrice France")
    assert not ed("Deezer fan 92") and not ed("Topsify France") and not ed("editor")


def test_cap_after_exclusions_users_only_and_own_warning(monkeypatch, capsys):
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    conn = lib()
    conn.execute("INSERT INTO feedback VALUES ('curator', '8', 'dislike', 0)")
    good = [t(1, 10)] * 4 + [t(2, 10)] * 3 + [t(3, 20)] * 3 + [t(4, 99)] * 2
    pl = lambda pid, uid, name: {"id": pid, "user": {"id": uid, "name": name}}
    sess = FakeSession({"search/playlist": {"data": [pl(601, 8, "Bob"), pl(602, 3, "Rod - Deezer Rock Editor"),
                                                     pl(603, 4, "Zoe")]},
                        "/playlist/602/": {"data": good}, "/playlist/603/": {"data": good}})
    assert rc.run(conn, "999", max_playlists=2, session=sess) == 2   # rated 601 does not eat the cap
    assert "could not resolve" in capsys.readouterr().err            # /playlist/999 -> 800
    assert [r[0] for r in rc.top_playlists(conn, users_only=True)] == [603]
    assert [r[0] for r in rc.top_curators(conn, users_only=True)] == [4]
    assert len(rc.top_curators(conn)) == 2
