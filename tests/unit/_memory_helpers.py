# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for memory import/export tests.

``FakeEmbeddingFunction`` is a deterministic, dependency-free stand-in for
ChromaDB's default embedding function so tests never trigger the ~80 MB model
download (see CLAUDE.md gotcha). Patch it in via ``patch_fake_embeddings``.
"""

from __future__ import annotations

import hashlib

from chromadb.api.types import EmbeddingFunction


class FakeEmbeddingFunction(EmbeddingFunction):
    """Hash each document to a fixed-length float vector. No model, no network."""

    def __init__(self) -> None:
        pass

    def __call__(self, input: list[str]) -> list[list[float]]:
        return [
            [float(b) for b in hashlib.sha256(text.encode()).digest()[:16]]
            for text in input
        ]


def patch_fake_embeddings(monkeypatch) -> None:
    """Force every collection access to use the fake embedding function.

    Patching only ``DefaultEmbeddingFunction`` is not enough: ChromaDB
    re-derives the real default embedder when ``get_collection`` rehydrates an
    existing on-disk collection (e.g. each CLI invocation builds a fresh store
    against the same persist dir), which would download the ~80 MB model and
    cause a dimension mismatch against the fake-embedded vectors. Replacing
    ``_get_collection`` with a ``get_or_create_collection`` call that always
    passes the fake EF keeps the embedding dimension consistent across
    instances. Test-only — the vendored source is untouched.
    """
    from sqllens.agent.integrations.chromadb.agent_memory import ChromaAgentMemory

    def _get_collection(self: ChromaAgentMemory):  # type: ignore[no-untyped-def]
        if self._collection is None:
            self._collection = self._get_client().get_or_create_collection(
                name=self.collection_name,
                embedding_function=FakeEmbeddingFunction(),
                metadata={"description": "Tool usage memories for learning"},
            )
        return self._collection

    monkeypatch.setattr(ChromaAgentMemory, "_get_collection", _get_collection)
