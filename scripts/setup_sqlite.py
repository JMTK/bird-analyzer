"""One-time setup for the SQLite storage backend.

Creates the SQLite database file and required tables. Pure Python/stdlib,
so it runs on any device (Windows/macOS/Linux/Raspberry Pi) with no
external services required.

Usage:
  python scripts/setup_sqlite.py [--path runtime/bird-analyzer.db]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import SqliteStorage  # noqa: E402


def main() -> None:
  parser = argparse.ArgumentParser(description="Initialize the SQLite storage backend")
  parser.add_argument(
    "--path",
    default=str(Path.cwd() / "runtime" / "bird-analyzer.db"),
    help="Path to the SQLite database file (default: runtime/bird-analyzer.db)",
  )
  args = parser.parse_args()

  storage = SqliteStorage(args.path)
  storage.setup()

  print(f"SQLite database ready at {args.path}")
  print()
  print("Set the following in config.py to use it:")
  print('  storage_backend = "sqlite"')
  print(f'  sqlite_path = r"{args.path}"')


if __name__ == "__main__":
  main()
