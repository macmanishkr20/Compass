"""Work IQ — Home-only hybrid retrieval over Azure AI Search.

Powers the Home chat's optional "Work IQ" toggle: embed the user's question,
run a HYBRID query (keyword BM25 + vector, plus optional semantic rerank)
against the configured index, and return the top chunks so the chat can answer
grounded in the organization's knowledge base.

Entirely optional and isolated — nothing here is imported by the agent console.
All configuration comes from AZURE_AISEARCH_* env vars (see config.AiSearchSettings).
Every call is best-effort: on any failure it returns no documents, so the chat
simply reports that nothing relevant was found.
"""

from __future__ import annotations

import logging

import httpx

from compass.config import get_settings

logger = logging.getLogger("compass.work_iq")

_TIMEOUT = 20.0


def configured() -> bool:
    return get_settings().ai_search.configured


async def _embed(text: str) -> list[float] | None:
    """Embed the query with the configured Azure OpenAI embeddings deployment.
    Returns None (→ keyword-only search) when embeddings aren't configured."""
    ais = get_settings().ai_search
    az = get_settings().azure
    if not ais.embed_deployment or not az.endpoint or not az.api_key:
        return None
    url = (
        f"{az.endpoint.rstrip('/')}/openai/deployments/{ais.embed_deployment}"
        f"/embeddings?api-version={az.api_version}"
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={"api-key": az.api_key, "content-type": "application/json"},
                json={"input": text},
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    except Exception as err:  # noqa: BLE001
        logger.warning("Work IQ embedding failed: %s", err)
        return None


async def hybrid_search(query: str) -> list[dict]:
    """Top-K documents for the query (hybrid when embeddings are available,
    keyword-only otherwise). Each item: {content, title, url, score}."""
    ais = get_settings().ai_search
    if not ais.configured or not query.strip():
        return []

    content_fields = [f.strip() for f in ais.content_fields.split(",") if f.strip()]
    select = list(content_fields)
    for extra in (ais.title_field, ais.url_field):
        if extra and extra not in select:
            select.append(extra)

    body: dict = {
        "search": query,
        "top": ais.top_k,
        "select": ",".join(select),
    }
    vector = await _embed(query)
    if vector:
        body["vectorQueries"] = [
            {
                "kind": "vector",
                "vector": vector,
                "fields": ais.vector_field,
                "k": ais.top_k,
            }
        ]
    if ais.semantic_config:
        body["queryType"] = "semantic"
        body["semanticConfiguration"] = ais.semantic_config

    url = (
        f"{ais.endpoint.rstrip('/')}/indexes/{ais.index}/docs/search"
        f"?api-version={ais.api_version}"
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={"api-key": ais.api_key, "content-type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as err:  # noqa: BLE001
        logger.warning("Work IQ search failed: %s", err)
        return []

    out: list[dict] = []
    for doc in data.get("value", []):
        content = "\n".join(
            str(doc[f]) for f in content_fields if doc.get(f)
        ).strip()
        if not content:
            continue
        out.append(
            {
                "content": content,
                "title": (doc.get(ais.title_field) if ais.title_field else "") or "",
                "url": (doc.get(ais.url_field) if ais.url_field else "") or "",
                "score": doc.get("@search.rerankerScore") or doc.get("@search.score"),
            }
        )
    return out


def format_context(docs: list[dict]) -> str:
    """Numbered context block for the grounding system prompt."""
    if not docs:
        return ""
    parts = []
    for i, d in enumerate(docs, 1):
        header = f"[{i}]" + (f" {d['title']}" if d.get("title") else "")
        parts.append(f"{header}\n{d['content']}")
    return "\n\n".join(parts)


def sources_for_ui(docs: list[dict]) -> list[dict]:
    """Compact source list surfaced under the grounded answer."""
    return [
        {
            "n": i,
            "title": d.get("title") or f"Document {i}",
            "url": d.get("url") or "",
        }
        for i, d in enumerate(docs, 1)
    ]


WORK_IQ_SYSTEM_PROMPT = (
    "You are Compass Work IQ, answering from the organization's knowledge base. "
    "Use ONLY the retrieved documents below to answer the user's question. If the "
    "answer is not contained in them, clearly say you could not find it in the "
    "knowledge base — do NOT use outside knowledge, and do NOT guess. When you use "
    "a document, cite it inline like [1], [2]. Be concise and accurate.\n\n"
    "Retrieved documents:\n{context}\n\n"
    "(If the section above is empty, no documents matched — tell the user that "
    "nothing relevant was found in the knowledge base.)"
)
