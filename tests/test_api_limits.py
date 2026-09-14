from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.intelligence import SimilarIncidentSearch
from src.server import _bounded_request_body, _intelligence_text


class _Request:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def stream(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_webhook_body_limit_reads_chunks_and_rejects_overflow() -> None:
    assert await _bounded_request_body(_Request([b"a", b"b"]), maximum=2) == b"ab"
    with pytest.raises(HTTPException) as error:
        await _bounded_request_body(_Request([b"a", b"b", b"c"]), maximum=2)
    assert error.value.status_code == 413


def test_intelligence_text_requires_string_and_bounds_utf8_bytes() -> None:
    assert _intelligence_text({"summary": " incident "}, "text", "summary") == "incident"
    with pytest.raises(HTTPException) as missing:
        _intelligence_text({"text": {"nested": True}}, "text")
    assert missing.value.status_code == 422
    with pytest.raises(HTTPException) as oversized:
        _intelligence_text({"text": "é" * 20_000}, "text")
    assert oversized.value.status_code == 422


def test_similarity_search_validates_direct_callers() -> None:
    search = SimilarIncidentSearch()
    with pytest.raises(ValueError, match="query must"):
        search.search("", limit=1)
    with pytest.raises(ValueError, match="query is too large"):
        search.search("x" * (32 * 1024 + 1), limit=1)
    with pytest.raises(ValueError, match="limit"):
        search.search("incident", limit=0)
    with pytest.raises(ValueError, match="limit"):
        search.search("incident", limit=True)
