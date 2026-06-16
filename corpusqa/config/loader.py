"""YAML configuration loading with fail-fast diagnostics."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from corpusqa.config.schema import AppConfig
from corpusqa.errors import ConfigError


def load_config(path: Path) -> AppConfig:
    """Loads and validates a corpusqa YAML configuration file.

    Args:
        path: Path to the YAML file.

    Returns:
        The validated application configuration.

    Raises:
        ConfigError: If the file is missing, unparseable, or fails schema
            validation. The message aggregates all validation errors so the
            user fixes the file in one pass.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping, got {type(data).__name__}")
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ConfigError(
            f"invalid configuration in {path}:\n" + "\n".join(lines)
        ) from exc
