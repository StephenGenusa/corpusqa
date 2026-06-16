"""Typed exception hierarchy.

Library code raises these; only the CLI layer translates them to messages
and exit codes (separation of concerns -- library never calls sys.exit).
"""


class CorpusQAError(Exception):
    """Base class for all corpusqa errors."""


class ConfigError(CorpusQAError):
    """Invalid, missing, or inconsistent configuration."""


class IngestError(CorpusQAError):
    """File discovery, parsing, or caching failure for a specific file."""


class LLMError(CorpusQAError):
    """An LLM call failed after retries/fallbacks were exhausted."""


class StructuredOutputError(LLMError):
    """Model output failed schema validation after the repair attempt.

    Attributes:
        raw_output: The final raw model output, retained for logging.
    """

    def __init__(self, message: str, raw_output: str) -> None:
        """Initializes the error.

        Args:
            message: Human-readable failure description.
            raw_output: The final raw model output that failed validation.
        """
        super().__init__(message)
        self.raw_output = raw_output


class BudgetExceededError(CorpusQAError):
    """Projected cost exceeded the configured threshold without confirmation."""
