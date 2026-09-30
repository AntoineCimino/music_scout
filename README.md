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
