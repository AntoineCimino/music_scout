# Music Scout

Open-source taste profiler and music/curator recommender.

Music Scout ingests the tracks you like (a public Deezer playlist, a local music
folder, later Spotify), enriches them, groups them into "listening modes"
(taste clusters), and recommends:

- **artists** you don't have yet, related to the ones you love;
- **curators**: owners of public playlists that overlap your taste while still
  bringing new tracks.

A small CLI feedback loop (like / dislike / already known) refines the next round.

> Status: early scaffold — features land ticket by ticket.

## Curators (how they are approximated)

Deezer has **no native curator concept** (no follower graph or "curator" flag in the
public API). `python -m src.cli recommend curators|playlists [--top N] [--max-playlists 150] [--users-only]`
approximates it:

1. per listening mode, search public playlists for the mode name, its top genres and top artists;
2. score each playlist on its first 100 tracks: `overlap` = share of tracks in your library or by
   a library artist; `novelty` = share of tracks you don't have by library or recommended artists;
   `score = overlap × (1 − |novelty − 0.6|)`, damped for huge (> 200 tracks) and single-artist playlists;
3. a curator = a playlist creator; curator score = best playlist score + 10% per extra matching playlist (max +30%).

Deezer editors are regular users; they are labelled `editorial` from their name
(e.g. "… - Deezer Rock Editor", "… - Pop & Hits Editor", French "Editrice").
`--users-only` hides them (editors dominate otherwise; label-run brands such as Topsify/Filtr still show as users). Your own playlists (creator of `deezer.playlist_id`)
and anything with a `feedback` row (`playlist` / `curator`) are skipped.

## Requirements

- Python 3.10+
- Optional: the [`claude` CLI](https://docs.claude.com/en/docs/claude-code) for naming/describing clusters (no API key needed)

## Setup

```bash
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml   # then edit
python -m src.cli --help
```

Data sources:
- **Deezer**: set `deezer.playlist_id` to any *public* playlist id (no auth required).
- **Local files**: run `tree` on your music folder (files named `Artist - Title.mp3`)
  and point `local.tree_file` to the output.

All your data (`data/`, `*.db`, `config/config.yaml`, `.env`) stays local and is gitignored.

## Tests

```bash
pytest tests/ -v
```

## License

MIT — see [LICENSE](LICENSE).
