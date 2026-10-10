"""The REST surface for business functions.

Mounted only when the module is enabled, which is why the import in
`api/server.py` sits inside the conditional: a module that is switched off
should not appear in the route table, and the table is compared against a
recorded snapshot, so leakage is caught rather than merely unlikely.

THE SCOPE COMES FROM THE REQUEST, NEVER FROM THE BODY. `entity` and `period`
are query parameters a person picked from two selectors, and `user` is taken
from `require_user` and nothing else. A feature scoped to "the people who
report to you" therefore has no field anybody could widen — not the client,
not a sentence, not a model asked to fill in a filter. That is the whole
reason the scope object is assembled here rather than accepted.

TWO WAYS TO ACT, AND THE DIFFERENCE MATTERS.

  /act    the row buttons. A person clicked a specific row, so the click IS
          the confirmation and there is nothing a plan would add.
  /ask    the rail. The person wrote a sentence and something had to work out
          which rows it meant, so what comes back is a proposal and the rows
          are not touched until /plans/{id}/confirm.

Inferred targets get a confirmation step; pointed-at targets already had one.

Every route is behind `require_user`, like the other 136.
"""

from __future__ import annotations

import asyncio

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from compass.businessfunctions import (
    assistant,
    export,
    ledger,
    notices,
    people,
    registry,
    reports,
)
from compass.businessfunctions.features import rewardlens
from compass.businessfunctions.features.base import Scope
from compass.common.auth import require_user

logger = logging.getLogger("compass.businessfunctions")

router = APIRouter(prefix="/v1/business-functions", tags=["business-functions"])


def _scope(user: str, entity: str, period: str, feature=None) -> Scope:
    """What the person has narrowed to, and what they are.

    `user` is never one of the inputs, and neither is anything resolved from
    it: the roles come from the directory and the breadth from the feature's
    own manifest. A client that could send either could send any.
    """
    return people.scope_for(user, feature, entity=entity, period=period)


def _resolve(function_id: str, feature_id: str):
    """The function, its feature and the handler, or a 404 naming which is missing."""
    fn, feature, handler = registry.feature_of(function_id, feature_id)
    if fn is None:
        raise HTTPException(404, f"No business function called {function_id!r}.")
    if feature is None:
        raise HTTPException(404, f"{fn.name} has no feature called {feature_id!r}.")
    if handler is None:
        # The registry refuses to load a manifest whose handler is missing, so
        # reaching here means the register changed under a running server.
        raise HTTPException(503, f"{feature.name} is not available right now.")
    return fn, feature, handler


def _feature_card(fn_id: str, feature) -> dict:
    """A feature as the switcher and the launcher grid read it."""
    return {
        "id": feature.id,
        "name": feature.name,
        "short": feature.short,
        "blurb": feature.blurb,
        "scope": feature.scope,
        "choices": feature.choices,
        "scope_why": feature.scope_why,
        "actions": [
            {"id": a.id, "label": a.label, "confirm": a.confirm,
             "reversible": a.reversible, "outward": a.outward,
             "note_label": a.note_label}
            for a in feature.actions
        ],
        "forms": [
            {"id": f.id, "label": f.label, "blurb": f.blurb,
             "submit_label": f.submit_label, "confirm": f.confirm,
             "on_row": f.on_row,
             "fields": [
                 {"id": x.id, "label": x.label, "kind": x.kind, "help": x.help,
                  "required": x.required, "options": x.options}
                 for x in f.fields
             ]}
            for f in feature.forms
        ],
    }


def _rail(fn, feature, scope: Scope) -> dict:
    view = assistant.view(fn, feature, scope)
    return {
        "title": view.title,
        "subtitle": view.subtitle,
        "scope_label": view.scope_label,
        "starters": view.starters,
        "can_act": view.can_act,
    }


@router.get("")
async def list_functions(user: str = Depends(require_user)) -> dict:
    """Every business function, for the switcher.

    Deliberately thin: the switcher shows a name, a subtitle and a count, and
    loading each function's figures to render a menu would be work nobody
    asked for.
    """
    return {
        "functions": [
            {
                "id": fn.id,
                "name": fn.name,
                "subtitle": fn.subtitle,
                "features": len(fn.features),
                "empty": fn.empty,
            }
            for fn in registry.all_functions()
        ]
    }


@router.get("/{function_id}")
async def get_function(
    function_id: str,
    entity: str = Query("", description="Legal entity, where a feature is scoped by one"),
    period: str = Query("", description="Reporting period, same"),
    user: str = Depends(require_user),
) -> dict:
    """One function's overview: what it is, what it holds, and what needs you.

    The queue is computed from each feature's own figures rather than stored,
    so it cannot say three mismatches while the table shows none. A feature
    that cannot answer is skipped rather than failing the page — an overview
    that will not render because one feature is unhappy is worse than an
    overview missing a line.
    """
    await _ready()
    fn = registry.get(function_id)
    if fn is None:
        raise HTTPException(404, f"No business function called {function_id!r}.")

    queue: list[dict] = []
    for feature in fn.features:
        handler = registry.feature_of(fn.id, feature.id)[2]
        if handler is None:
            continue
        # A scope PER FEATURE, because the breadth is a property of the
        # screen and not of the function. Built once for the whole overview,
        # this quietly answered every feature at firm breadth — so an
        # employee's "what needs you" counted the firm's unacknowledged
        # items, which is the figure the register itself refuses to show
        # them. Two lines up, one hole.
        scope = _scope(user, entity, period, feature)
        try:
            needs = [f for f in handler.figures(scope) if f.tone == "warn"]
        except Exception:  # noqa: BLE001 — one unhappy feature is not the page
            logger.warning("overview: %s/%s could not report",
                           fn.id, feature.id, exc_info=True)
            continue
        for figure in needs:
            queue.append({
                "feature": feature.id,
                "feature_name": feature.name,
                "count": figure.value,
                "what": figure.caption,
                "key": figure.key,
            })

    return {
        "id": fn.id,
        "name": fn.name,
        "subtitle": fn.subtitle,
        "blurb": fn.blurb,
        "empty": fn.empty,
        "features": [_feature_card(fn.id, f) for f in fn.features],
        "queue": queue,
        "rail": _rail(fn, None, _scope(user, entity, period)),
    }


@router.get("/{function_id}/features/{feature_id}")
async def get_feature(
    function_id: str,
    feature_id: str,
    entity: str = Query(""),
    period: str = Query(""),
    tab: str = Query("", description="Which view; the first when not given"),
    user: str = Depends(require_user),
) -> dict:
    """The workbench: the figures, the tabs, the rows and the rules in force.

    `rules` is only the ones that apply right now. The manifest holds more,
    and sending all of them would put the UI in the position of deciding which
    are true — which is the handler's job and depends on data the UI does not
    have.
    """
    await _ready()
    fn, feature, handler = _resolve(function_id, feature_id)
    scope = _scope(user, entity, period, feature)

    if missing := scope.missing(feature.scope):
        # Not an error. The person has not chosen an entity yet, and the UI
        # needs to know which selector to wait on.
        return {
            "id": feature.id,
            "name": feature.name,
            "needs_scope": missing,
            "scope_why": feature.scope_why,
            "rail": _rail(fn, feature, scope),
        }

    tabs = handler.tabs(scope)
    chosen = tab or (tabs[0].key if tabs else "")
    applies = set(handler.rules(scope))

    return {
        "id": feature.id,
        "name": feature.name,
        "short": feature.short,
        "scope": {"entity": scope.entity, "period": scope.period},
        "figures": [
            {"key": f.key, "value": f.value, "caption": f.caption, "tone": f.tone}
            for f in handler.figures(scope)
        ],
        # Which field identifies a row on the tab being shown, so the UI does
        # not have to guess. A tab may hold a different kind of thing from the
        # rest of the feature and then it says so.
        "row_key": next((t.key_field for t in tabs
                         if t.key == chosen and t.key_field), handler.row_key),
        "tabs": [{"key": t.key, "label": t.label, "count": t.count} for t in tabs],
        "tab": chosen,
        "rows": handler.rows(scope, chosen),
        "rules": [
            {"id": r.id, "headline": r.headline, "detail": r.detail, "tone": r.tone}
            for r in feature.rules if r.id in applies
        ],
        "actions": _feature_card(fn.id, feature)["actions"],
        "forms": _feature_card(fn.id, feature)["forms"],
        "rail": _rail(fn, feature, scope),
    }


@router.get("/{function_id}/features/{feature_id}/export.xlsx")
async def export_feature(
    function_id: str,
    feature_id: str,
    entity: str = Query(""),
    period: str = Query(""),
    user: str = Depends(require_user),
) -> Response:
    """This screen, as a workbook.

    Deliberately the same resolution, the same scope and the same handler
    the screen uses, so there is no second path to the data: a feature that
    shows somebody nothing returns them an empty workbook, because this
    calls the methods that decided that. An export endpoint with its own
    query is how "the UI hides it and the API does not" happens.

    Building a workbook is CPU work, so it runs in a thread rather than
    holding the loop while a few hundred rows are formatted.
    """
    await _ready()
    fn, feature, handler = _resolve(function_id, feature_id)
    scope = _scope(user, entity, period, feature)
    if missing := scope.missing(feature.scope):
        # The screen would be asking for a selector; a spreadsheet of
        # nothing-in-particular is worse than being told to choose.
        raise HTTPException(
            422, f"Choose {' and '.join(missing)} before exporting {feature.name}.")
    try:
        data = await asyncio.to_thread(
            export.to_excel, fn, feature, handler, scope, asked_by=user)
    except ImportError as exc:  # openpyxl absent — say so, do not 500
        raise HTTPException(
            status_code=501,
            detail="openpyxl is not installed on the server host "
                   "(pip install openpyxl)",
        ) from exc
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{export.filename(fn, feature, scope)}"'},
    )


class ReportBody(BaseModel):
    """When to send it. Never WHO to — that comes from the session.

    There is no recipient field on purpose: a standing request that could
    name somebody else is one person's click causing mail to a person who
    never asked for it, every month, possibly containing something they are
    not allowed to see. Somebody who wants it subscribes themselves.
    """

    period: str = ""
    day: int = Field(default=1, ge=1, le=31)
    time_of_day: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


def _report_view(sub: reports.Subscription | None) -> dict:
    if sub is None:
        return {"subscribed": False}
    # Said at the moment somebody turns it on, not discovered a month later
    # when nothing has arrived. The subscription still stands — an address
    # can be added — but the person asking is told now.
    address = rewardlens.address_of(sub.user)
    return {
        "subscribed": True,
        "warning": ("" if address else
                    "Compass has no email address for you, so this will be "
                    "recorded each month and not sent. SCS can add one."),
        "period": sub.period,
        "day": sub.day,
        "time_of_day": sub.time_of_day,
        "summary": sub.summary,
        "runs": [{"at": r.at, "period": r.period, "said": r.said,
                  "figures": [list(f) for f in r.figures]}
                 for r in reversed(sub.runs)],
    }


@router.get("/{function_id}/features/{feature_id}/report")
async def get_report(
    function_id: str,
    feature_id: str,
    user: str = Depends(require_user),
) -> dict:
    """This person's own standing request for this screen, and its history."""
    await _ready()
    _resolve(function_id, feature_id)
    return _report_view(await reports.for_user(user, function_id, feature_id))


@router.put("/{function_id}/features/{feature_id}/report")
async def set_report(
    function_id: str,
    feature_id: str,
    body: ReportBody,
    user: str = Depends(require_user),
) -> dict:
    """Ask for this screen monthly, for yourself.

    Refused when the screen would need a scope nobody has chosen: a report
    of nothing-in-particular arriving every month is worse than none.
    """
    await _ready()
    _fn, feature, _handler = _resolve(function_id, feature_id)
    scope = _scope(user, "", body.period, feature)
    if missing := scope.missing(feature.scope):
        raise HTTPException(
            422,
            f"A monthly {feature.name} report needs "
            f"{' and '.join(missing)} — it has to know what it is reporting on.")
    sub = await reports.subscribe(
        user=user, function_id=function_id, feature_id=feature_id,
        period=body.period, day=body.day, time_of_day=body.time_of_day)
    return _report_view(sub)


@router.delete("/{function_id}/features/{feature_id}/report")
async def stop_report(
    function_id: str,
    feature_id: str,
    user: str = Depends(require_user),
) -> dict:
    """Stop your own. There is no route for stopping anybody else's."""
    await _ready()
    _resolve(function_id, feature_id)
    await reports.unsubscribe(user, function_id, feature_id)
    return {"subscribed": False}


@router.post("/{function_id}/features/{feature_id}/report/send")
async def send_report_now(
    function_id: str,
    feature_id: str,
    user: str = Depends(require_user),
) -> dict:
    """Send yours now, so somebody can see what will arrive on the 1st.

    The same render and the same delivery the loop uses, so what they see
    is what the month will bring rather than a preview of it.
    """
    await _ready()
    _resolve(function_id, feature_id)
    sub = await reports.for_user(user, function_id, feature_id)
    if sub is None:
        raise HTTPException(404, "You have no monthly report for this screen.")
    # The same render, the same save and the same delivery the loop uses, so
    # what somebody sees now is what the month will bring rather than a
    # preview of it.
    sub = await reports.save(await asyncio.to_thread(reports.run, sub))
    await _deliver()
    await _save()
    return _report_view(sub)


async def _ready() -> None:
    """Load the register, the trail and the outbox before answering.

    Lazily rather than only at startup: a check, a test client or a reload
    gets the same state a running server has, and `ready` is a no-op once
    the process has it.
    """
    from compass.businessfunctions.features import rewardlens

    await ledger.ready(seed=rewardlens.SEED)
    await notices.ready()


async def _save() -> None:
    """Persist what this request recorded.

    After the work and after delivery, for the reason the ledger gives: a
    slow store delays a reply rather than undoing a decision that is
    already true in this process.
    """
    try:
        await ledger.flush()
        await notices.save()
    except Exception:  # noqa: BLE001 — a decision is not undone by a slow store
        logger.warning("business functions: could not store what just "
                       "happened; it is in memory and will be written on the "
                       "next change", exc_info=True)


async def _deliver() -> None:
    """Send anything an action just put in the outbox.

    In a thread, because SMTP blocks and a twenty-second timeout inside a
    request is a twenty-second request. After the action, never before: the
    decision is already recorded, so a mail server that is down delays a
    reply and does not undo what somebody decided.

    Failures are not raised. The notice records what happened and the row
    shows it, which is the point of writing it down first — an exception here
    would tell the reviewer their decision failed, and it did not.
    """
    try:
        sent, stuck = await asyncio.to_thread(notices.flush)
        if sent or stuck:
            logger.info("business-function notices: %d sent, %d held", sent, stuck)
    except Exception:  # noqa: BLE001 — a notice is not the decision
        logger.warning("business-function notices could not be delivered",
                       exc_info=True)


class ActBody(BaseModel):
    action: str
    targets: list[str] = Field(default_factory=list)
    entity: str = ""
    period: str = ""
    #: The reason, for an action whose manifest asks for one. Capped here so
    #: an essay is refused before the handler sees it; the handler applies
    #: its own, tighter limit and refuses an empty one.
    note: str = Field(default="", max_length=4096)


@router.post("/{function_id}/features/{feature_id}/act")
async def act(
    function_id: str,
    feature_id: str,
    body: ActBody,
    user: str = Depends(require_user),
) -> dict:
    """Do one thing to rows the person pointed at.

    No plan, because there is nothing to confirm that the click did not
    already say: these targets came from a button on a specific row, not from
    a sentence somebody had to interpret. The handler still re-checks state,
    refuses a closed period and refuses a bulk outward action.
    """
    await _ready()
    _fn, _feature, handler = _resolve(function_id, feature_id)
    outcome = handler.act(_scope(user, body.entity, body.period, _feature),
                          body.action, body.targets, body.note)
    await _deliver()
    await _save()
    return {"ok": outcome.ok, "said": outcome.said, "touched": outcome.touched}


class FormBody(BaseModel):
    """What somebody typed. Strings, because that is what a form sends.

    The handler parses and judges them; nothing here assumes a value is a
    number or a date just because the manifest called the field one.
    """

    values: dict[str, str] = Field(default_factory=dict)
    #: The row this form is about, for a form opened from one.
    about: str = Field(default="", max_length=128)
    entity: str = ""
    period: str = ""


#: A ceiling on one value, well past anything a field asks for. The handler
#: enforces its own, tighter limits; this one exists so a megabyte of text
#: is refused before any of that runs.
_VALUE_MAX = 4096


def _form_values(feature, form_id: str, body: FormBody) -> dict[str, str]:
    """The submitted values, checked against the shape the manifest declared.

    The route enforces the SHAPE — these fields and no others — and the
    handler enforces the MEANING. Splitting it that way means an unknown key
    is refused at the edge, before anything that might treat it as data.
    """
    form = next((f for f in feature.forms if f.id == form_id), None)
    if form is None:
        raise HTTPException(404, f"{feature.name} has no form called {form_id!r}.")

    declared = {x.id for x in form.fields}
    if unknown := sorted(set(body.values) - declared):
        raise HTTPException(
            422, f"{form.label} does not ask for {', '.join(unknown)}.")
    for key, value in body.values.items():
        if len(value) > _VALUE_MAX:
            raise HTTPException(422, f"{key} is too long.")
    return {k: body.values.get(k, "") for k in declared}


@router.post("/{function_id}/features/{feature_id}/forms/{form_id}/preview")
async def preview_form(
    function_id: str,
    feature_id: str,
    form_id: str,
    body: FormBody,
    user: str = Depends(require_user),
) -> dict:
    """Say what recording this would do. Changes nothing.

    The half of the form that earns its place: a person declaring a ₹4,000
    hamper cannot otherwise know it is the one that takes them past the
    annual limit, which is the same blindness the whole feature is about.
    """
    await _ready()
    _fn, feature, handler = _resolve(function_id, feature_id)
    values = _form_values(feature, form_id, body)
    outcome = handler.preview(_scope(user, body.entity, body.period, feature),
                              form_id, values, body.about)
    return {"ok": outcome.ok, "said": outcome.said}


@router.post("/{function_id}/features/{feature_id}/forms/{form_id}")
async def submit_form(
    function_id: str,
    feature_id: str,
    form_id: str,
    body: FormBody,
    user: str = Depends(require_user),
) -> dict:
    """Record it, against the person who is signed in and nobody else.

    `user` comes from the session, never from the body. A form that let the
    caller name whose record to write to would be a form for putting a gift
    on somebody else's year.
    """
    await _ready()
    _fn, feature, handler = _resolve(function_id, feature_id)
    values = _form_values(feature, form_id, body)
    outcome = handler.submit(_scope(user, body.entity, body.period, feature),
                             form_id, values, body.about)
    await _save()
    return {"ok": outcome.ok, "said": outcome.said, "touched": outcome.touched}


class AskBody(BaseModel):
    text: str
    entity: str = ""
    period: str = ""
    #: Which view is open. "That one" means a row the person can see, and
    #: which rows those are depends on the tab.
    tab: str = ""


@router.post("/{function_id}/features/{feature_id}/ask")
async def ask(
    function_id: str,
    feature_id: str,
    body: AskBody,
    user: str = Depends(require_user),
) -> dict:
    """Read a sentence against what is on screen. Changes nothing, ever.

    Three shapes come back and the rail renders each differently: a `plan` to
    be confirmed or cancelled, an `answer` carrying the facts a model will
    phrase, or a `clarify` when the sentence named an action but not clearly
    enough which rows — asked rather than guessed, because an assistant that
    picks the likeliest row is wrong exactly when it hurts.
    """
    await _ready()
    fn, feature, handler = _resolve(function_id, feature_id)
    scope = _scope(user, body.entity, body.period, feature)
    result = assistant.interpret(fn.id, feature, handler, scope, body.text,
                                 user, body.tab)

    if isinstance(result, assistant.Proposal):
        return {
            "kind": "plan",
            "plan": {
                "id": result.id,
                "action": result.action,
                "headline": result.headline,
                "detail": result.detail,
                "targets": [{"id": t.id, "label": t.label} for t in result.targets],
                "outward": result.outward,
                "reversible": result.reversible,
            },
        }
    if isinstance(result, assistant.Clarify):
        return {
            "kind": "clarify",
            "text": result.text,
            "options": [{"id": t.id, "label": t.label} for t in result.options],
        }
    return {"kind": "answer", "text": result.text, "facts": result.facts}


class PlanBody(BaseModel):
    """The plan's own coordinates, so the right handler performs it.

    Carried rather than looked up from the proposal because the handler is
    resolved through the registry the same way every other route resolves it,
    and a second lookup path is a second thing to keep correct.
    """

    function_id: str
    feature_id: str


@router.post("/plans/{plan_id}/confirm")
async def confirm_plan(
    plan_id: str, body: PlanBody, user: str = Depends(require_user)
) -> dict:
    """Carry out a plan, once, for the person who made it.

    This performs, so it loads and saves like any other way of acting: a
    decision taken from the rail is the same decision taken from the table.
    """
    await _ready()
    _fn, _feature, handler = _resolve(body.function_id, body.feature_id)
    outcome = assistant.confirm(plan_id, user, handler)
    await _deliver()
    await _save()
    return {"ok": outcome.ok, "said": outcome.said, "touched": outcome.touched}


@router.post("/plans/{plan_id}/cancel")
async def cancel_plan(plan_id: str, user: str = Depends(require_user)) -> dict:
    """Drop a plan, and say plainly that nothing happened."""
    outcome = assistant.cancel(plan_id, user)
    return {"ok": outcome.ok, "said": outcome.said}
