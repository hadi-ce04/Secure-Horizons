"""Config loading and hashing (the single shared place for both).

Config hash = SHA-256 hex of the canonical JSON of
``{"model": model, "system_prompt": system_prompt, "tool_schemas": tool_schemas}``.
``name``, ``version`` and ``description`` are metadata and are not hashed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent.tools import get_tool_schemas

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS_DIR = REPO_ROOT / "configs"
REQUIRED_STRING_FIELDS = ("name", "version", "description", "model", "system_prompt")


def canonical_json(obj) -> str:
    """Canonical JSON serialization used for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def compute_config_hash(model: str, system_prompt: str, tool_schemas) -> str:
    """SHA-256 over the canonical (model, system_prompt, tool_schemas) triple."""
    body = {"model": model, "system_prompt": system_prompt, "tool_schemas": list(tool_schemas)}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentConfig:
    """An immutable agent configuration."""

    name: str
    version: str
    description: str
    model: str
    system_prompt: str
    tools: tuple
    tool_schemas: tuple
    path: str = ""

    @property
    def config_hash(self) -> str:
        """Content hash of model + system prompt + tool schemas."""
        return compute_config_hash(self.model, self.system_prompt, list(self.tool_schemas))

    def to_dict(self) -> dict:
        """JSON-serializable representation including the hash."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "tools": list(self.tools),
            "tool_schemas": list(self.tool_schemas),
            "path": self.path,
            "hash": self.config_hash,
        }

    def replace(self, **changes) -> "AgentConfig":
        """Return a copy with fields replaced."""
        return dataclasses.replace(self, **changes)


def resolve_path(p) -> Path:
    """Resolve a config path as given, then relative to the repo root."""
    candidate = Path(p)
    if candidate.exists():
        return candidate.resolve()
    repo_relative = REPO_ROOT / p
    if repo_relative.exists():
        return repo_relative.resolve()
    raise FileNotFoundError(f"Config not found: tried {candidate} and {repo_relative}")


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def load_config(path) -> AgentConfig:
    """Load and validate a YAML config file."""
    resolved = resolve_path(path)
    with open(resolved, "r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ValueError(f"{resolved.name}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{resolved.name}: config must be a YAML mapping")
    for key in REQUIRED_STRING_FIELDS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{resolved.name}: field '{key}' must be a non-empty string")
    tools = data.get("tools")
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise ValueError(f"{resolved.name}: field 'tools' must be a list of tool names")
    try:
        schemas = get_tool_schemas(tools)
    except KeyError as exc:
        raise ValueError(f"{resolved.name}: {exc.args[0]}") from exc
    return AgentConfig(
        name=data["name"],
        version=str(data["version"]),
        description=data["description"],
        model=data["model"],
        system_prompt=data["system_prompt"],
        tools=tuple(tools),
        tool_schemas=tuple(schemas),
        path=_display_path(resolved),
    )


def list_configs() -> list[Path]:
    """Return the sorted list of configs/*.yaml files."""
    return sorted(CONFIGS_DIR.glob("*.yaml"))
