from src import db, ingest_deezer, ingest_tree
from src.db import track_key
from tests.test_ingest_deezer import PAGES, FakeSession

TREE = """.
├── 1940 - 1950 -1960
│   ├── Artist A - Song One.mp3
│   └── Élodie Ñ - Café (Live).mp3
├── 1970
│   ├── Sub
│   │   └── Deep Artist - Deep Song.flac
│   └── Artist C-Song Three.mp3
├── Classique
│   ├── 08 Lonely Title.m4a
│   └── cover.jpg
└── Artist B & Friend - Song Two feat. X.mp3

3 directories, 6 files
"""


def test_parse_nbsp_indent():
    # real `tree` output indents with "│\xa0\xa0 " instead of plain spaces
    rows = list(ingest_tree.parse_tree(TREE.replace("│   ", "│\xa0\xa0 ").splitlines()))
    assert len(rows) == 6 and rows[2][0] == "1970/Sub/Deep Artist - Deep Song.flac"


def test_parse_nested_and_odd_names():
    rows = list(ingest_tree.parse_tree(TREE.splitlines()))
    by_title = {r[2]: r for r in rows}
    assert len(rows) == 6
    assert by_title["Song One"][3] == "1940s-1960s"
    assert by_title["Deep Song"][0] == "1970/Sub/Deep Artist - Deep Song.flac"
    assert by_title["Deep Song"][3] == "1970s"
    assert by_title["Song Three"][1] == "Artist C"
    assert by_title["Lonely Title"][1:5] == ("", "Lonely Title", None, "Classique")
    assert by_title["Song Two feat. X"][3:] == (None, None)


def test_track_key_normalization():
    assert track_key("Élodie Ñ", "Café (Live)") == track_key("elodie n", "CAFE")
    assert track_key("Artist C", "Song Three (Remastered 2011)") == track_key("Artist C", "Song Three")
    assert track_key("Artist B & Friend", "Song Two feat. X") == track_key("Artist B", "Song Two")
    assert track_key("A", "Song - 2011 Remaster") == track_key("A", "Song")


def test_dedup_merges_sources_any_order(tmp_path):
    for order in ("deezer_first", "local_first"):
        conn = db.connect(tmp_path / f"{order}.db")
        tree = tmp_path / "tree.txt"
        tree.write_text(TREE)
        steps = [lambda: ingest_deezer.ingest(conn, "1", FakeSession(PAGES)), lambda: ingest_tree.ingest(conn, tree)]
        for step in (steps if order == "deezer_first" else steps[::-1]) * 2:  # twice: idempotent
            step()
        assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 6  # 3 deezer + 6 local - 3 overlap
        merged = conn.execute("SELECT t.title, t.era, t.isrc FROM tracks t JOIN track_sources s ON s.track_id=t.id "
                              "GROUP BY t.id HAVING COUNT(DISTINCT s.source)=2 ORDER BY t.isrc").fetchall()
        assert [m[2] for m in merged] == ["XX0000000001", "XX0000000002", "XX0000000003"]
        assert merged[0][1] == "1940s-1960s"


def test_split_name_track_numbers():
    assert ingest_tree.split_name("50 Cent - In Da Club") == ("50 Cent", "In Da Club")
    assert ingest_tree.split_name("01 - 50 Cent - In Da Club") == ("50 Cent", "In Da Club")
    assert ingest_tree.split_name("01. Artist - Title") == ("Artist", "Title")
    assert ingest_tree.split_name("08 Lonely Title") == ("", "Lonely Title")


def test_missing_config_exits_clearly(tmp_path):
    import pytest
    from src import cli
    with pytest.raises(SystemExit, match="config.example.yaml"):
        cli.main(["--config", str(tmp_path / "nope.yaml"), "stats"])
