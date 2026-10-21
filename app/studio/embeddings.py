"""Local multilingual text embeddings for ranking and similarity.

Model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (Apache-2.0),
the official ONNX export, run on CPU with ONNX Runtime. It was chosen on the
TGhost test sets (plans/embedding-eval-2026-10-21 in the agent state): top-3
passage ranking 91% for English and 90% for Russian queries, and it recognises
an English story as the same one a Russian post covered. The files are fetched
at image build time by scripts/fetch_embedding_model.py; nothing is downloaded
at run time and no text leaves the machine.

When the model files are absent (local development without the image), the
embedder is unavailable and callers keep their original order.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol, Sequence

from .. import limits

log = logging.getLogger(__name__)

# The onnxruntime wheel ships a usage-telemetry client. Measured with strace:
# it opens an outside connection on session start unless this variable is set
# before the library initialises; disable_telemetry_events() alone does not
# stop it. The image sets it too.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")


class TextEmbedder(Protocol):
    def encode(self, texts: Sequence[str]) -> Any: ...


class OnnxMiniLM:
    """Mean-pooled, L2-normalised sentence embeddings, as sentence-transformers computes them."""

    def __init__(self, model_dir: Path, *, threads: int) -> None:
        import numpy as np
        import onnxruntime
        from tokenizers import Tokenizer

        self._np = np
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=int(limits.EMBEDDING_MAX_TOKENS))
        self._tokenizer.enable_padding()
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = max(1, threads)
        options.inter_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(str(model_dir / "model.onnx"), options,
                                                     providers=["CPUExecutionProvider"])
        self._inputs = {item.name for item in self._session.get_inputs()}
        self._lock = threading.Lock()

    def encode(self, texts: Sequence[str]) -> Any:
        np = self._np
        if not texts:
            return np.zeros((0, int(limits.EMBEDDING_DIMENSIONS)), dtype=np.float32)
        chunks = []
        size = max(1, int(limits.EMBEDDING_BATCH))
        for start in range(0, len(texts), size):
            encoded = self._tokenizer.encode_batch([str(text or " ") for text in texts[start:start + size]])
            ids = np.array([item.ids for item in encoded], dtype=np.int64)
            mask = np.array([item.attention_mask for item in encoded], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            with self._lock:
                hidden = self._session.run(None, feeds)[0]
            weights = mask[..., None].astype(np.float32)
            pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
            chunks.append(pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None))
        return np.vstack(chunks).astype(np.float32)


_embedder: TextEmbedder | None = None
_loaded = False
_load_lock = threading.Lock()


def model_dir() -> Path:
    return Path(limits.EMBEDDING_MODEL_DIR)


def get_embedder() -> TextEmbedder | None:
    """The process-wide embedder, loaded once; None when the model is not installed."""
    global _embedder, _loaded
    if _loaded:
        return _embedder
    with _load_lock:
        if not _loaded:
            directory = model_dir()
            if (directory / "model.onnx").is_file() and (directory / "tokenizer.json").is_file():
                try:
                    _embedder = OnnxMiniLM(directory, threads=int(limits.EMBEDDING_THREADS))
                except Exception as exc:  # noqa: BLE001 - ranking degrades to original order
                    log.warning("embedding model failed to load: %s", type(exc).__name__)
                    _embedder = None
            else:
                log.info("embedding model not installed at %s; ranking keeps original order", directory)
            _loaded = True
    return _embedder


def set_embedder(embedder: TextEmbedder | None) -> None:
    """Install a specific embedder (tests, or a different deployment)."""
    global _embedder, _loaded
    _embedder, _loaded = embedder, True


async def embed(texts: Sequence[str]) -> Any | None:
    """Embed off the event loop; None when no embedder is available."""
    embedder = get_embedder()
    if embedder is None or not texts:
        return None
    return await asyncio.to_thread(embedder.encode, list(texts))


async def rank(query: str, candidates: Sequence[str]) -> list[int] | None:
    """Indices of candidates, most similar to the query first; None without an embedder."""
    vectors = await embed([query, *candidates])
    if vectors is None:
        return None
    scores = vectors[1:] @ vectors[0]
    return [int(index) for index in (-scores).argsort(kind="stable")]
