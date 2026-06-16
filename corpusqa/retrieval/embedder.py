"""Embedding wrapper with model/dimension fingerprinting (M5).

On startup the configured embed model + dimension is compared to the index
fingerprint; a mismatch locks out the vector channel until re-embedding
(``EmbeddingMismatchError``) -- never silently mix embedding spaces.
"""

from __future__ import annotations


def check_fingerprint(
    config_model: str,
    config_dim: int,
    index_model: str | None,
    index_dim: int | None,
) -> None:
    """Validates config embedding identity against the index fingerprint.

    Args:
        config_model: Embed model string from configuration.
        config_dim: Embedding dimension from configuration.
        index_model: Model recorded in ``index_meta`` (None on fresh index).
        index_dim: Dimension recorded in ``index_meta``.

    Raises:
        EmbeddingMismatchError: If a recorded fingerprint differs from the
            configured one.
    """
    raise NotImplementedError("M5")
