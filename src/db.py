"""SQLite schema, track-key normalization and upsert/dedup logic."""
import re
import sqlite3
import unicodedata

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,          -- normalized "artist|title"
    artist TEXT, title TEXT, isrc TEXT UNIQUE,
    deezer_id INTEGER, deezer_artist_id INTEGER, album TEXT, deezer_album_id INTEGER,
    rank INTEGER, duration INTEGER, era TEXT, genre_hint TEXT, added_at INTEGER
);
CREATE TABLE IF NOT EXISTS track_sources (
    track_id INTEGER NOT NULL REFERENCES tracks(id),
    source TEXT NOT NULL,              -- 'deezer' | 'local'
    source_ref TEXT NOT NULL,          -- deezer track id or local relative path
    PRIMARY KEY (source, source_ref)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub(r"[(\[].*?[)\]]", " ", s)                      # (Remastered 2011), [Live], (feat. X)
    s = re.sub(r"\s-\s.*(remaster|version|edit|mix|live|mono|stereo).*$", " ", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", " ", s)
    s = s.replace("&", " and ")
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def track_key(artist, title):
    # ponytail: primary artist only ("A & B", "A, B", "A feat. B" -> "a"); may merge
    # rare same-title songs by a duo and its lead artist, split keys if that matters.
    primary = re.split(r"\s*(?:&|,|/|\bfeat\.?|\bft\.?|\bx\b)\s*", artist or "", maxsplit=1, flags=re.I)[0]
    return f"{_norm(primary)}|{_norm(title)}"


def upsert(conn, source, source_ref, row):
    """Insert or merge a track; returns track id. Match by ISRC, else normalized key.

    `row` keys are tracks columns (artist, title required). Existing non-null values win,
    NULL columns are filled in, so the ingest order does not matter.
    """
    key = track_key(row["artist"], row["title"])
    hit = None
    if row.get("isrc"):
        hit = conn.execute("SELECT id FROM tracks WHERE isrc=?", (row["isrc"],)).fetchone()
    hit = hit or conn.execute("SELECT id FROM tracks WHERE key=?", (key,)).fetchone()
    if hit:
        tid = hit[0]
        sets = ", ".join(f"{c}=COALESCE({c}, ?)" for c in row)
        conn.execute(f"UPDATE tracks SET {sets} WHERE id=?", (*row.values(), tid))
    else:
        cols = ["key", *row]
        tid = conn.execute(
            f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (key, *row.values()),
        ).lastrowid
    conn.execute("INSERT OR IGNORE INTO track_sources VALUES (?, ?, ?)", (tid, source, str(source_ref)))
    return tid
