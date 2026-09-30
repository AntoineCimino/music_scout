"""Parse a `tree` dump of a local music folder and ingest it into SQLite."""
import re

from src.db import upsert

AUDIO = re.compile(r"\.(mp3|m4a|flac|ogg|opus|wav|aac|wma)$", re.I)
PREFIX = re.compile(r"^((?:[│ \xa0][ \xa0]{3})*)[├└]──[ \xa0]")  # indent units + branch; `tree` emits NBSPs


def folder_tags(folder):
    """'1970' -> era '1970s'; '1940 - 1950 -1960' -> '1940s-1960s'; other -> genre hint."""
    years = re.findall(r"\b(1[89]\d0|20\d0)\b", folder)
    if years:
        return (f"{years[0]}s" if len(years) == 1 else f"{years[0]}s-{years[-1]}s"), None
    return None, folder


def split_name(stem):
    # Strip a track number only when it can't be part of the artist ("50 Cent - X"):
    # "01 - Artist - Title" / "01. Artist - Title", or a name with no separator at all.
    m = re.match(r"^\d{1,3}\s*[-.]\s+(.*-.*)$", stem)
    if m:
        stem = m.group(1)
    elif "-" not in stem:
        stem = re.sub(r"^\d{1,3}[\s._]+", "", stem)
    stem = stem.strip()
    for sep in (" - ", " – ", "-"):
        if sep in stem:
            artist, title = stem.split(sep, 1)
            return artist.strip(), title.strip()
    return "", stem  # ponytail: no artist in filename, key is title-only; MUSIC-003 search can fix


def parse_tree(lines):
    """Yield (path, artist, title, era, genre_hint) for every audio file line."""
    stack = []
    for line in lines:
        m = PREFIX.match(line.rstrip("\n"))
        if not m:
            continue
        depth, name = len(m.group(1)) // 4, line.rstrip("\n")[m.end():]
        stack = stack[:depth]
        if not AUDIO.search(name):
            stack.append(name)
            continue
        era = genre = None
        for folder in stack:
            e, g = folder_tags(folder)
            era, genre = e or era, g or genre
        artist, title = split_name(AUDIO.sub("", name))
        yield "/".join([*stack, name]), artist, title, era, genre


def ingest(conn, tree_path):
    with open(tree_path, encoding="utf-8") as f:
        rows = list(parse_tree(f))
    for path, artist, title, era, genre in rows:
        upsert(conn, "local", path, {"artist": artist, "title": title, "era": era, "genre_hint": genre})
    conn.commit()
    return len(rows)
