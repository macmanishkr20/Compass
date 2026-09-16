"""The REST surface for Estimate.

Mounted only when the module is enabled, which is why the import in
`api/server.py` sits inside the conditional: a module that is switched off
should not appear in the route table, and the route table is compared against a
recorded snapshot, so leakage would be caught rather than merely unlikely.

The endpoints follow the shape the other modules use — list, create, get,
delete — plus `stream`, which is the same creation running with its progress
sent as it happens. `/catalog` is the one carrying design: the task types,
scales, complexities and platforms a brief may use are the engine's, so the
form is rendered from what the server says it can price rather than from a
second copy of the same lists maintained in the client. A task type added to
`catalog.py` becomes selectable with no frontend change.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from compass.common.auth import require_user
from compass.common.config import get_settings
from compass.common.ownership import owned, owner_for, visible_to

from . import export, intake, pipeline
from . import store as estore
from .catalog import EFFORT_UNIT_HOURS, SIZE_BAND_UNITS, TASK_TYPES
from .platforms import PLATFORM_PROFILES
from .types import Estimation, ProjectInput

logger = logging.getLogger("compass.estimate")

router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_fields(brief: ProjectInput, estimation: Estimation) -> dict:
    """The denormalised columns the list view reads, taken off a finished
    estimate. One place, so a list row can never disagree with the report it
    opens."""
    return {
        "name": brief.project_name,
        "project_type": brief.project_type,
        "industry_domain": brief.industry_domain,
        "score": estimation.feasibility.score,
        "total_expected": estimation.cost_breakdown.total.expected,
        "currency": estimation.cost_breakdown.currency,
        "verdict": estimation.verdict.decision if estimation.verdict else "",
        "confidence": estimation.confidence.level if estimation.confidence else "",
        "status": "complete",
    }


async def _owned_estimate(estimate_id: str, user: str) -> estore.Estimate:
    record = await estore.estimates.get(estimate_id)
    if record is None or not visible_to(user, record.owner):
        # The same answer either way: whether a record exists is itself
        # something a user who cannot see it has no business learning.
        raise HTTPException(status_code=404, detail="Estimate not found")
    return record


# --------------------------------------------------------------------------- catalog


@router.get("/v1/estimates/catalog")
async def catalog(user: str = Depends(require_user)) -> dict:
    """What a brief may contain, as the engine understands it."""
    return {
        "task_types": list(TASK_TYPES),
        "scales": ["small", "medium", "large", "enterprise"],
        "complexities": ["low", "medium", "high", "very_high"],
        "priorities": ["must_have", "nice_to_have", "exploratory"],
        # The work-breakdown vocabulary. Sent rather than hard-coded in the
        # form so a line's hours are read off the same table the engine
        # prices with, and re-banding stays a one-file change.
        "unit_hours": EFFORT_UNIT_HOURS,
        "size_bands": [
            {"key": k, "units": u, "hours": u * EFFORT_UNIT_HOURS}
            for k, u in SIZE_BAND_UNITS.items()
        ],
        "platforms": [
            {"key": p.key, "label": p.label, "cost_model": p.cost_model}
            for p in PLATFORM_PROFILES.values()
        ],
        "stages": [{"key": k, "label": label} for k, label in pipeline.STAGES],
        # Whether a model will be asked to label anything on this box. The UI
        # says so rather than implying an intelligence that is switched off.
        "classifier": get_settings().estimate.classifier,
        "architect": get_settings().estimate.architect,
        "draft": get_settings().estimate.draft,
    }


# --------------------------------------------------------------------------- rate card


class RateCard(BaseModel):
    dev_hourly_rate: float = Field(gt=0, le=100_000)
    maint_hourly_rate: float = Field(gt=0, le=100_000)
    effective_hours_per_week: float = Field(gt=0, le=168)
    #: Everything around the code, as a multiple of it. 0 is legal and means
    #: "we cost implementation only" — which is what the model used to do.
    delivery_overhead: float = Field(ge=0, le=5)
    loaded_hourly_rate: float = Field(gt=0, le=100_000)
    automation_rate_percent: float = Field(ge=0, le=100)
    #: Zero is legal and means "no ramp" — the old behaviour, reachable on
    #: purpose for anyone who wants to compare against it.
    benefit_ramp_months: float = Field(ge=0, le=60)


@router.get("/v1/estimates/rate-card")
async def get_rate_card(user: str = Depends(require_user)) -> dict:
    """The dials this person's estimates default to.

    Bounded rather than free: `effective_hours_per_week` above 168 is not a
    number of hours, and a zero rate makes every figure zero without anything
    looking broken. The ceilings are absurdly high on purpose — they exist to
    catch a slipped decimal point, not to have an opinion about what anyone
    pays.
    """
    return {"rate_card": await estore.rate_cards.get(owner_for(user)),
            "baseline": estore.BASELINE_RATES}


@router.put("/v1/estimates/rate-card")
async def put_rate_card(body: RateCard, user: str = Depends(require_user)) -> dict:
    saved = await estore.rate_cards.save(owner_for(user), body.model_dump())
    return {"rate_card": saved, "baseline": estore.BASELINE_RATES}


# --------------------------------------------------------------------------- drafting


class DraftRequest(BaseModel):
    description: str = Field(min_length=1, max_length=8_000)
    project_type: str = "new"


@router.post("/v1/estimates/draft")
async def draft(body: DraftRequest, user: str = Depends(require_user)) -> dict:
    """Turn a paragraph into a brief the person then corrects.

    Returns the brief and nothing else — no estimate, no id, nothing stored.
    Drafting and costing are separate calls on purpose: a draft that priced
    itself on the way past would put a number in front of someone before they
    had read the assumptions it came from, and that number is the one they
    would remember.
    """
    if not get_settings().estimate.draft:
        raise HTTPException(status_code=501, detail="Drafting is switched off on this server")
    try:
        brief = await intake.draft_brief(body.description, body.project_type)
    except Exception as exc:  # noqa: BLE001 - the surface says what went wrong
        logger.warning("draft failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Could not draft the brief: {exc}") from exc
    return {"brief": brief.model_dump()}


class BrdRequest(BaseModel):
    name: str = Field(min_length=1, max_length=260)
    mime: str = Field(default="", max_length=200)
    #: The file as a data URL. Twenty megabytes of document is about 27 MB
    #: of base64.
    data_url: str = Field(min_length=1, max_length=28_000_000)
    project_type: str = "new"


#: What a requirements document arrives as. Images are refused rather than
#: read: a photographed BRD is a scan, and a scan extracts to nothing.
_BRD_EXTENSIONS = {"pdf", "docx", "md", "markdown", "txt"}


@router.post("/v1/estimates/brd")
async def read_brd(body: BrdRequest, user: str = Depends(require_user)) -> dict:
    """Read an uploaded requirements document and draft the brief from it.

    Returns the brief — carrying what the reading found — and nothing else,
    exactly as `/draft` does: nothing costed, nothing stored, no id. The team
    and skill scores are asked of the person afterwards, in the form.
    """
    if not get_settings().estimate.draft:
        raise HTTPException(status_code=501, detail="Drafting is switched off on this server")
    ext = body.name.rsplit(".", 1)[-1].lower() if "." in body.name else ""
    if ext not in _BRD_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="Upload the BRD as a PDF, Word (.docx), Markdown or text file.",
        )

    import asyncio

    from compass.common.attachments import extract_document_text

    text = await asyncio.to_thread(extract_document_text, body.name, body.mime, body.data_url)
    note = text.strip()
    # The extractor never raises; it writes a bracketed note instead. Each is
    # turned into something a person can act on rather than passed to a model
    # as if it were the requirement.
    if not note:
        raise HTTPException(status_code=422, detail="The document is empty.")
    if note.endswith("had no extractable text]"):
        raise HTTPException(
            status_code=422,
            detail="This document has no text layer — it looks scanned. Upload the "
                   "Word version, or export the PDF with its text.",
        )
    if "No module named 'docx'" in note:
        raise HTTPException(
            status_code=422,
            detail="This server cannot read Word files yet (python-docx is not "
                   "installed). Upload the BRD as a PDF instead.",
        )
    if note.startswith("[could not read"):
        raise HTTPException(status_code=422,
                            detail=f"Could not read the document: {note.strip('[]')}")

    try:
        brief = await intake.analyse_brd(text, body.name, body.project_type)
    except Exception as exc:  # noqa: BLE001 - the surface says what went wrong
        logger.warning("brd analysis failed: %s", exc)
        raise HTTPException(status_code=502,
                            detail=f"Could not analyse the document: {exc}") from exc
    return {"brief": brief.model_dump(), "characters": len(text)}


# --------------------------------------------------------------------------- preview


@router.post("/v1/estimates/preview")
async def preview(brief: ProjectInput, user: str = Depends(require_user)) -> dict:
    """Cost a brief without keeping it, and without calling a model.

    The form shows a running estimate beside it as you type. That number has to
    come from somewhere, and there were only three options: mirror the engine
    in TypeScript, invent a second cheaper formula for the sidebar, or ask the
    server. The first is the 1,400-line duplicate this port deleted, and the
    second is worse — a rail that disagrees with the report it is previewing
    teaches people to distrust both.

    So it is the same engine, called for real. It skips the two model-touching
    stages deliberately rather than for speed: `resolve` may take twenty
    seconds, and a sidebar cannot wait for that on every keystroke. The
    platform comes from the keyword heuristic, which is what the architect
    falls back to anyway — so the preview matches the estimate exactly on any
    brief the architect does not overrule, and the report says which platform
    was finally chosen.

    Nothing is stored and no id is minted. This is arithmetic, not a record.
    """
    resolved, proposal = pipeline.resolve_without_model(brief)
    estimation = pipeline.compute(resolved, "preview", _now_iso(), proposal)
    return {"estimation": estimation.model_dump()}


# --------------------------------------------------------------------------- estimates


@router.get("/v1/estimates")
async def list_estimates(user: str = Depends(require_user)) -> dict:
    """Summaries only — the payloads are large and a list never reads them."""
    items = owned(await estore.estimates.list(), user)
    items.sort(key=lambda e: e.updated_at, reverse=True)
    return {"estimates": [e.summary() for e in items]}


@router.post("/v1/estimates")
async def create_estimate(brief: ProjectInput,
                          user: str = Depends(require_user)) -> dict:
    """Estimate a brief and keep the result."""
    estimation = await pipeline.run_estimate(brief, estore._new_id(), _now_iso())
    record = await estore.estimates.create(
        brief=brief.model_dump(),
        result=estimation.model_dump(),
        owner=owner_for(user),
        **_record_fields(brief, estimation),
    )
    # The stored id wins over the one the pipeline was handed: the store mints
    # ids, and an estimate whose body disagrees with the record it lives in is
    # a bug waiting for whoever links to it.
    record.result["id"] = record.id
    await estore.estimates.save(record)
    return record.to_dict()


@router.post("/v1/estimates/stream")
async def stream_estimate(brief: ProjectInput,
                          user: str = Depends(require_user)) -> StreamingResponse:
    """The same estimate, with each stage announced as it starts.

    The stages are walked here rather than computed up front so a frame is sent
    because work began, not because a timer expired. Classification and the
    architect are awaited first — they are the slow pair, since they may call a
    model — and the arithmetic is then driven a step at a time.
    """
    labels = dict(pipeline.STAGES)
    order = [k for k, _ in pipeline.STAGES]

    def frame(stage: str, **extra) -> str:
        payload = {
            "stage": stage,
            "label": labels.get(stage, stage),
            "progress": round((order.index(stage) + 1) / len(order) * 100) if stage in order else 100,
            **extra,
        }
        return f"data: {json.dumps(payload)}\n\n"

    async def events():
        started = time.time()
        try:
            yield frame("classify")
            resolved, proposal = await pipeline.resolve(brief)
            yield frame("architect", platform=proposal.get("recommended_platform", ""))

            estimation: Estimation | None = None
            for stage, done in pipeline.compute_steps(
                resolved, estore._new_id(), _now_iso(), proposal
            ):
                if done is None:
                    yield frame(stage)
                else:
                    estimation = done

            if estimation is None:  # pragma: no cover - compute_steps always ends with one
                raise RuntimeError("estimate stages finished without producing a result")

            record = await estore.estimates.create(
                brief=brief.model_dump(),
                result=estimation.model_dump(),
                owner=owner_for(user),
                **_record_fields(brief, estimation),
            )
            record.result["id"] = record.id
            await estore.estimates.save(record)
            logger.info("estimate %s computed in %.0fms", record.id,
                        (time.time() - started) * 1000)
            yield frame("report", status="complete", estimate=record.to_dict())
        except Exception as exc:  # noqa: BLE001 - a stream must end, and say why
            logger.exception("estimate failed")
            yield f"data: {json.dumps({'stage': 'report', 'status': 'failed', 'error': str(exc)})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # Nginx buffers event streams into uselessness without this.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/v1/estimates/{estimate_id}")
async def get_estimate(estimate_id: str,
                       user: str = Depends(require_user)) -> dict:
    record = await _owned_estimate(estimate_id, user)
    return record.to_dict()


@router.get("/v1/estimates/{estimate_id}/export/pdf")
async def export_pdf(estimate_id: str,
                     user: str = Depends(require_user)) -> Response:
    """The report, printed.

    From the stored result rather than a fresh run: an estimate is a dated
    artefact, and a PDF that quietly re-derived today's answer to the same
    question would be a different document with the same title.
    """
    record = await _owned_estimate(estimate_id, user)
    estimation = Estimation.model_validate(record.result)
    try:
        data = await export.to_pdf(estimation)
    except RuntimeError as exc:  # Playwright missing — say so, do not 500
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="{export.slug(record.name)}-estimate.pdf"'},
    )


@router.get("/v1/estimates/{estimate_id}/export/excel")
async def export_excel(estimate_id: str,
                       user: str = Depends(require_user)) -> Response:
    """The numbers, in five sheets."""
    record = await _owned_estimate(estimate_id, user)
    estimation = Estimation.model_validate(record.result)
    try:
        data = export.to_excel(estimation)
    except ImportError as exc:
        raise HTTPException(
            status_code=501,
            detail="openpyxl is not installed on the server host (pip install openpyxl)",
        ) from exc
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{export.slug(record.name)}-estimate.xlsx"'},
    )


@router.delete("/v1/estimates/{estimate_id}")
async def delete_estimate(estimate_id: str,
                          user: str = Depends(require_user)) -> dict:
    await _owned_estimate(estimate_id, user)
    return {"deleted": await estore.estimates.delete(estimate_id)}
