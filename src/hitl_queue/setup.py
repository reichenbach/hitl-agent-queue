"""Non-interactive first-run setup shared by the CLI and desktop board."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .agents.config import tomllib
from .notify import NOTIFIER_ENV


def example_path() -> Path:
    """Package data lives beside this module, including in PyInstaller bundles."""
    return Path(__file__).with_name("agents.example.toml")


def _example() -> dict:
    return tomllib.loads(example_path().read_text(encoding="utf-8"))["agents"]


def needs_setup(state_dir: str | Path) -> bool:
    return not (Path(state_dir) / "agents.toml").is_file()


def setup_status(state_dir: str | Path) -> dict:
    state = Path(state_dir)
    return {
        "needed": needs_setup(state),
        "settings_exist": (state / "settings.json").is_file(),
        "agents": list(_example()),
        "notifiers": [
            {"name": name, "env": env} for name, env in NOTIFIER_ENV.items()
        ],
    }


def _toml_table(name: str, data: dict) -> str:
    """Serialize the example's scalar/list values and nested tables."""
    lines = [f"[{name}]"]
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{json.dumps(key)} = {json.dumps(value, ensure_ascii=False)}")
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append("\n" + _toml_table(f"{name}.{json.dumps(key)}", value))
    return "\n".join(lines) + "\n"


def _write_new(path: Path, text: str) -> bool:
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
        return True
    except FileExistsError:
        return False


def setup_queue(
    state_dir: str | Path, agents: list[str] | None = None, notifier: str = "stdout"
) -> dict:
    """Create missing configuration only; never read or persist credentials."""
    example = _example()
    selected = list(dict.fromkeys(example if agents is None else agents))
    if not selected or any(name not in example for name in selected):
        raise ValueError(f"Choose one or more agents from: {', '.join(example)}")
    if notifier not in NOTIFIER_ENV:
        raise ValueError(f"Choose a notifier from: {', '.join(NOTIFIER_ENV)}")
    state = Path(state_dir).expanduser().resolve()
    agents_file = state / "agents.toml"
    created = []
    if not agents_file.exists():
        workspace_root = state.with_name(state.name + "-workspaces")
        tables = []
        folders = set()

        def folder(raw: str) -> str:
            path = Path(raw)
            if not path.is_absolute():
                if path.parts and path.parts[0] == "workspaces":
                    path = Path(*path.parts[1:])
                path = workspace_root / path
            path = path.resolve()
            if path == state or state in path.parents or path in state.parents:
                raise ValueError("Agent workspace must not overlap the state directory")
            folders.add(path)
            return os.path.relpath(path, state)

        for name in selected:
            data = dict(example[name])
            data["allowed_folders"] = [folder(raw) for raw in data["allowed_folders"]]
            data["work_dir"] = folder(data["work_dir"])
            tables.append(_toml_table(f"agents.{json.dumps(name)}", data))
        state.mkdir(parents=True, exist_ok=True)
        for path in folders:
            path.mkdir(parents=True, exist_ok=True)
        if _write_new(agents_file, "\n".join(tables)):
            created.append("agents.toml")
    state.mkdir(parents=True, exist_ok=True)
    settings = {"notifier": notifier, "notifier_env": NOTIFIER_ENV[notifier]}
    if _write_new(state / "settings.json", json.dumps(settings, indent=2) + "\n"):
        created.append("settings.json")
    return {"ok": True, "created": created, **setup_status(state)}
