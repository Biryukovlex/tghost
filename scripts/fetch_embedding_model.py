"""Fetch the pinned embedding model files and verify their hashes.

Used at image build time so the running app never downloads anything. The
revision and file hashes are pinned; a mismatch fails the build.

    python scripts/fetch_embedding_model.py /opt/models/paraphrase-multilingual-MiniLM-L12-v2
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

REPO = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
FILES = {
    # published path -> (local name, sha256)
    "onnx/model.onnx": ("model.onnx", "10f7a088420252b26caf819236ca2c9d2987afd0fc06fec7553b542a5655a05a"),
    "tokenizer.json": ("tokenizer.json", "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8"),
}


def fetch(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for remote, (name, expected) in FILES.items():
        destination = target / name
        url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{remote}"
        digest = hashlib.sha256()
        partial = destination.with_suffix(destination.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as handle:  # noqa: S310 - fixed https URL
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                handle.write(chunk)
        if digest.hexdigest() != expected:
            partial.unlink(missing_ok=True)
            raise SystemExit(f"hash mismatch for {remote}")
        partial.replace(destination)
        print(f"{name}: {destination.stat().st_size} bytes, sha256 ok")


if __name__ == "__main__":
    fetch(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/models/paraphrase-multilingual-MiniLM-L12-v2"))
