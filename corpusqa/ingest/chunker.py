"""Vector-channel chunking via Docling HybridChunker.

Chunks serve the vector channel only; pass-2 extraction reads whole-file
markdown (design doc section 4.1). Chunking runs at conversion time on the
in-memory DoclingDocument (contract change M2-C2). Chunk texts are persisted
to ``<hash>.chunks.jsonl`` in the parse cache until LanceDB exists (M5);
provenance rows go to SQLite.

Tokenizer policy: the default is an offline character-heuristic tokenizer
(~4 chars/token) so first run never depends on a HuggingFace download; users
can configure an HF tokenizer matched to their embedding model. Chunk-size
precision at this stage is not worth a network dependency (judgment: KISS).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Chunk:
    """One vector-channel chunk with full provenance.

    Attributes:
        chunk_id: ``f"{file_hash}:{chunk_index}"``.
        file_hash: Owning file identity.
        chunk_index: Zero-based position within the file.
        heading_path: Ancestor headings, root to leaf.
        page_no: First source page of the chunk, when paginated.
        char_start: Best-effort offset of the chunk text in the cached
            markdown; ``-1`` when the serialized chunk text does not occur
            verbatim (HybridChunker serialization can differ from the
            markdown export).
        char_end: Best-effort end offset; ``-1`` when unknown.
        text: Chunk text.
    """

    chunk_id: str
    file_hash: str
    chunk_index: int
    heading_path: list[str]
    page_no: int | None
    char_start: int
    char_end: int
    text: str


def _approx_token_count(text: str) -> int:
    """Offline char-heuristic token count (~4 chars/token), minimum 1.

    Module-level so the same callable object is reused across files; docling
    hands this to ``semchunk.chunkerify``, which memoizes token counters in a
    process-global dict keyed by identity. A fresh closure per file would leak
    one cache entry per file.
    """
    return max(1, len(text) // 4)


def _make_tokenizer(target_tokens: int, hf_model: str | None) -> Any:  # noqa: ANN401
    """Builds the chunker tokenizer (HF when configured, else offline)."""
    from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer

    if hf_model is not None:
        from docling_core.transforms.chunker.tokenizer.huggingface import (
            HuggingFaceTokenizer,
        )

        return HuggingFaceTokenizer.from_pretrained(
            model_name=hf_model, max_tokens=target_tokens
        )

    class Approx(BaseTokenizer):
        max_tokens: int = target_tokens

        def count_tokens(self, text: str) -> int:
            return _approx_token_count(text)

        def get_max_tokens(self) -> int:
            return self.max_tokens

        def get_tokenizer(self) -> Any:  # noqa: ANN401
            # docling's HybridChunker passes this to semchunk.chunkerify when
            # re-splitting oversize chunks; semchunk accepts a token-counting
            # callable. Returning None makes chunkerify call lru_cache(None),
            # which raises "the first argument must be callable". Return the
            # same heuristic used by count_tokens so the two stay consistent.
            return _approx_token_count

    return Approx()


def chunk_document(
    document: Any,  # noqa: ANN401 -- DoclingDocument; keeps docling lazy
    file_hash: str,
    markdown: str,
    target_tokens: int,
    hf_tokenizer_model: str | None = None,
) -> list[Chunk]:
    """Chunks one converted document for the vector channel.

    Args:
        document: The DoclingDocument from conversion.
        file_hash: Owning file identity.
        markdown: The cached markdown export (for best-effort char spans).
        target_tokens: Target chunk size in tokens.
        hf_tokenizer_model: Optional HF tokenizer name matched to the
            embedding model; None selects the offline heuristic.

    Returns:
        Ordered chunks with provenance.
    """
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker

    chunker = HybridChunker(
        tokenizer=_make_tokenizer(target_tokens, hf_tokenizer_model)
    )
    chunks: list[Chunk] = []
    for index, raw in enumerate(chunker.chunk(document)):
        meta: Any = raw.meta  # runtime DocMeta; statically typed as BaseMeta
        headings = list(meta.headings or [])
        page_no: int | None = None
        for item in meta.doc_items:
            if item.prov:
                page_no = item.prov[0].page_no
                break
        start = markdown.find(raw.text)
        end = start + len(raw.text) if start >= 0 else -1
        chunks.append(
            Chunk(
                chunk_id=f"{file_hash}:{index}",
                file_hash=file_hash,
                chunk_index=index,
                heading_path=headings,
                page_no=page_no,
                char_start=start,
                char_end=end,
                text=raw.text,
            )
        )
    return chunks


def write_chunk_texts(chunks: list[Chunk], cache_dir: Path, file_hash: str) -> None:
    """Persists chunk records to ``<hash>.chunks.jsonl`` pending M5.

    Args:
        chunks: Chunks to persist.
        cache_dir: Parse-cache directory.
        file_hash: Owning file identity (names the sidecar).
    """
    path = cache_dir / f"{file_hash}.chunks.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")