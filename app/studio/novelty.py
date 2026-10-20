"""Novelty and copy checks measured against the channel's own archive.

A story is "already covered" when its text is closer to an archive post than
two different archive posts typically are to each other. The threshold is
the median nearest-neighbour similarity inside the archive itself, so it
adapts to every channel's vocabulary instead of a fixed cut-off. Copying is
measured as runs of consecutive words shared with an archive post, after
removing the lines a channel repeats on many posts (signatures, footers).
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from typing import Iterable

# A line that recurs in at least this share of posts is the channel's
# signature or footer, not content; it is ignored by the copy check.
SIGNATURE_LINE_SHARE = 0.1


def normalize_text(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"\s+", " ", re.sub(r"[*_~`>#]", " ", text.lower())).strip()


def _grams(text: str, size: int = 4) -> Counter:
    value = normalize_text(text)
    return Counter(value[index:index + size] for index in range(max(0, len(value) - size + 1)))


class ArchiveCorpus:
    """TF-IDF over character 4-grams with an inverted index for fast similarity."""

    def __init__(self, texts: Iterable[str]):
        self.texts = [text for text in texts if (text or "").strip()]
        counts = [_grams(text) for text in self.texts]
        document_frequency: Counter = Counter()
        for vector in counts:
            document_frequency.update(vector.keys())
        total = len(counts)
        self.idf = {gram: math.log((1 + total) / (1 + seen)) + 1 for gram, seen in document_frequency.items()}
        self.vectors = [self._weight(vector) for vector in counts]
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for index, weighted in enumerate(self.vectors):
            for gram, weight in weighted.items():
                self.postings[gram].append((index, weight))

    def _weight(self, vector: Counter) -> dict[str, float]:
        weighted = {gram: count * self.idf.get(gram, 1.0) for gram, count in vector.items()}
        norm = math.sqrt(sum(value * value for value in weighted.values())) or 1.0
        return {gram: value / norm for gram, value in weighted.items()}

    def similarities(self, text: str, *, skip: int | None = None) -> list[float]:
        query = self._weight(_grams(text))
        scores = [0.0] * len(self.vectors)
        for gram, weight in query.items():
            for index, other in self.postings.get(gram, ()):
                scores[index] += weight * other
        if skip is not None and 0 <= skip < len(scores):
            scores[skip] = 0.0
        return scores

    def max_similarity(self, text: str) -> float:
        return max(self.similarities(text), default=0.0)

    def covered_threshold(self, *, sample: int = 200) -> float:
        """Median similarity between an archive post and its closest other post."""
        if len(self.texts) < 3:
            return 1.0
        step = max(1, len(self.texts) // sample)
        nearest = [max(self.similarities(self.texts[index], skip=index), default=0.0)
                   for index in range(0, len(self.texts), step)]
        return float(statistics.median(nearest))


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", normalize_text(text))


def signature_lines(texts: list[str]) -> set[str]:
    counts: Counter = Counter()
    for text in texts:
        counts.update({normalize_text(line) for line in (text or "").split("\n") if normalize_text(line)})
    floor = max(2, int(SIGNATURE_LINE_SHARE * len(texts)))
    return {line for line, seen in counts.items() if seen >= floor}


def copied_fragments(draft: str, texts: list[str], *, words: int, ignore: set[str] | None = None) -> list[str]:
    """Runs of `words` consecutive words the draft shares with any archive text."""

    ignore = ignore or set()

    def content(text: str) -> list[str]:
        return _words("\n".join(line for line in (text or "").split("\n") if normalize_text(line) not in ignore))

    draft_words = content(draft)
    shingles = {tuple(draft_words[index:index + words]) for index in range(len(draft_words) - words + 1)}
    found: set[str] = set()
    for text in texts:
        other = content(text)
        found.update(" ".join(shared) for shared in shingles.intersection(
            tuple(other[index:index + words]) for index in range(len(other) - words + 1)))
    return sorted(found)
