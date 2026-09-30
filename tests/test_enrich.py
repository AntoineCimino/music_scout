from src import db, enrich


class FakeResp:
    def __init__(self, body): self.body = body
    def raise_for_status(self): pass
    def json(self): return self.body


class FakeSession:
    """Routes by URL substring; a list value is consumed one response per call."""
    def __init__(self, routes): self.routes, self.calls = routes, []
    def get(self, url, timeout=None):
        self.calls.append(url)
        for frag, body in self.routes.items():
            if frag in url:
                return FakeResp(body.pop(0) if isinstance(body, list) else body)
        return FakeResp({"error": {"code": 800, "message": "no data"}})


def hit(tid, artist, title, aid, alid):
    return {"id": tid, "title": title, "rank": 5, "duration": 180,
            "artist": {"id": aid, "name": artist}, "album": {"id": alid, "title": "Some Album"}}


ROUTES = {
    "search?q=Artist+A+Song": [{"error": {"code": 4, "message": "quota"}},
                                         {"data": [hit(9, "Other", "Song One", 7, 70), hit(1, "Artist A", "Song One (Live)", 11, 101)]}],
    "search?q=Nobody": {"data": []},
    "/artist/11/related": {"data": [{"id": 21}, {"id": 22}]},
    "/artist/11": {"id": 11, "name": "Artist A", "nb_fan": 1234},
    "/album/101": {"id": 101, "title": "Some Album", "release_date": "1987-05-01",
                   "genres": {"data": [{"name": "Pop"}, {"name": "Rock"}]}},
}


def test_enrich_resolves_relates_and_caches(monkeypatch):
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    conn = db.connect(":memory:")
    db.upsert(conn, "local", "a.mp3", {"artist": "Artist A", "title": "Song One"})
    db.upsert(conn, "local", "b.mp3", {"artist": "Nobody", "title": "Lost Song"})
    s = FakeSession({k: (list(v) if isinstance(v, list) else v) for k, v in ROUTES.items()})

    res = enrich.enrich(conn, session=s, throttle=0)
    assert res["resolved"] == 1 and res["unresolved"] == 1
    assert conn.execute("SELECT deezer_id, deezer_artist_id, era FROM tracks WHERE artist='Artist A'").fetchone() == (1, 11, "1980s")
    assert conn.execute("SELECT related_id, rank FROM artist_related ORDER BY rank").fetchall() == [(21, 1), (22, 2)]
    assert conn.execute("SELECT nb_fan FROM artists WHERE id=11").fetchone() == (1234,)
    assert {r[0] for r in conn.execute("SELECT genre FROM album_genres")} == {"Pop", "Rock"}

    n = len(s.calls)
    res2 = enrich.enrich(conn, session=s, throttle=0)
    assert len(s.calls) == n and res2["http_fetched"] == 0  # everything served from cache


def test_best_match_rejects_other_songs():
    assert enrich.best_match([hit(1, "Artist A", "Different", 1, 1)], "Artist A", "Song One") is None
    assert enrich.best_match([hit(1, "Artist A feat. B", "Song One", 1, 1)], "Artist A", "Song One")["id"] == 1


def test_limit_caps_fetches(monkeypatch):
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    conn = db.connect(":memory:")
    for i in range(3):
        db.upsert(conn, "local", f"{i}.mp3", {"artist": "Nobody", "title": f"Song {i}"})
    s = FakeSession({"search": {"data": []}})
    assert enrich.enrich(conn, session=s, limit=2, throttle=0)["http_fetched"] == 2


def test_best_match_uses_word_tokens():
    assert enrich.best_match([hit(1, "Artist A", "Lovely Day", 1, 1)], "Artist A", "Love") is None
    assert enrich.best_match([hit(1, "Artist A", "Love - Radio Edit", 1, 1)], "Artist A", "Love")["id"] == 1


def test_era_filled_for_tracks_of_already_known_album(monkeypatch):
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO albums VALUES (101, 'Some Album', 1994)")
    db.upsert(conn, "deezer", 1, {"artist": "Artist A", "title": "Song", "deezer_id": 1, "deezer_album_id": 101})
    enrich.enrich(conn, session=FakeSession({"/artist": {"data": []}}), throttle=0)
    assert conn.execute("SELECT era FROM tracks").fetchone() == ("1990s",)


def test_transient_error_retried_and_failure_leaves_no_row(monkeypatch):
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    conn = db.connect(":memory:")
    db.upsert(conn, "deezer", 1, {"artist": "A", "title": "S", "deezer_id": 1, "deezer_artist_id": 5, "deezer_album_id": 6})
    busy = {"error": {"code": 700}}
    s = FakeSession({"/artist/5/related": {"data": []}, "/artist/5": [busy, {"id": 5, "nb_fan": 3}],
                     "/album/6": {"error": {"code": 999}}})
    enrich.enrich(conn, session=s, throttle=0)
    assert conn.execute("SELECT nb_fan FROM artists").fetchall() == [(3,)]  # 700 retried
    assert conn.execute("SELECT COUNT(*) FROM albums").fetchone() == (0,)  # unknown error: no placeholder


def test_resolve_merges_into_track_with_same_deezer_id(monkeypatch):
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    conn = db.connect(":memory:")
    db.upsert(conn, "deezer", 1, {"artist": "Artist A", "title": "Song One Extended", "deezer_id": 1})
    db.upsert(conn, "local", "a.mp3", {"artist": "Artist A", "title": "Song One", "era": "1980s"})
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone() == (2,)
    s = FakeSession({"search": {"data": [hit(1, "Artist A", "Song One Extended", 11, 101)]}})
    enrich.resolve(conn, enrich.Deezer(conn, s, throttle=0))
    assert conn.execute("SELECT COUNT(*), MAX(era) FROM tracks").fetchone() == (1, "1980s")
    assert conn.execute("SELECT COUNT(DISTINCT track_id) FROM track_sources").fetchone() == (1,)


def test_limit_checked_per_request(monkeypatch):
    monkeypatch.setattr(enrich.time, "sleep", lambda s: None)
    conn = db.connect(":memory:")
    db.upsert(conn, "deezer", 1, {"artist": "A", "title": "S", "deezer_id": 1, "deezer_artist_id": 5})
    s = FakeSession({"/artist": {"data": []}})
    res = enrich.enrich(conn, session=s, limit=1, throttle=0)
    assert res["http_fetched"] == 1 and res["artists"] == 0  # half-fetched artist not marked done
