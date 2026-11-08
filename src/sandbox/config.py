"""Repository configuration parser for .openjules.yaml."""

from pathlib import Path
from typing import Dict, List, Optional
import yaml
from pydantic import BaseModel, Field


class OpenJulesConfig(BaseModel):
    """Pydantic schema for .openjules.yaml configuration."""

    image: str = Field(
        default="python:3.11-slim",
        description="Base Docker image to spawn for the sandbox runtime.",
    )
    setup: List[str] = Field(
        default_factory=list,
        description="List of shell commands to prepare the environment (e.g. pip install -e .).",
    )
    test_command: str = Field(
        default="pytest",
        description="Shell command to verify the codebase after agent edits.",
    )
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables or secret keys to inject into the sandbox.",
    )
    timeout_seconds: int = Field(
        default=900,
        description="Total execution timeout in seconds for sandbox tasks.",
    )


def load_config_from_yaml(content: str) -> OpenJulesConfig:
    """Parse YAML string into OpenJulesConfig.

    Returns default OpenJulesConfig if content is empty or invalid YAML.
    """
    if not content or not content.strip():
        return OpenJulesConfig()
    try:
        parsed = yaml.safe_load(content)
        if not isinstance(parsed, dict):
            return OpenJulesConfig()
        return OpenJulesConfig(**parsed)
    except Exception:
        return OpenJulesConfig()


def load_config_from_file(file_path: Path | str) -> OpenJulesConfig:
    """Read .openjules.yaml file if it exists, otherwise return defaults."""
    path = Path(file_path)
    if not path.is_file():
        return OpenJulesConfig()
    try:
        content = path.read_text(encoding="utf-8")
        return load_config_from_yaml(content)
    except Exception:
        return OpenJulesConfig()
