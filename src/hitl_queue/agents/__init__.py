"""Adapters for running local and OpenAI-compatible agents."""

from .base import Agent, AgentLimits, AgentResult
from .cli import CliAgent
from .config import load_agents
from .fake import FakeAgent
from .openai_compat import OpenAICompatAgent

__all__ = [
    "Agent",
    "AgentLimits",
    "AgentResult",
    "CliAgent",
    "FakeAgent",
    "OpenAICompatAgent",
    "load_agents",
]
