"""Music Scout CLI entry point (subcommands added per backlog ticket)."""
import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(prog="music_scout", description="Taste profiler + music/curator recommender.")
    parser.parse_args(argv)
    print("Music Scout running")


if __name__ == "__main__":
    main()
