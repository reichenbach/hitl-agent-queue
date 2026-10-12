from __future__ import annotations

try:
    import tomllib
except ImportError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib
from pathlib import Path
from typing import Any

from .base import Agent, AgentLimits
from .cli import CliAgent
from .fake import FakeAgent
from .openai_compat import OpenAICompatAgent


def load_agents(path: str | Path = "agents.toml") -> dict[str, Agent]:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as handle:
        document = tomllib.load(handle)
    entries = document.get("agents")
    if not isinstance(entries, dict):
        raise ValueError("agents.toml must contain an [agents] table")
    return {
        name: _build_agent(name, data, config_path.parent)
        for name, data in entries.items()
    }


def _build_agent(name: str, data: Any, base_dir: Path) -> Agent:
    if not isinstance(data, dict):
        raise ValueError(f"agent {name!r} must be a table")
    allowed = _string_list(data.get("allowed_folders"), name, "allowed_folders")
    limits = AgentLimits(
        allowed_folders=tuple(
            (Path(folder) if Path(folder).is_absolute() else base_dir / folder).resolve()
            for folder in allowed
        ),
        max_turns=_positive_int(data.get("max_turns"), name, "max_turns"),
        timeout_seconds=_positive_number(data.get("timeout_seconds"), name, "timeout_seconds"),
        max_output_bytes=_positive_int(
            data.get("max_output_bytes", 1_048_576), name, "max_output_bytes"
        ),
    )
    kind = data.get("type")
    if kind == "cli":
        return CliAgent(
            name,
            _string_list(data.get("command"), name, "command"),
            limits,
            extra_args=_optional_string_list(data.get("extra_args"), name, "extra_args"),
            turns_flag=_optional_string(data.get("turns_flag"), name, "turns_flag"),
            prompt_via=data.get("prompt_via", "stdin"),
            env_passthrough=_optional_string_list(
                data.get("env_passthrough"), name, "env_passthrough"
            ),
        )
    if kind == "openai_compat":
        return OpenAICompatAgent(
            name,
            _required_string(data.get("base_url"), name, "base_url"),
            _required_string(data.get("model"), name, "model"),
            limits,
            _optional_string(data.get("api_key_env"), name, "api_key_env"),
        )
    if kind == "fake":
        return FakeAgent(name, limits, output=str(data.get("output", "")))
    raise ValueError(f"agent {name!r} has unsupported type {kind!r}")


def _string_list(value: Any, agent: str, field: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"agent {agent!r} field {field!r} must be a non-empty string list")
    return value


def _optional_string_list(value: Any, agent: str, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"agent {agent!r} field {field!r} must be a string list")
    return value


def _required_string(value: Any, agent: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"agent {agent!r} field {field!r} must be a non-empty string")
    return value


def _optional_string(value: Any, agent: str, field: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, agent, field)


def _positive_int(value: Any, agent: str, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"agent {agent!r} field {field!r} must be a positive integer")
    return value


def _positive_number(value: Any, agent: str, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"agent {agent!r} field {field!r} must be a positive number")
    return float(value)
