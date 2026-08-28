"""The Design section's REST surface.

Everything under /v1/design: projects and their versions, pages, files and
comments; design systems and the pages they are documented on; generating
a design, reviewing it, repairing what the review found, and exporting the
result. Mounted by server.py, which owns the app.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from compass.common.auth import require_user
from compass.common.paths import _SKIP_DIRS, _safe_join
from compass.common.config import get_settings
from compass.common.gateway.responses import ReasoningTrace

logger = logging.getLogger("compass.design")

router = APIRouter()


def _why_degraded(err: Exception) -> str:
    """What to tell someone about a document written without the good path.

    Phrased around the consequence rather than the cause: "rate limited" tells
    a reader nothing about whether to trust what is in front of them, whereas
    "written without web research, so anything recent may be out of date"
    tells them exactly what to check.
    """
    detail = str(err)
    if "429" in detail or "rate limit" in detail.lower():
        why = "the model was rate limited"
    elif "content" in detail.lower() and "filter" in detail.lower():
        why = "the content filter stopped the reasoning call"
    else:
        why = "the reasoning call failed"
    return (
        f"Written without deliberation or web research — {why}. "
        "Anything here that depends on recent facts came from the model's "
        "training data and may be out of date; check it before relying on it, "
        "or generate again."
    )


async def _think_through(
    prompt: str,
    asked: str,
    *,
    max_tokens: int,
    model: str = "",
    images: list[str] | None = None,
    prior: ReasoningTrace | None = None,
    effort: str = "",
) -> tuple[str, ReasoningTrace | None, str]:
    """Ask the model, letting it think, and keep what it thought.

    Design's work is a sequence rather than a single question: write the
    document, review it, correct what the review found, review again. Handing
    each step the reasoning from the one before it makes the next a
    continuation instead of a stranger meeting the document for the first
    time — the same reason an agent hands thinking back with a tool result.

    Falls back to a plain completion on a deployment that cannot reason, or
    when the reasoning call fails, so the sequence still runs — just without
    the continuity, and without the research that rides on the same call.

    The third return value is why it settled, empty when it did not have to.
    That value exists because the fallback is invisible in its output: asked
    for a brief on Angular's current release while rate limited, this returned
    a perfectly well-formed document about Angular 18 — the version in the
    training data, four majors behind, with nothing anywhere to say the
    research had not happened. A degraded document that looks identical to a
    good one is worse than a failed one.
    """
    from compass.common.config import get_settings
    from compass.common.gateway.azure_client import get_model_client

    settings = get_settings()
    client = get_model_client()
    deployment = model or settings.azure.deployment
    wanted = effort or settings.thinking.design_effort

    if settings.thinking.reasons(deployment):
        try:
            # Research while writing, the way Claude does: the tool is
            # offered, and the model decides per document whether the content
            # needs facts it does not have. A document about the conversation
            # above searches nothing; one about a library's current release
            # goes and checks. Verified that a hosted search and a strict
            # json_schema coexist on this API before relying on it.
            text, trace = await client.complete_reasoning(
                prompt, asked, max_tokens=max_tokens, deployment=deployment,
                images=images, effort=wanted, prior=prior,
                server_tools=True,
            )
            return text, trace, ""
        except Exception as err:  # noqa: BLE001 — never lose the step over this
            logger.warning("design: reasoning call failed (%s); plain completion", err)
            settled = _why_degraded(err)
    else:
        settled = (
            f"{deployment} does not support extended reasoning, so this was "
            "written without deliberation or web research."
        )

    out = await client.complete_utility(
        prompt, asked, max_tokens=max_tokens, prefer_main=True,
        model=model, images=images, effort=wanted,
    )
    return out, None, settled


class DesignCreate(BaseModel):
    name: str = ""
    template: str = "blank"
    prompt: str = ""
    design_system: str = ""
    design_systems: list[str] = []


class DesignPatch(BaseModel):
    name: str | None = None
    html: str | None = None
    prompt: str | None = None
    starred: bool | None = None
    design_system: str | None = None
    design_systems: list[str] | None = None
    turns: list[dict] | None = None
    # The pending question form. An empty object clears it — the project has
    # stopped waiting because it got its answer.
    clarify: dict | None = None


@router.get("/v1/design/templates")
async def design_templates(user: str = Depends(require_user)) -> dict:
    from compass.design.skills.catalogue import TEMPLATES

    return {"templates": TEMPLATES}


@router.get("/v1/design/projects")
async def design_projects(user: str = Depends(require_user)) -> dict:
    from compass.design.store import get_design_store

    return {"projects": await get_design_store().list()}


class DesignClarify(BaseModel):
    prompt: str
    template: str = "blank"
    answers: str = ""      # what the first round already settled
    followup: bool = False  # ask the next round rather than the first


class DesignAttachment(BaseModel):
    name: str
    mime: str = ""
    data_url: str = ""


@router.post("/v1/design/attach")
async def design_attach(
    body: DesignAttachment, user: str = Depends(require_user)
) -> dict:
    """Read an attachment the way Chat does — a PDF, a Word file or a zip is
    text once it has been through the same extractor, and the design gets to
    use it. Images come back untouched, for the model to look at."""
    from compass.common.attachments import process_attachment

    done = process_attachment(body.model_dump())
    if not done:
        raise HTTPException(status_code=415, detail="that file cannot be read")
    if done.get("kind") == "text" and not (done.get("text") or "").strip():
        raise HTTPException(
            status_code=422, detail="nothing readable in that file"
        )
    return done


@router.post("/v1/design/clarify")
async def design_clarify(body: DesignClarify, user: str = Depends(require_user)) -> dict:
    """Is this brief enough to design from? If not, what should we ask?"""
    import json as _json

    from compass.design.clarify import (
        CLARIFY_PROMPT,
        CLARIFY_SCHEMA,
        FOLLOWUP_FALLBACK,
        FOLLOWUP_PROMPT,
        normalize_clarify,
    )
    from compass.design.skills.catalogue import TEMPLATES

    prompt = body.prompt.strip()
    stem = next(
        (t.get("stem", "") for t in TEMPLATES if t["id"] == body.template), ""
    ).strip()
    # A prompt that is still just the template's opening words, or barely more
    # than that, is the case worth asking about. Anything fuller goes straight
    # through — nobody wants a form in front of a clear request.
    without_stem = prompt[len(stem):].strip() if stem and prompt.startswith(stem) else prompt
    if not body.followup and len(without_stem) >= 25:
        return {"ready": True}

    from compass.common.gateway.azure_client import get_model_client

    asked = f"Template: {body.template}\nRequest: {prompt or '(empty)'}"
    if body.answers.strip():
        asked += f"\n\nAlready answered:\n{body.answers.strip()}"

    try:
        # The answer is constrained to the schema, so the parsing below is
        # now a formality rather than the thing that decides whether anyone
        # sees a form. The prompt is unchanged: this constrains the shape of
        # the reply, not what is asked for.
        raw = await get_model_client().complete_utility(
            FOLLOWUP_PROMPT if body.followup else CLARIFY_PROMPT,
            asked,
            max_tokens=4_000,
            prefer_main=True,
            schema=CLARIFY_SCHEMA,
            schema_name="clarify",
        )
    except Exception:  # noqa: BLE001 - never block designing on this
        return {"ready": True}

    raw = raw.strip()
    if "```" in raw:
        import re as _re

        m = _re.search(r"```(?:json)?\s*\n(.*?)```", raw, _re.S)
        if m:
            raw = m.group(1).strip()
    try:
        parsed = _json.loads(raw)
    except ValueError:
        parsed = {}
    form = normalize_clarify(parsed) if parsed.get("fields") else {"fields": []}
    if parsed.get("ready") or not form["fields"]:
        # Being asked for another round is itself the answer: someone who
        # pressed the button wants questions, not "nothing to ask".
        return dict(FOLLOWUP_FALLBACK) if body.followup else {"ready": True}
    return form


@router.post("/v1/design/projects")
async def design_create(body: DesignCreate, user: str = Depends(require_user)) -> dict:
    from compass.design.store import get_design_store

    return await get_design_store().create(
        name=body.name or (body.prompt[:60] if body.prompt else "Untitled"),
        template=body.template,
        prompt=body.prompt,
        design_system=body.design_system,
        design_systems=body.design_systems,
    )


@router.get("/v1/design/projects/{project_id}")
async def design_get(project_id: str, user: str = Depends(require_user)) -> dict:
    from compass.design.store import get_design_store

    p = await get_design_store().get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


@router.patch("/v1/design/projects/{project_id}")
async def design_patch(
    project_id: str, body: DesignPatch, user: str = Depends(require_user)
) -> dict:
    from compass.design.store import get_design_store

    p = await get_design_store().update(project_id, **body.model_dump())
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


@router.delete("/v1/design/projects/{project_id}")
async def design_delete(project_id: str, user: str = Depends(require_user)) -> dict:
    from compass.design import files as design_files
    from compass.design.store import get_design_store

    deleted = await get_design_store().delete(project_id)
    if deleted:
        design_files.delete_project(project_id)
    return {"deleted": deleted}


class DesignSystemCreate(BaseModel):
    name: str = ""
    source: str = "pasted"   # pasted | upload | url | repo
    text: str = ""           # a style guide, a stylesheet, or notes
    css: str = ""            # tokens to reproduce verbatim
    url: str = ""            # a brand or docs page to read
    workspace_id: str = ""   # a repo already registered as a workspace
    path: str = ""           # ...and the folder inside it to read
    distil: bool = True      # read the source into a system, rather than storing it raw


# What a repo import reads. Stylesheets and token files carry the system; the
# rest of a codebase is noise that would only dilute the distillation.
_STYLE_SUFFIXES = {".css", ".scss", ".sass", ".less", ".styl"}
_TOKEN_NAMES = {
    "tailwind.config.js", "tailwind.config.ts", "theme.ts", "theme.js",
    "tokens.json", "design-tokens.json", "styleguide.md", "style-guide.md",
}
_REPO_READ_LIMIT = 60_000  # characters handed to the model


def _read_repo_styles(root: Path, rel: str) -> tuple[str, list[str]]:
    """Concatenate the stylesheets and token files under `rel`, biggest first."""
    target = _safe_join(root, rel)
    if not target.exists():
        raise HTTPException(status_code=404, detail="no such path in that workspace")

    files: list[Path] = []
    if target.is_file():
        files = [target]
    else:
        for p in target.rglob("*"):
            if any(part in _SKIP_DIRS for part in p.parts) or not p.is_file():
                continue
            if p.suffix.lower() in _STYLE_SUFFIXES or p.name in _TOKEN_NAMES:
                files.append(p)
    if not files:
        raise HTTPException(
            status_code=404, detail="found no stylesheets or token files there"
        )

    # Biggest first, but capped per file — one enormous stylesheet would
    # otherwise eat the whole budget and hide the rest of the system.
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    per_file = max(4_000, _REPO_READ_LIMIT // 8)
    chunks, names, budget = [], [], _REPO_READ_LIMIT
    for p in files:
        if budget <= 0:
            break
        try:
            body = p.read_text(errors="replace")[: min(per_file, budget)]
        except OSError:
            continue
        budget -= len(body)
        names.append(str(p.relative_to(root)))
        chunks.append(f"/* {p.relative_to(root)} */\n{body}")
    return "\n\n".join(chunks), names


async def _fetch_page(url: str) -> str:
    """Read a page's own markup for distillation. The user names the URL — it is
    never taken from generated content."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="only http(s) URLs can be read")
    import httpx

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            r = await client.get(url, headers={"user-agent": "Compass Design"})
            r.raise_for_status()
            return r.text[:_REPO_READ_LIMIT]
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"could not read {url}: {err}")


@router.get("/v1/design/systems")
async def design_systems(user: str = Depends(require_user)) -> dict:
    from compass.design.systems import BUILTIN_SYSTEMS, get_system_store

    return {"systems": await get_system_store().list(), "included": BUILTIN_SYSTEMS}


@router.post("/v1/design/systems")
async def design_system_create(
    body: DesignSystemCreate, user: str = Depends(require_user)
) -> dict:
    """Import a design system. With `distil`, the pasted source is read into a
    short system first — a whole stylesheet in the prompt would crowd out the
    actual design request."""
    from compass.design.systems import EXTRACT_PROMPT, get_system_store, parse_extract

    text, origin = body.text.strip(), ""
    if body.url.strip():
        text = await _fetch_page(body.url.strip())
        origin = body.url.strip()
    elif body.workspace_id:
        from compass.common.workspaces import get_workspace_registry

        root = await get_workspace_registry().resolve_root(body.workspace_id)
        text, names = _read_repo_styles(root, body.path)
        origin = f"{body.workspace_id}/{body.path}".rstrip("/") + f" ({len(names)} files)"

    if not text and not body.css.strip():
        raise HTTPException(status_code=400, detail="nothing to import")

    name, notes, fonts, swatches = body.name.strip(), text, "", []
    if text and body.distil:
        from compass.common.gateway.azure_client import get_model_client

        try:
            notes = (
                await get_model_client().complete_utility(
                    EXTRACT_PROMPT, text[:40_000], max_tokens=8_000, prefer_main=True
                )
            ).strip() or text
        except Exception as err:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"could not read that: {err}")
        read_name, fonts, swatches = parse_extract(notes)
        name = name or read_name

    return await get_system_store().create(
        name=name,
        source=body.source,
        notes=notes,
        css=body.css,
        fonts=fonts,
        swatches=swatches,
        origin=origin,
    )


class SystemSetup(BaseModel):
    """The set-up form: who you are, and whatever design material you can hand
    over. Everything but the blurb is optional — the point is to take what a
    team already has rather than make them write a specification."""

    name: str = ""
    blurb: str = ""            # company and what it makes, or the system's name
    github: str = ""           # https://github.com/owner/repo
    workspace_id: str = ""     # a repo already registered here
    path: str = ""             # ...and a folder inside it
    files: list[dict] = []     # {name, text} read in the browser
    images: list[str] = []     # data: URLs — logos, screenshots, brand pages
    notes: str = ""            # anything else worth knowing
    css: str = ""              # tokens to reproduce verbatim


@router.post("/v1/design/systems/setup")
async def design_system_setup(
    body: SystemSetup, user: str = Depends(require_user)
) -> dict:
    """Build a design system from everything the form collected."""
    from compass.design.systems import EXTRACT_PROMPT, get_system_store, parse_extract

    sources: list[str] = []
    origin_bits: list[str] = []

    if body.blurb.strip():
        sources.append("The company, in their words:\n" + body.blurb.strip())

    repo_workspace, repo_path = body.workspace_id.strip(), body.path.strip()
    if body.github.strip() and not repo_workspace:
        # Clone it, then read it the same way a registered repo is read.
        full_name = (
            body.github.strip()
            .replace("https://github.com/", "")
            .replace("http://github.com/", "")
            .rstrip("/")
            .removesuffix(".git")
        )
        try:
            from compass.common.github import clone_repo

            ws = await clone_repo(full_name)
            repo_workspace = ws.to_dict()["id"]
        except Exception as err:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"could not clone {full_name}: {err}. Clone it under Code, "
                "then point at the workspace instead.",
            )
        origin_bits.append(full_name)

    if repo_workspace:
        from compass.common.workspaces import get_workspace_registry

        root = await get_workspace_registry().resolve_root(repo_workspace)
        text, names = _read_repo_styles(root, repo_path)
        sources.append("Stylesheets and tokens from the codebase:\n" + text)
        origin_bits.append(f"{repo_workspace}/{repo_path}".rstrip("/") + f" ({len(names)} files)")

    for f in body.files[:40]:
        name = str(f.get("name", "file"))
        text = str(f.get("text", ""))[:20_000]
        if text.strip():
            sources.append(f"/* {name} */\n{text}")
    if body.files:
        origin_bits.append(f"{len(body.files)} uploaded files")

    if body.notes.strip():
        sources.append("Notes from the team:\n" + body.notes.strip())

    if not sources and not body.images:
        raise HTTPException(status_code=400, detail="nothing to build a system from")

    from compass.common.gateway.azure_client import get_model_client

    try:
        notes = (
            await get_model_client().complete_utility(
                EXTRACT_PROMPT,
                "\n\n".join(sources)[:60_000] or "Read the attached images.",
                max_tokens=8_000,
                prefer_main=True,
                images=body.images[:6],
            )
        ).strip()
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"could not read that: {err}")

    read_name, fonts, swatches = parse_extract(notes)
    if body.images:
        origin_bits.append(f"{len(body.images)} images")

    return await get_system_store().create(
        name=body.name.strip() or read_name,
        source="set up",
        notes=notes,
        css=body.css,
        fonts=fonts,
        swatches=swatches,
        origin=" · ".join(origin_bits),
    )


@router.post("/v1/design/systems/{system_id}/duplicate")
async def design_system_duplicate(system_id: str, user: str = Depends(require_user)) -> dict:
    from compass.design.systems import get_system_store

    store = get_system_store()
    system = await store.get(system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="no such design system")
    return await store.duplicate(system)


@router.get("/v1/design/systems/{system_id}/doc")
async def design_system_doc(system_id: str, user: str = Depends(require_user)) -> dict:
    """The system as a browsable project: its pages, its parameters, and the
    files a developer would receive."""
    from compass.design import docs as design_docs
    from compass.design.systems import get_system_store

    system = await get_system_store().get(system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="no such design system")
    return {
        "system": {k: v for k, v in system.items() if k != "notes"},
        "name": system.get("name"),
        "notes": system.get("notes", ""),
        "sections": design_docs.tree(system),
        "params": design_docs.theme(system),
        "swatches": design_docs._ramp(system),
        "usage": system.get("usage") or {},
    }


@router.get("/v1/design/systems/{system_id}/page/{section_id}")
async def design_system_page(
    system_id: str, section_id: str, user: str = Depends(require_user)
) -> Response:
    """One section, as a standalone document — what the preview frames render."""
    from compass.design import docs as design_docs
    from compass.design.systems import get_system_store

    system = await get_system_store().get(system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="no such design system")
    try:
        html = design_docs.page_html(system, section_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such page")
    return Response(content=html, media_type="text/html")


@router.get("/v1/design/systems/{system_id}/file")
async def design_system_file(
    system_id: str, path: str = "styles.css", user: str = Depends(require_user)
) -> Response:
    """A raw file from the system — the token sheet, the guide, the record."""
    from compass.design import docs as design_docs
    from compass.design.systems import get_system_store

    system = await get_system_store().get(system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="no such design system")
    files = {
        "styles.css": (design_docs.styles_css, "text/css"),
        "readme.md": (design_docs.readme_md, "text/markdown"),
        "theme.json": (design_docs.theme_json, "application/json"),
    }
    if path not in files:
        raise HTTPException(status_code=404, detail="no such file")
    build, media = files[path]
    return Response(content=build(system), media_type=media)


@router.get("/v1/design/systems/{system_id}/export")
async def design_system_export(system_id: str, user: str = Depends(require_user)) -> Response:
    from compass.design import docs as design_docs
    from compass.design.systems import get_system_store

    system = await get_system_store().get(system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="no such design system")
    stem = "".join(
        c for c in system.get("name", "design-system") if c.isalnum() or c in " -_"
    ).strip() or "design-system"
    return Response(
        content=design_docs.system_zip(system),
        media_type="application/zip",
        headers={"content-disposition": f'attachment; filename="{stem}.zip"'},
    )


class SystemUsage(BaseModel):
    section: str
    note: str


@router.post("/v1/design/systems/{system_id}/usage")
async def design_system_usage(
    system_id: str, body: SystemUsage, user: str = Depends(require_user)
) -> dict:
    """Usage notes a team adds to a section. Only a system of the user's own can
    carry them — the included ones are read-only by design."""
    from compass.design.systems import get_system_store

    store = get_system_store()
    rows = store._read()
    for r in rows:
        if r.get("id") == system_id:
            usage = dict(r.get("usage") or {})
            if body.note.strip():
                usage[body.section] = body.note.strip()
            else:
                usage.pop(body.section, None)
            r["usage"] = usage
            r["updated_at"] = time.time()
            store._write(rows)
            return {"usage": usage}
    raise HTTPException(
        status_code=404, detail="usage notes can only be added to your own systems"
    )


@router.delete("/v1/design/systems/{system_id}")
async def design_system_delete(system_id: str, user: str = Depends(require_user)) -> dict:
    from compass.design.systems import get_system_store

    return {"deleted": await get_system_store().delete(system_id)}


class DesignHtml(BaseModel):
    html: str
    label: str = "Edited on canvas"


@router.post("/v1/design/projects/{project_id}/html")
async def design_save_html(
    project_id: str, body: DesignHtml, user: str = Depends(require_user)
) -> dict:
    """Store a design edited directly on the canvas, keeping the old one as a
    version. Separate from PATCH so canvas edits always enter history."""
    from compass.design.store import get_design_store

    p = await get_design_store().save_html(project_id, body.html, label=body.label)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


@router.post("/v1/design/projects/{project_id}/open")
async def design_open(project_id: str, user: str = Depends(require_user)) -> dict:
    """Mark the project as viewed — backs the table's Last viewed column."""
    from compass.design.store import get_design_store

    p = await get_design_store().touch(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


@router.post("/v1/design/projects/{project_id}/duplicate")
async def design_duplicate(project_id: str, user: str = Depends(require_user)) -> dict:
    from compass.design.store import get_design_store

    p = await get_design_store().duplicate(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


class PageCreate(BaseModel):
    name: str = ""


@router.get("/v1/design/projects/{project_id}/pages")
async def design_pages(project_id: str, user: str = Depends(require_user)) -> dict:
    from compass.design.store import get_design_store

    store = get_design_store()
    project = await store.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such design project")
    pages = await store.pages(project_id)
    return {"pages": pages, "active": project.get("active_page") or (pages[0]["id"] if pages else "")}


@router.post("/v1/design/projects/{project_id}/pages")
async def design_page_add(
    project_id: str, body: PageCreate, user: str = Depends(require_user)
) -> dict:
    from compass.design.store import get_design_store

    p = await get_design_store().add_page(project_id, body.name)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return p


@router.delete("/v1/design/projects/{project_id}/pages/{page_id}")
async def design_page_delete(
    project_id: str, page_id: str, user: str = Depends(require_user)
) -> dict:
    from compass.design.store import get_design_store

    p = await get_design_store().delete_page(project_id, page_id)
    if p is None:
        raise HTTPException(
            status_code=400, detail="a project keeps at least one page"
        )
    return p


@router.post("/v1/design/projects/{project_id}/pages/{page_id}/open")
async def design_page_open(
    project_id: str, page_id: str, user: str = Depends(require_user)
) -> dict:
    from compass.design.store import get_design_store

    p = await get_design_store().open_page(project_id, page_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such page")
    return p


# ---- a project's own files ------------------------------------------------


class ProjectFile(BaseModel):
    path: str
    text: str = ""
    data_url: str = ""


@router.get("/v1/design/projects/{project_id}/files")
async def design_files(
    project_id: str, path: str = "", user: str = Depends(require_user)
) -> dict:
    from compass.design import files as design_files

    try:
        return design_files.listing(project_id, path)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such folder")


@router.get("/v1/design/projects/{project_id}/files/read")
async def design_file_read(
    project_id: str, path: str, user: str = Depends(require_user)
) -> Response:
    from compass.design import files as design_files

    try:
        blob, media = design_files.read_bytes(project_id, path)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such file")
    return Response(content=blob, media_type=media)


@router.post("/v1/design/projects/{project_id}/files")
async def design_file_write(
    project_id: str, body: ProjectFile, user: str = Depends(require_user)
) -> dict:
    from compass.design import files as design_files

    try:
        if not body.text and not body.data_url:
            return design_files.make_folder(project_id, body.path)
        return design_files.write(
            project_id, body.path, text=body.text, data_url=body.data_url
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except IsADirectoryError:
        raise HTTPException(status_code=400, detail="that path is a folder")


@router.delete("/v1/design/projects/{project_id}/files")
async def design_file_delete(
    project_id: str, path: str, user: str = Depends(require_user)
) -> dict:
    from compass.design import files as design_files

    try:
        return {"deleted": design_files.remove(project_id, path)}
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))


@router.get("/v1/design/projects/{project_id}/versions")
async def design_versions(project_id: str, user: str = Depends(require_user)) -> dict:
    """The history list — html omitted, since a version can be tens of kilobytes."""
    from compass.design.store import get_design_store

    p = await get_design_store().get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    return {
        "current": {"label": p.get("version_label") or "Current", "at": p.get("updated_at")},
        "versions": [
            {k: v for k, v in ver.items() if k != "html"} for ver in (p.get("versions") or [])
        ],
    }


@router.post("/v1/design/projects/{project_id}/versions/{version_id}/restore")
async def design_restore(
    project_id: str, version_id: str, user: str = Depends(require_user)
) -> dict:
    """Restore a past version. The design being replaced becomes a version of
    its own, so restoring is itself undoable."""
    from compass.design.store import get_design_store

    store = get_design_store()
    p = await store.get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    version = next((v for v in (p.get("versions") or []) if v.get("id") == version_id), None)
    if version is None:
        raise HTTPException(status_code=404, detail="no such version")
    return await store.save_html(project_id, version["html"], label="Restored") or p


class DesignComment(BaseModel):
    x: float = 0        # position as a fraction of the design's width
    y: float = 0        # ...and of its height, so pins survive a resize
    text: str = ""
    resolved: bool | None = None


@router.post("/v1/design/projects/{project_id}/comments")
async def design_comment_add(
    project_id: str, body: DesignComment, user: str = Depends(require_user)
) -> dict:
    import uuid as _uuid

    from compass.design.store import get_design_store

    store = get_design_store()
    p = await store.get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    comments = list(p.get("comments") or [])
    comments.append(
        {
            "id": _uuid.uuid4().hex[:12],
            "x": body.x,
            "y": body.y,
            "text": body.text,
            "author": user,
            "resolved": False,
            "at": time.time(),
        }
    )
    return await store.update(project_id, comments=comments) or p


@router.delete("/v1/design/projects/{project_id}/comments/{comment_id}")
async def design_comment_delete(
    project_id: str, comment_id: str, user: str = Depends(require_user)
) -> dict:
    from compass.design.store import get_design_store

    store = get_design_store()
    p = await store.get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    kept = [c for c in (p.get("comments") or []) if c.get("id") != comment_id]
    return await store.update(project_id, comments=kept) or p


@router.get("/v1/design/projects/{project_id}/thumbnail")
async def design_thumbnail(project_id: str, user: str = Depends(require_user)) -> Response:
    """A small render of the design, cached on disk until the design changes."""
    from compass.design.store import get_design_store

    p = await get_design_store().get(project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such design project")
    html = p.get("html") or ""
    if not html:
        raise HTTPException(status_code=404, detail="no design yet")

    settings = get_settings()
    cache_dir = settings.workspace_root / settings.data_dir / "design_thumbs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{project_id}-{int(p.get('updated_at', 0))}.png"
    if not cached.is_file():
        from compass.design import export as ex

        try:
            png = await ex.to_thumbnail(html)
        except RuntimeError as err:
            raise HTTPException(status_code=501, detail=str(err))
        except Exception as err:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"thumbnail failed: {err}")
        cached.write_bytes(png)
        for stale in cache_dir.glob(f"{project_id}-*.png"):
            if stale != cached:
                stale.unlink(missing_ok=True)

    return Response(
        content=cached.read_bytes(),
        media_type="image/png",
        headers={"cache-control": "private, max-age=86400"},
    )


@router.get("/v1/design/projects/{project_id}/export")
async def design_export(
    project_id: str, format: str = "html", user: str = Depends(require_user)
) -> Response:
    """Export the design as html | pdf | png | zip | pptx."""
    from compass.design.store import get_design_store
    from compass.design.systems import get_system_store

    project = await get_design_store().get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such design project")
    html = project.get("html") or ""
    if not html:
        raise HTTPException(status_code=409, detail="this project has no design yet")

    stem = "".join(c for c in project.get("name", "design") if c.isalnum() or c in " -_").strip()
    stem = stem or "design"

    def send(body: bytes, media: str, ext: str) -> Response:
        return Response(
            content=body,
            media_type=media,
            headers={"content-disposition": f'attachment; filename="{stem}.{ext}"'},
        )

    if format == "html":
        return send(html.encode(), "text/html", "html")

    from compass.design import export as ex

    if format in ("zip", "archive"):
        system = await get_system_store().get(project.get("design_system") or "")
        notes = (system or {}).get("notes", "")
        if format == "zip":
            return send(
                ex.to_zip(
                    name=project.get("name", "Design"),
                    html=html,
                    prompt=project.get("prompt", ""),
                    system_notes=notes,
                ),
                "application/zip",
                "zip",
            )

        # The whole project: every page, and every file it was given.
        from compass.design import files as design_files

        pages = await get_design_store().pages_with_html(project_id)
        carried: list[tuple[str, bytes]] = []
        try:
            root = design_files.project_root(project_id)
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    carried.append((str(path.relative_to(root)), path.read_bytes()))
        except Exception:  # noqa: BLE001 - a missing folder is not a failure
            carried = []

        return send(
            ex.project_archive(
                name=project.get("name", "Design"),
                prompt=project.get("prompt", ""),
                pages=pages,
                files=carried,
                system_notes=notes,
            ),
            "application/zip",
            "zip",
        )

    try:
        if format == "pdf":
            return send(await ex.to_pdf(html), "application/pdf", "pdf")
        if format == "png":
            return send(await ex.to_png(html), "image/png", "png")
        if format == "pptx":
            return send(
                await ex.to_pptx(html),
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "pptx",
            )
    except RuntimeError as err:  # an optional library is missing on this host
        raise HTTPException(status_code=501, detail=str(err))
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"export failed: {err}")

    raise HTTPException(status_code=400, detail=f"unknown format: {format}")


class DesignGenerate(BaseModel):
    prompt: str
    template: str = ""  # "" = keep the project's own template
    design_system: str = ""
    model: str = ""     # deployment chosen in the composer; "" = the default
    images: list[str] = []  # data: URLs to design from
    # How hard to think before writing. "" = the configured design posture.
    effort: str = ""


class DesignElement(BaseModel):
    html: str                 # the element as it stands
    instruction: str          # what to change about it
    label: str = ""           # what it is, e.g. "span.delta"
    path: str = ""            # where it sits, so it can be photographed
    model: str = ""


@router.post("/v1/design/projects/{project_id}/element")
async def design_element(
    project_id: str, body: DesignElement, user: str = Depends(require_user)
) -> dict:
    """Rewrite one element to order — Factory calls this design mode: point at
    the part that needs attention, say what to change, and the change lands
    there rather than anywhere else in the document."""
    from compass.design.store import get_design_store

    project = await get_design_store().get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such design project")
    fragment = (body.html or "").strip()
    if not fragment:
        raise HTTPException(status_code=400, detail="nothing selected")
    if not body.instruction.strip():
        raise HTTPException(status_code=400, detail="say what to change")

    # The element arrives without its stylesheet, so hand over the palette it
    # can legitimately name; otherwise it invents var(--success) and gets black.
    import re as _pre

    tokens = ""
    if root := _pre.search(r":root\s*{([^}]*)}", project.get("html") or "", _pre.S):
        names = _pre.findall(r"(--[\w-]+)\s*:\s*([^;]+);", root.group(1))
        if names:
            tokens = "\n\nThe design defines these, and only these:\n" + "\n".join(
                f"  {k}: {v.strip()}" for k, v in names[:40]
            )

    system = (
        "You are editing ONE element of a finished design, in place.\n"
        "Reply with that element's replacement markup and nothing else: no "
        "commentary, no code fence, no surrounding document. Exactly one root "
        "element, the same kind as the one you were given unless the "
        "instruction says otherwise.\n"
        "Keep every class and id it already has — the document's stylesheet is "
        "written against them and you cannot see it. Style anything new with an "
        "inline style attribute, using the custom properties the design already "
        "defines — the list follows — rather than inventing property names or "
        "fresh colours. If the colour you want is not among them, write the "
        "literal value.\n"
        "Change what was asked and leave the rest of the element alone."
    )
    asked = (
        f"The element{(' — ' + body.label) if body.label else ''}:\n\n"
        f"{fragment[:20_000]}\n\n"
        f"Change it so that: {body.instruction.strip()}"
        f"{tokens}"
    )

    # A picture of the element where it lives. "This is cramped" and "the
    # colour is wrong" are about how it looks, and the markup does not show it.
    shot = ""
    if body.path:
        from compass.design import export as _ex

        shot = await _ex.element_shot(project.get("html") or "", body.path)
    if shot:
        system += (
            "\nYou are also shown a photograph of the element as it renders, "
            "with a little of what surrounds it. Judge spacing, size, colour "
            "and alignment from the picture, and the structure from the markup."
        )

    try:
        from compass.common.gateway.azure_client import get_model_client

        out = await get_model_client().complete_utility(
            system,
            asked,
            max_tokens=8_000,
            prefer_main=True,
            model=body.model,
            images=[shot] if shot else None,
        )
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"that edit failed: {err}")

    import re as _re

    new_html = (out or "").strip()
    if "```" in new_html:
        m = _re.search(r"```(?:html)?\s*\n(.*?)```", new_html, _re.S)
        if m:
            new_html = m.group(1).strip()
    if not new_html.startswith("<"):
        raise HTTPException(status_code=502, detail="the edit came back unusable")
    return {"html": new_html, "saw": bool(shot)}


# A picture the model was shown, referred to by a name it can type.
_IMAGE_MARK = "compass-image:"
_EMBEDDED = r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]{40,}"


def _fold_images(html: str, slots: list[str]) -> str:
    """Swap embedded pictures for short markers, remembering each one.

    A design that already carries a photograph carries fifty kilobytes of
    base64 with it, and the existing document is handed back to the model on
    every refinement. Folding them keeps that budget for the design.
    """
    import re as _re

    def take(m: "_re.Match[str]") -> str:
        url = m.group(0)
        if url not in slots:
            slots.append(url)
        return f"{_IMAGE_MARK}{slots.index(url) + 1}"

    return _re.sub(_EMBEDDED, take, html)


def _place_images(html: str, slots: list[str]) -> str:
    """Put the real pictures back where the markers are."""
    import re as _re

    def put(m: "_re.Match[str]") -> str:
        i = int(m.group(1)) - 1
        if 0 <= i < len(slots):
            return slots[i]
        return slots[0] if slots else ""

    html = _re.sub(_IMAGE_MARK + r"(\d+)", put, html)
    # It may write the shape of a data URI with no bytes in it anyway. That is
    # always a broken image, so point it at the picture it was given.
    if slots:
        html = _re.sub(
            r"data:image/[A-Za-z0-9.+-]+;base64,(?=[\"'\s>])", slots[0], html
        )
    return html


def _image_note(seen: int, folded: int) -> str:
    """What to tell the model about the pictures it has."""
    lines = []
    if seen:
        lines.append(
            f"You have been given {seen} image"
            f"{'s' if seen > 1 else ''} and you can see "
            f"{'them' if seen > 1 else 'it'}. To place one in the design, write "
            f'src="{_IMAGE_MARK}1" on an ordinary <img> — the number of the image '
            "you mean — with alt text and an explicit width and height. The real "
            "picture is put in after you reply.\n"
            "Do not write base64 yourself. You cannot reproduce the bytes, and "
            'src="data:image/png;base64," with nothing after the comma renders as '
            "a broken-image icon. Do not invent a file path or a URL for it "
            "either, and do not draw a placeholder box where the picture goes."
        )
    if folded:
        lines.append(
            f"Pictures already in this design appear as {_IMAGE_MARK}N markers. "
            "Leave them exactly as they are unless you are asked to change them."
        )
    return "\n".join(lines)


async def _keep_images(project_id: str, images: list[str]) -> None:
    """Put what was attached into the project's own folder, so it survives.

    An image passed to one generation and thrown away can never be used by the
    next one, nor exported with the project.
    """
    from compass.design import files as design_files

    kinds = {"jpeg": "jpg", "svg+xml": "svg"}
    for n, url in enumerate(images, 1):
        head, _, _ = url.partition(",")
        mime = head[len("data:image/"):].split(";")[0].lower() if "image/" in head else "png"
        try:
            design_files.write(
                project_id, f"uploads/attachment-{n}.{kinds.get(mime, mime or 'png')}",
                data_url=url,
            )
        except Exception:  # noqa: BLE001 - keeping it is a courtesy, not a gate
            pass


REPAIR_PROMPT = (
    "You are correcting faults found in a finished design by an automated "
    "review. The review measured the rendered page, so each finding is a "
    "fact about it, not an opinion.\n"
    "Return the WHOLE corrected document in one ```html block and nothing "
    "else — no commentary, no explanation.\n"
    "Fix every finding. Change nothing else: same content, same words, same "
    "structure, same palette, same behaviour. A fix that redesigns the page "
    "is a worse outcome than the fault it cures.\n"
    "How these are fixed:\n"
    "- A title that is not a heading becomes one — h1 for the screen's title, "
    "h2 for a panel's — keeping its classes and its type size.\n"
    "- Numbers set in the sans move to the mono face with tabular figures.\n"
    "- A page drawn in one hue keeps its first measure's colour and gives the "
    "others theirs, by meaning, from the palette already declared on :root.\n"
    "- Contrast below the floor is fixed by darkening the text, never by "
    "lightening the ground under it.\n"
    "- A hit area under 24px grows to at least 24, by padding.\n"
    "- An icon blown up past 64px gets width and height attributes and a "
    "CSS width, and its flex parent gets flex: none.\n"
    "- Sideways scroll is cured at the element that overflows — a fixed table "
    "layout, a min-width: 0 on a grid or flex child — never by hiding it.\n"
    "- A script that threw is rewritten until it parses. One syntax error "
    "takes down every control in the prototype at once, so read the whole "
    "script through, keep what it was trying to do, and write it plainly: no "
    "clever one-liners, no template literals nested inside template literals, "
    "and never document.write.\n"
    "- A design built beside its design system is rebuilt from it: declare the "
    "system\u2019s custom properties on :root with the values it gives, then "
    "refer to them by var() everywhere instead of writing a colour or a face "
    "in by hand. Replace an off-palette colour with the nearest one the system "
    "names rather than inventing a token for it, and set the face on anything "
    "that does not inherit one \u2014 a button and an input take the "
    "browser\u2019s default, not the page\u2019s.\n"
    "- A sheet that runs past its page is cut, not scaled: take out the "
    "weakest block, shorten the copy, tighten the spacing. Never shrink the "
    "headline to make room — the headline is the reason the sheet works.\n"
    "- A printed piece built out of bordered cards is regrouped with space "
    "and a hairline rule, keeping the words and dropping the boxes.\n"
    "- Prose, a date or a time set in a monospace moves to the sans; the mono "
    "stays only on a code, an id or a URL.\n"
    "- Navigation that does not switch screens is wired properly: each nav "
    "item names a screen, the handler hides every screen and shows that one "
    "and marks the item current. Prove it to yourself by reading the ids in "
    "the markup against the ids the script looks for — a handler that "
    "queries an id nothing has is the usual cause."
)


def _kinds(findings: list[str]) -> set[str]:
    """What sort of fault each finding is, ignoring its particulars.

    Two contrast findings are the same kind of problem however the ratio and
    the screens differ, and that is the comparison worth making when deciding
    whether a repair left the design better than it found it.

    Every check the review can emit needs a mark here. One that has none falls
    back to its own wording, and a repair that takes four dead controls down to
    two then reads as a brand-new kind of fault and is thrown away — which is
    exactly what happened to the Meridian stylesheet.
    """
    import re as _re

    marks = (
        ("script", "script threw"),
        ("nav", "does not switch screens"),
        ("contrast", "contrast"),
        ("hit-area", "hit area"),
        ("touch-target", "under 44x44"),
        ("focus", "when the keyboard reaches"),
        ("spacing", "off the 4px scale"),
        ("icon", "icon"),
        ("headings", "heading element"),
        ("mono-numbers", "set in the sans"),
        ("hue", "same colour"),
        ("chrome", "strong colour covers"),
        ("sidebar-foot", "short of its foot"),
        ("empty-table", "no rows"),
        ("thin-screen", "barely anything on it"),
        ("scroll", "sideways scroll"),
        ("tweaks", "change nothing"),
        # a design system
        ("system-tokens", "are never declared"),
        ("system-palette", "not in"),
        ("system-face", "which is not"),
        # a printed sheet
        ("page-overflow", "past the bottom"),
        ("mono-prose", "set in the monospace"),
        ("tiny-type", "too small to read on paper"),
        ("headline", "the body text"),
        ("headline-place", "below the top third"),
        ("panels", "bordered panels"),
        ("wordy", "words on a poster"),
        # a document
        ("reading-size", "not a reading size"),
        ("leading", "a line height of"),
        ("measure", "characters a line"),
        ("hierarchy", "hierarchy is flat"),
        ("sub-heading", "not smaller than the sections"),
        ("heading-size", "to read as headings"),
        ("no-sections", "no section headings"),
        ("caption", "no caption"),
        ("page-spill", "crosses the edge"),
        ("cells", "no space between them"),
    )
    out: set[str] = set()
    for f in findings:
        for kind, mark in marks:
            if mark in f:
                out.add(kind)
                break
        else:
            # Unmarked: use the wording, with the counts taken out, so "4 of
            # these" and "2 of these" are one complaint rather than two.
            out.add(_re.sub(r"\d+", "#", f)[:40])
    return out


# Faults a rule can cure, as opposed to ones that need the markup changed.
_STYLE_FIXABLE = (
    "which is not",           # ...'s face
    "hit area",
    "under 44x44",
    "focus state",
    "off the 4px scale",
    "coloured area is not",
    "below the 4.5:1 floor",
    "icon(s) blown up",
    "sideways scroll",
    "no space between them",
    "change nothing",         # a tweak knob wired to nothing
    "are never declared",     # a design system's tokens
)

CSS_REPAIR_PROMPT = (
    "You are correcting faults in a finished design by adding CSS to it. The "
    "faults were measured on the rendered page, so each one is a fact.\n"
    "Reply with ONE ```css block and nothing else: the rules that put them "
    "right, appended to the end of the document\u2019s stylesheet. No markup, "
    "no commentary, no @import.\n"
    "Write the narrowest rules that will do it. You may use !important where a "
    "later rule has to win. Change nothing that was not named.\n"
    "How these are cured:\n"
    "- A face that is not the system\u2019s: set font-family on the elements "
    "that do not inherit one \u2014 button, input, select and textarea all "
    "take the browser\u2019s default \u2014 using the system\u2019s own "
    "custom property where it declares one.\n"
    "- A colour off the system\u2019s palette: map it to the nearest colour "
    "the system names, by var().\n"
    "- Tokens the design never declares: declare them on :root with the "
    "system\u2019s values, and make the design read them.\n"
    "- A tweak control that changes nothing: make the design read the custom "
    "property it sets, so every value it offers is visible.\n"
    "- A hit area under its floor: add padding, or a min-width and "
    "min-height, without moving what is around it.\n"
    "- A checkbox or a radio under the floor: those default to about 13px and "
    "ignore padding, so give the control an explicit width and height, or put "
    "the padding on its label and let that be what a finger hits. Do not "
    "exempt them \u2014 they are the control most often missed.\n"
    "- No focus state: give :focus-visible a visible outline with an offset.\n"
    "- Spacing off the scale: round the offending values to the nearest 4.\n"
    "- Contrast below the floor: darken the text, never lighten the ground.\n"
    "- Cells whose words run together: give the table cells horizontal "
    "padding."
)


def _weight(findings: list[str]) -> tuple[int, int]:
    """How much is wrong: how many kinds of fault, and how many things in them.

    Counting findings alone cannot see a repair that takes 27 controls under
    the touch floor down to 9 — that is still one finding, so a guard watching
    the count refuses a fix that plainly worked. Most findings open with the
    number of things they are about; that number is the rest of the answer.
    """
    import re as _re

    things = 0
    for f in findings:
        m = _re.match(r"\s*(\d+)", f)
        things += int(m.group(1)) if m else 1
    return len(_kinds(findings)), things


async def _repair_css(
    html: str, issues: list[str], model: str, kind: str, system: dict | None,
    prior: "ReasoningTrace | None" = None,
) -> tuple[str, list[str], list[str], "ReasoningTrace | None"]:
    """Cure a style fault with a stylesheet rather than a rewrite.

    Asking for a whole prototype back to change a font-family is a hundred and
    forty kilobytes reproduced from memory. A handful of appended rules is a
    small enough ask to come back right, and it cannot truncate the document.
    """
    from compass.design import export as _ex

    asked = "The review found:\n- " + "\n- ".join(issues)
    if system:
        asked += (
            "\n\nThe design system in force names these colours: "
            + ", ".join(system.get("colours", [])[:10])
            + "; these faces: " + ", ".join(system.get("faces", [])[:4])
            + "; and these custom properties: "
            + ", ".join(system.get("names", [])[:24])
        )
    asked += "\n\nThe document:\n\n```html\n" + html[:120_000] + "\n```"

    try:
        from compass.common.gateway.azure_client import get_model_client

        # The reply is a few rules, but the thinking that finds them is billed
        # against the same budget: measured at 8,128 reasoning tokens on a
        # 134KB prototype, which a cap of 8,000 truncated into an empty string.
        # A degraded repair is a repair made without deliberation. It is not
        # reported separately: the generation's own notice already tells the
        # reader this document did not get the good path.
        out, thought, _settled = await _think_through(
            CSS_REPAIR_PROMPT, asked, max_tokens=32_000, model=model, prior=prior
        )
    except Exception:  # noqa: BLE001
        return html, [], issues, prior

    import re as _re

    css = (out or "").strip()
    if "```" in css:
        if m := _re.search(r"```(?:css)?\s*\n?(.*?)```", css, _re.S):
            css = m.group(1).strip()
    if not css or css.lstrip().startswith("<") or len(css) > 24_000:
        return html, [], issues, prior

    block = "<style data-dz-fix>\n" + css + "\n</style>"
    if "</head>" in html:
        fixed = html.replace("</head>", block + "\n</head>", 1)
    elif "</body>" in html:
        fixed = html.replace("</body>", block + "\n</body>", 1)
    else:
        fixed = html + block

    try:
        after = await _ex.audit(fixed, kind=kind, system=system)
    except Exception:  # noqa: BLE001
        return html, [], issues, prior

    if _kinds(after) - _kinds(issues):        # a fault of a new kind appeared
        return html, [], issues, prior
    if _weight(after) >= _weight(issues):     # or nothing is less wrong
        return html, [], issues, prior
    cured = [i for i in issues if _kinds([i]) - _kinds(after)]
    return fixed, cured, after, thought


async def _repair(
    html: str, issues: list[str], model: str, kind: str = "",
    system: dict | None = None, prior: "ReasoningTrace | None" = None,
) -> tuple[str, list[str], list[str], "ReasoningTrace | None"]:
    """One corrective pass: the document to keep, what it cured, what is left.

    A repair that trades one fault for another is not a repair, so the
    original is kept unless the review comes back with nothing new in it.
    """
    from compass.design import export as _ex

    try:
        from compass.common.gateway.azure_client import get_model_client

        out, thought, _settled = await _think_through(
            REPAIR_PROMPT,
            "The review found:\n- " + "\n- ".join(issues)
            + "\n\nThe document:\n\n```html\n" + html + "\n```",
            max_tokens=64_000,
            model=model,
            prior=prior,
        )
    except Exception:  # noqa: BLE001 - a failed repair is not a failed design
        return html, [], issues, prior

    import re as _re

    fixed = (out or "").strip()
    if "```" in fixed:
        if m := _re.search(r"```(?:html)?\s*\n(.*?)```", fixed, _re.S):
            fixed = m.group(1).strip()
    # It has to still be a document, and not a stub of one.
    if not fixed.lower().startswith("<!doctype") and not fixed.lower().startswith("<html"):
        return html, [], issues, prior
    if len(fixed) < len(html) * 0.6:
        return html, [], issues, prior

    try:
        after = await _ex.audit(fixed, kind=kind, system=system)
    except Exception:  # noqa: BLE001
        return html, [], issues, prior

    # Judging the repair by how many findings are left throws away good work:
    # moving one colour from 4.09:1 to 4.46:1 across six screens is progress,
    # and it leaves the count untouched. Compare what KIND of fault is left,
    # and let a fault that merely changed shape count as movement.
    before_kinds, after_kinds = _kinds(issues), _kinds(after)
    if after_kinds - before_kinds:          # a fault of a new kind appeared
        return html, [], issues, prior
    if _weight(after) >= _weight(issues):   # or nothing is less wrong
        return html, [], issues, prior
    if after == issues:                     # or nothing moved at all
        return html, [], issues, prior

    # Cured means the kind of fault is gone. A fault that survived in a milder
    # form is not cured — it comes back in the remainder, in its new words.
    cured = [i for i in issues if _kinds([i]) - after_kinds]
    return fixed, cured, after, thought


@router.post("/v1/design/projects/{project_id}/generate")
async def design_generate(
    project_id: str, body: DesignGenerate, user: str = Depends(require_user)
) -> dict:
    """Generate (or refine) the project's design and store the HTML."""
    from compass.design.skills import DESIGN_SYSTEM_PROMPT, TEMPLATE_PROMPTS
    from compass.design.store import get_design_store
    from compass.design.systems import get_system_store, system_prompt_block

    store = get_design_store()
    project = await store.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such design project")

    template = body.template or project.get("template") or "blank"
    parts = [TEMPLATE_PROMPTS.get(template, "")]

    ids = (
        [body.design_system] if body.design_system
        else project.get("design_systems") or
        ([project["design_system"]] if project.get("design_system") else [])
    )
    attached: list[dict] = []
    if ids:
        store_s = get_system_store()
        attached = [x for x in [await store_s.get(i) for i in ids] if x]
        parts.append(system_prompt_block(*attached))
    # Pictures: the ones attached to this turn, then any already embedded in
    # the design, all referred to by marker rather than by their bytes.
    slots: list[str] = [i for i in body.images if i.startswith("data:image/")]
    seen = len(slots)
    if seen:
        await _keep_images(project_id, slots)

    existing = project.get("html") or ""
    if existing:
        before = len(slots)
        existing = _fold_images(existing, slots)
        parts.append(
            "Refine the EXISTING design below; keep everything not mentioned "
            "unchanged.\n\n```html\n" + existing[:60_000] + "\n```"
        )
        folded = len(slots) - before
    else:
        folded = 0

    if note := _image_note(seen, folded):
        parts.append(note)
    parts.append("Request: " + body.prompt)

    try:
        from compass.common.gateway.azure_client import get_model_client

        # A full design needs a large budget: on reasoning models the thinking
        # is billed against the same cap, so a small one returns nothing — and
        # a prototype of eight working screens is a lot of document.
        out, made_it, settled_for = await _think_through(
            DESIGN_SYSTEM_PROMPT,
            "\n\n".join(p for p in parts if p),
            max_tokens=64_000,
            model=body.model,
            images=body.images,
            effort=body.effort,
        )
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"design generation failed: {err}")

    import re

    raw_out = out.strip()
    # The direction it took and the note it leaves are written around the
    # block, so the conversation can say them instead of "here it is".
    direction = ""
    notes = ""
    if m := re.search(r"^\s*DIRECTION:\s*(.+)$", raw_out, re.M):
        direction = m.group(1).strip()
    if m := re.search(r"^\s*NOTES:\s*(.+)$", raw_out, re.M):
        notes = m.group(1).strip()

    html = raw_out
    if "```" in html:  # pull the html out of the fenced block
        m = re.search(r"```(?:html)?\s*\n(.*?)```", html, re.S)
        if m:
            html = m.group(1).strip()
    html = _place_images(html, slots)
    if not html:
        raise HTTPException(
            status_code=502,
            detail=(
                "the model ran out of room before it finished the design — "
                "ask for fewer screens, or split it across two requests"
            ),
        )

    # The seed prompt is the project's identity — refinements are appended to
    # the transcript instead, so reopening a project replays the conversation.
    turns = list(project.get("turns") or [])
    turns.append({"role": "user", "text": body.prompt})
    # "Checking the design for issues" is what the card says while this runs;
    # this is that check, actually run — contrast, hit areas, sideways scroll,
    # tables with no rows, a script that threw.
    from compass.design import export as _ex

    # The review's criteria depend on what was asked for: a flier judged by a
    # dashboard's rules passes while looking nothing like a flier.
    kind = body.template or project.get("template") or ""
    # What the attached systems demand, as things a browser can be asked
    # about. Applies to every template: a brand does not stop applying
    # because the thing being built is a flier rather than a dashboard.
    wanted = _ex.system_expectations(*attached) if attached else None
    try:
        issues = await _ex.audit(html, kind=kind, system=wanted)
    except Exception:  # noqa: BLE001 - never fail a design over its review
        issues = []

    steps = ["Reading the brief", "Refining design" if project.get("html") else "Designing"]

    # Finding a fault and reporting it leaves the fault in the design. Correct
    # what was found, once, and say what was corrected.
    cured: list[str] = []
    # The reasoning from the generation carries into the first correction, and
    # from each correction into the next: three rounds of a model rediscovering
    # the same document is three times the thinking for the same work.
    thought: ReasoningTrace | None = made_it
    if issues:
        steps.append("Found issues — fixing")
        # Corrections, plural: keep going while each pass leaves the design
        # less wrong, and stop the moment one does not move it.
        stalled = 0
        for _ in range(3):
            was = html
            # A style fault wants a stylesheet, not the document written again.
            if all(any(m in i for m in _STYLE_FIXABLE) for i in issues):
                html, got, issues, thought = await _repair_css(
                    html, issues, body.model, kind, wanted, thought
                )
            else:
                html, got, issues, thought = await _repair(
                    html, issues, body.model, kind, wanted, thought
                )
            cured += got
            if not issues:
                break
            # A round that changes nothing is usually the model varying rather
            # than the fault being incurable — the same finding has cured on a
            # second attempt. Give it one, then stop.
            stalled = stalled + 1 if html == was else 0
            if stalled >= 2:
                break

    # A document is laid out over its pages by measuring, which no amount of
    # asking can do: the model has to decide where page three ends before it
    # knows how tall anything renders. This does not need it to.
    if kind in _ex._PAGED_KINDS:
        html, flow = await _ex.reflow_pages(html)
        if flow.get("moved"):
            steps.append("Laying out the pages")
            try:
                issues = await _ex.audit(html, kind=kind, system=wanted)
            except Exception:  # noqa: BLE001
                pass

    said = " ".join(x for x in (direction, notes) if x)
    # First, because it changes how everything after it should be read.
    if settled_for:
        said = (settled_for + " " + said).strip()
    if cured:
        said = (said + " ").lstrip() + "Found and fixed: " + "; ".join(cured) + "."
    if issues:
        said = (said + " ").lstrip() + "Still worth fixing: " + "; ".join(issues) + "."

    turns.append(
        {
            "role": "assistant",
            "text": said or "Here it is — tell me what to change.",
            "degraded": bool(settled_for),
            "steps": steps,
            "file": f"{project.get('name', 'Design')}.html",
        }
    )
    await store.update(
        project_id, turns=turns, prompt=project.get("prompt") or body.prompt
    )
    label = "Refined" if project.get("html") else "First version"
    updated = await store.save_html(project_id, html, label=label)
    return updated or {"id": project_id, "html": html}
