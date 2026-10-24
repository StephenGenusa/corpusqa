"""Shared unit-test fixtures."""

from __future__ import annotations

import pytest

from corpusqa import splitting


@pytest.fixture(autouse=True)
def _fresh_input_budget_warnings() -> None:
    """Input-budget warnings are logged once per process; tests start clean."""
    splitting.reset_input_budget_warnings()
