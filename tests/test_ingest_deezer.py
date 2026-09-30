import pytest

from src import db, ingest_deezer


def trk(i, artist, title, isrc):
    return {"id": i, "title": title, "isrc": isrc, "rank": 1000 * i, "time_add": 1700000000 + i,
            "duration": 200, "artist": {"id": 10 + i, "name": artist}, "album": {"id": 100 + i, "title": f"Album {i}"}}


PAGES = {
    None: {"data": [trk(1, "Artist A", "Song One", "XX0000000001"), trk(2, "Artist B", "Song Two", "XX0000000002")],
           "total": 3, "next": "https://example.test/next"},
    "https://example.test/next": {"data": [trk(3, "Artist C", "Song Three (Remastered 2011)", "XX0000000003")], "total": 3},
}


class FakeResp:
    def __init__(self, body): self.body = body
    def raise_for_status(self): pass
    def json(self): return self.body


class FakeSession:
    def __init__(self, pages): self.pages, self.calls = pages, []
    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        return FakeResp(self.pages[None if params else url])


def test_paginates_and_stores(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    s = FakeSession(PAGES)
    assert ingest_deezer.ingest(conn, "123", s) == 3
    assert len(s.calls) == 2
    row = conn.execute("SELECT artist, title, isrc, deezer_id, album, rank, added_at FROM tracks WHERE deezer_id=1").fetchone()
    assert row == ("Artist A", "Song One", "XX0000000001", 1, "Album 1", 1000, 1700000001)


def test_idempotent(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    ingest_deezer.ingest(conn, "123", FakeSession(PAGES))
    ingest_deezer.ingest(conn, "123", FakeSession(PAGES))
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM track_sources").fetchone()[0] == 3


def test_total_mismatch_warns_and_keeps(capsys):
    pages = {None: {"data": [trk(1, "A", "B", "X")], "total": 5}}
    assert len(ingest_deezer.fetch_tracks("1", FakeSession(pages))) == 1
    assert "warning" in capsys.readouterr().err


def test_api_error_body_raises():
    with pytest.raises(RuntimeError):
        ingest_deezer.fetch_tracks("1", FakeSession({None: {"error": {"code": 800}}}))
