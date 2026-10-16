"""Launcher for the bundled tray application and its Open app command."""

from __future__ import annotations

import sys

from hitl_queue.cli import main


if __name__ == "__main__":
    arguments = ["app", *sys.argv[2:]] if sys.argv[1:2] == ["app"] else ["tray", *sys.argv[1:]]
    raise SystemExit(main(arguments))
