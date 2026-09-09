"""Use-case classification — one of the two places a model touches an estimate.

Free-text or blank use cases are mapped to one of the catalog's task types so
the deterministic engine can price them. The model only ever returns a *label
from a fixed set*; it never computes a cost, and a label it invents is thrown
away in favour of the keyword rules.

Three things about this port are deliberate.

*It goes through Compass's own model client.* The original built its own
`AzureOpenAI` from its own settings, which in Compass would mean a second
configuration path to keep in step with the first, and no `mock_model` — so
every test of a costing route would want a live deployment. `complete_utility`
already handles the deployment fallback, the mock and the refusal shape.

*It is async.* That is what the shared client is, and this is called from a
pipeline stage rather than from inside the arithmetic, so it can be.

*The Anthropic path is gone.* Compass runs on Azure OpenAI. Carrying a second
provider means carrying a second SDK, a second failure mode and a second thing
to get wrong for a call that returns fifteen characters.

What survives untouched is the part that matters: the keyword rules below, and
the rule that anything the model returns which is not in `TASK_TYPES` is
replaced by them. The estimate is identical with or without a model configured,
which is the property that makes the figures defensible.
"""

from __future__ import annotations

import json
import logging

from compass.common.config import get_settings
from compass.common.gateway.azure_client import get_model_client

from .catalog import TASK_TYPES
from .types import AIUseCase

logger = logging.getLogger("compass.estimate")

# Priority-ordered keyword → task-type rules. First match wins, so the more
# specific / higher-intensity patterns are listed before the generic ones.
_KEYWORD_RULES: list[tuple[tuple[str, ...], str]] = [
    (("multi-agent", "multi agent", "orchestrat", "coordinat agent", "agent team"), "multi_agent_orchestration"),
    (("rag", "retriev", "knowledge base", "knowledge-base", "q&a", "qa over", "ask your", "ask the docs"), "rag_qa"),
    (("summar", "tl;dr", "digest", "recap"), "summarization"),
    (("translat", "localis", "localiz", "multilingual"), "translation"),
    (("ocr", "image", "vision", "photo", "picture", "screenshot", "visual"), "image_analysis"),
    (("contract", "document analysis", "analyse document", "analyze document", "pdf", "invoice parsing"), "document_analysis"),
    (("classif", "categor", "triage", "sentiment", "tag ", "label ", "routing", "intent"), "text_classification"),
    (("extract", "parse", "scrape", "pull fields", "structured data"), "data_extraction"),
    (("code", "program", "sql generat", "refactor", "unit test", "boilerplate"), "code_generation"),
    (("recommend", "suggest", "personaliz", "personalis", "next best"), "recommendation"),
    (("anomaly", "fraud", "outlier", "intrusion", "threat detect"), "anomaly_detection"),
    # Deterministic capabilities — match before the conversational catch-all so a plain
    # lookup or rules flow isn't swept up as an AI assistant.
    (("crud", "look up", "lookup", "status check", "record retrieval", "data entry", "form submission", "database query"), "crud_lookup"),
    (("approval workflow", "business rule", "rules engine", "if-then", "decision table", "deterministic workflow", "state machine"), "rules_workflow"),
    (("threshold", "alert when", "monitoring alert", "sla breach", "limit exceeded", "metric alert"), "threshold_alerting"),
    (("chatbot", "chat bot", "assistant", "conversation", "copilot", "agent", "chat"), "conversational_agent"),
]

_DEFAULT_TASK = "rag_qa"


def heuristic_task_type(text: str) -> str:
    """Deterministic keyword mapping; the fallback when no model is available."""
    t = (text or "").lower()
    for keywords, task in _KEYWORD_RULES:
        if any(k in t for k in keywords):
            return task
    return _DEFAULT_TASK


_SYSTEM_PROMPT = (
    "You label software use cases by task type. You only ever return a JSON "
    "object — never prose, never a cost or number."
)


def _build_prompt(items: list[tuple[str, str]]) -> str:
    """The classification instruction. One call for the whole list rather than
    one per use case: the labels are not independent — telling a summariser
    from a RAG assistant is easier with the other rows in view."""
    listing = "\n".join(f"{i}. {name} — {desc}" for i, (name, desc) in enumerate(items))
    return (
        "Classify each software use case below into exactly one task type from this set:\n"
        f"{', '.join(TASK_TYPES)}.\n\n"
        "Use cases:\n"
        f"{listing}\n\n"
        'Respond with ONLY a JSON object mapping the index (as a string) to the task type, '
        'e.g. {"0": "rag_qa", "1": "summarization"}. No prose.'
    )


def _parse_label_map(raw: str, items: list[tuple[str, str]]) -> dict[int, str]:
    """Extract {index: task_type} from raw model text, validating every label.

    Any label not in TASK_TYPES (or any parse glitch for a row) is replaced by
    the deterministic keyword heuristic, so a sloppy model can never inject an
    unpriceable task type.
    """
    raw = raw.strip()
    if raw.startswith("```"):  # tolerate fenced output
        raw = raw.strip("`").split("\n", 1)[-1]
    start, end = raw.find("{"), raw.rfind("}")
    parsed = json.loads(raw[start : end + 1])
    out: dict[int, str] = {}
    for k, v in parsed.items():
        idx = int(k)
        out[idx] = v if v in TASK_TYPES else heuristic_task_type(items[idx][0] + " " + items[idx][1])
    return out


async def _classify_with_model(items: list[tuple[str, str]]) -> dict[int, str] | None:
    """Ask the configured deployment to label each item. None on any failure."""
    try:
        raw = await get_model_client().complete_utility(
            _SYSTEM_PROMPT,
            _build_prompt(items),
            max_tokens=1_000,
            schema={
                "type": "object",
                "description": "index (as a string) -> task type",
                "additionalProperties": {"type": "string", "enum": list(TASK_TYPES)},
            },
            schema_name="task_types",
        )
        return _parse_label_map(raw, items)
    except Exception as exc:  # noqa: BLE001 - any failure means fall back to the rules
        logger.warning("classify failed, using keyword rules: %s", exc)
        return None


async def classify_use_cases(use_cases: list[AIUseCase]) -> list[AIUseCase]:
    """Fill in any missing/invalid task types. Returns a new list (inputs untouched)."""
    if not use_cases:
        return use_cases

    needs: list[int] = [
        i for i, uc in enumerate(use_cases)
        if not uc.task_type or uc.task_type not in TASK_TYPES
    ]
    if not needs:
        return use_cases

    resolved: dict[int, str] = {}
    items = [(use_cases[i].name, use_cases[i].description) for i in needs]
    if get_settings().estimate.classifier:
        labels = await _classify_with_model(items)
        if labels is not None:
            resolved = {
                needs[j]: labels.get(j, heuristic_task_type(items[j][0] + " " + items[j][1]))
                for j in range(len(needs))
            }

    out: list[AIUseCase] = []
    for i, uc in enumerate(use_cases):
        if i in needs:
            task = resolved.get(i) or heuristic_task_type(f"{uc.name} {uc.description}")
            out.append(uc.model_copy(update={"task_type": task}))
        else:
            out.append(uc)
    return out
