"""Launcher for the bundled macOS desktop application."""

from __future__ import annotations

import sys

from hitl_queue.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["app", *sys.argv[1:]]))
