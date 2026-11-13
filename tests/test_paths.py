import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from hitl_queue import app, paths, tray


class UserDataDirTests(unittest.TestCase):
    def test_board_and_tray_share_helper_and_tray_import_still_works(self):
        self.assertIs(app.user_data_dir, paths.user_data_dir)
        self.assertIs(tray.user_data_dir, paths.user_data_dir)

    def test_platform_paths_and_environment_overrides(self):
        home = Path("test-home")
        cases = [
            ("darwin", {}, home / "Library/Application Support/HITL Queue"),
            ("win32", {}, home / "AppData/Roaming/HITL Queue"),
            ("win32", {"APPDATA": "roaming-data"}, Path("roaming-data/HITL Queue")),
            ("linux", {}, home / ".local/share/hitl-queue"),
            ("linux", {"XDG_DATA_HOME": "xdg-data"}, Path("xdg-data/hitl-queue")),
        ]
        for platform, environment, expected in cases:
            with self.subTest(platform=platform, environment=environment), \
                    mock.patch.object(sys, "platform", platform), \
                    mock.patch.object(Path, "home", return_value=home), \
                    mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(paths.user_data_dir(), expected)
