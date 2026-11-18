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


class ProviderUnavailableError(LLMError):
    """The provider rejected a call in a way no retry or other input can fix.

    Examples: no model loaded (LM Studio), bad credentials, unknown model, or
    a server that stays unreachable after retries. Every later call of the
    run would fail the same way, so the run stops instead of continuing.
    """


class StructuredOutputError(LLMError):
    """Model output failed schema validation after the repair attempt.

    Attributes:
        raw_output: The final raw model output, retained for logging.
        output_exhausted: The model used its whole output allowance without
            writing an answer (e.g. a model that reasons first ran out of
            room); a larger ``max_tokens`` may succeed.
    """

    def __init__(
        self, message: str, raw_output: str, output_exhausted: bool = False
    ) -> None:
        """Initializes the error.

        Args:
            message: Human-readable failure description.
            raw_output: The final raw model output that failed validation.
            output_exhausted: See the class attributes.
        """
        super().__init__(message)
        self.raw_output = raw_output
        self.output_exhausted = output_exhausted


class BudgetExceededError(CorpusQAError):
    """Projected cost exceeded the configured threshold without confirmation."""
