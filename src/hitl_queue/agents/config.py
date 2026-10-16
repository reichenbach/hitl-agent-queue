from __future__ import annotations

try:
    import tomllib
except ImportError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib
from pathlib import Path
from typing import Any

from .base import Agent, AgentLimits, UsagePlan
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
    work_value = _required_string(data.get("work_dir"), name, "work_dir")
    work_dir = (Path(work_value) if Path(work_value).is_absolute() else base_dir / work_value).resolve()
    if not any(work_dir.is_relative_to(folder) for folder in limits.allowed_folders):
        raise ValueError(f"agent {name!r} work_dir must be inside allowed_folders")
    allow_agent_to_agent = data.get("allow_agent_to_agent", False)
    if not isinstance(allow_agent_to_agent, bool):
        raise ValueError(f"agent {name!r} field 'allow_agent_to_agent' must be a boolean")
    usage_plan = _usage_plan(data.get("plan"), name)
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
            work_dir=work_dir,
            allow_agent_to_agent=allow_agent_to_agent,
            usage_plan=usage_plan,
            usage_regex=_optional_string(data.get("usage_regex"), name, "usage_regex"),
            usage_json_field=_optional_string(data.get("usage_json_field"), name, "usage_json_field"),
        )
    if kind == "openai_compat":
        return OpenAICompatAgent(
            name,
            _required_string(data.get("base_url"), name, "base_url"),
            _required_string(data.get("model"), name, "model"),
            limits,
            _optional_string(data.get("api_key_env"), name, "api_key_env"),
            work_dir=work_dir,
            allow_agent_to_agent=allow_agent_to_agent,
            usage_plan=usage_plan,
        )
    if kind == "fake":
        return FakeAgent(
            name, limits, output=str(data.get("output", "")), work_dir=work_dir,
            allow_agent_to_agent=allow_agent_to_agent,
            usage_plan=usage_plan,
        )
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


def _usage_plan(value: Any, agent: str) -> UsagePlan | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"agent {agent!r} field 'plan' must be a table")
    allowed = {"window", "runs", "minutes", "tokens", "warn_threshold", "blocked_threshold"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"agent {agent!r} plan has unknown fields: {', '.join(sorted(unknown))}")
    try:
        return UsagePlan(
            window=_required_string(value.get("window"), agent, "plan.window"),
            runs=_optional_positive_int(value.get("runs"), agent, "plan.runs"),
            minutes=_optional_positive_number(value.get("minutes"), agent, "plan.minutes"),
            tokens=_optional_positive_int(value.get("tokens"), agent, "plan.tokens"),
            warn_threshold=float(value.get("warn_threshold", 0.8)),
            blocked_threshold=float(value.get("blocked_threshold", 1.0)),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"agent {agent!r} invalid usage plan: {exc}") from exc


def _optional_positive_int(value: Any, agent: str, field: str) -> int | None:
    return None if value is None else _positive_int(value, agent, field)


def _optional_positive_number(value: Any, agent: str, field: str) -> float | None:
    return None if value is None else _positive_number(value, agent, field)
