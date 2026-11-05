"""Settings and named credentials."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Tried in order. A later model is only used when the one before it is busy, unavailable
# or out of daily quota.
DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]


class ConfigError(Exception):
    """A setting that's missing or wrong, reported to the user as it stands."""


@dataclass
class Settings:
    test_case: str
    url: str | None = None
    secrets: dict[str, str] = field(default_factory=dict)
    max_steps: int = 25
    time_limit_seconds: int = 600
    headed: bool = False
    output_dir: Path = Path("runs")


def models_from(flag: str | None) -> list[str]:
    """--model wins, then NLTEST_MODEL, then the defaults. The defaults stay as backups."""
    chosen = flag or os.environ.get("NLTEST_MODEL")
    if not chosen:
        return DEFAULT_MODELS
    return [chosen, *[model for model in DEFAULT_MODELS if model != chosen]]


def load_credentials(path: str | None) -> dict[str, str]:
    """Reads {"customer": {"email": "...", "password": "..."}} as customer_email and customer_password."""
    if path is None:
        return {}

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as problem:
        raise ConfigError(f"Could not read the credentials file: {problem}") from problem
    except json.JSONDecodeError as problem:
        raise ConfigError(f"The credentials file isn't valid JSON: {problem}") from problem

    if not isinstance(data, dict) or not all(isinstance(fields, dict) for fields in data.values()):
        raise ConfigError('Credentials must look like {"customer": {"email": "...", "password": "..."}}.')

    return {f"{role}_{name}": str(value) for role, fields in data.items() for name, value in fields.items()}
