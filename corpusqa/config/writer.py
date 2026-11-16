"""Comment-preserving configuration writer."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from corpusqa.config.loader import format_validation_error
from corpusqa.config.schema import AppConfig
from corpusqa.errors import ConfigError

ConfigChange = tuple[tuple[str, ...], object]


def _plain(value: object) -> object:
    """Converts ruamel containers to plain dicts/lists for validation."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _apply(data: dict[str, Any], change: ConfigChange) -> None:
    keys, value = change
    if not keys:
        raise ConfigError("empty config key path")
    node: dict[str, Any] = data
    for key in keys[:-1]:
        child = node.get(key)
        if child is None:
            node[key] = {}
            child = node[key]  # ruamel may wrap the new mapping
        if not isinstance(child, dict):
            raise ConfigError(f"cannot set {'.'.join(keys)}: {key} is not a mapping")
        node = child
    if value is None:
        node.pop(keys[-1], None)
    else:
        node[keys[-1]] = value


def save_config_changes(path: Path, changes: list[ConfigChange]) -> AppConfig:
    """Applies changes to the YAML file, keeping comments and formatting.

    Args:
        path: The config file.
        changes: Key paths with new values; a value of None deletes the key.

    Returns:
        The validated configuration after the changes.

    Raises:
        ConfigError: If the file cannot be read or the result is invalid. The
            file is left untouched in that case.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    try:
        data = yaml.load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping, got {type(data).__name__}")
    for change in changes:
        _apply(data, change)
    try:
        config = AppConfig.model_validate(_plain(data))
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc, path)) from exc
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            yaml.dump(data, handle)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return config


def config_as_dict(config: AppConfig) -> dict[str, object]:
    """Dumps a configuration as JSON-safe plain values for the GUI.

    Args:
        config: The validated configuration.

    Returns:
        Nested dicts/lists with enums rendered as their string values.
    """
    return dict(config.model_dump(mode="json"))
