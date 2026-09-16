"""Prose in, a draft brief out — the third and last place a model is used.

The form this fills has around thirty fields across six sections, and most of
what goes in them is already sitting in the paragraph somebody wrote in a
planning doc. Asking them to retype it as structure is the reason a costing
tool gets opened once and never again.

So: describe the thing, get a filled form, correct it. The correcting is not a
politeness — it is the mechanism. Nothing is costed until a person has looked
at every field, which is what keeps this on the right side of the module's
founding rule even though the model is doing more here than labelling.

**What it may draft, and what it may not.** It drafts what the thing is — the
name, the domain, who uses it, the *work breakdown*, the AI use cases, the
assumptions, the rough scale — and it drafts the volume figures *only where the
description states them*.

The work breakdown is the substantial half, and it is where this stops being a
form-filler. Reading a description and naming the eight-to-twenty modules and
the sub-features underneath them is requirement analysis, and it is the thing
that makes an estimate accurate: a t-shirt size on "Search" is a guess about a
bucket, while "search and filters, sorting and autocomplete, multilingual
analyzers" is a list somebody can argue with line by line. The model still
never states an hour — it picks a *band* from a fixed set, the way it picks a
task type, and code multiplies bands by the house unit.

**Why the prompt argues about the band range.** Measured against a real
architect's sheet for the same system, three drafts of one paragraph landed at
2151, 2052 and 1899 hours against the sheet's 2034 — a mean dead on it. The
agreement was worthless. The architect's 58 lines ran the whole ladder, 19 of
them at the two-day minimum and 3 at the ceiling; every draft used four bands
out of six, never once emitting an xs or an xxl, with over half of all lines
on `m`. Same mean, no dynamic range: the model was pricing the average of a
system rather than the system, and the totals matched because the errors — up
to 2.4x per module — cancelled. Two sentences in the earlier prompt caused it.
"the ceiling" described xxl as a boundary to stay under rather than a thing to
say, so it was never said; and nothing anywhere claimed a lopsided spread was
what a real plan looks like. Both are now stated outright, which is also what
makes the report's ceiling flag reachable from a drafted brief at all — it had
never once fired on one.

Then a second finding changed the fix. Stating the range moved almost nothing:
xs stayed under 1% of lines. The architect's small lines turn out not to be
small *estimates* at all — they are the members of sets he wrote out
separately, one line per content category, and the model was folding each set
into a single averaged line. So the instruction that works is about
enumeration, not about sizing, and it is the one about naming a set's members.

**The numbers below are ten runs an arm, not three.** Three was what the first
pass used, and three was not enough to tell any of this apart. It reported the
old prompt landing dead on the sheet at 1.000x and the new one drifting to
1.11x, and both halves of that were sampling noise: at n=10 the arms sit at
1.093x and 1.100x, a difference of 15 hours, p=0.90. The old prompt was never
accurate on the total. It had drawn three tickets that happened to average
2034. Nothing about the prompt caused that and nothing about the change broke
it.

What survives at n=10, per draft of the same paragraph:

    xs share of lines        0.4%  ->  17.2%   p<0.0001   (the sheet's is 33%)
    biggest band holds      46.8%  ->  38.4%   p=0.0001   (the sheet's is 33%)
    bands in use              4.3  ->    5.1
    total hours             2223h  ->  2238h   p=0.90     — no drift
    sd of total hours        336h  ->   169h   p=0.036    — half the spread
    runs within 10% of sheet   5/10 ->   5/10

The spread halving is the result worth keeping. Accuracy on the mean did not
move, but a drafter whose answer ranges over 1251 hours run to run is not
usable at all, and one that ranges over 522 is. Enumerating a set's members is
apparently a stabler operation than judging how big the set is.

**A correction to the first version of this note.** It said gross error
against the sheet rose, 56.8% -> 64.6% (p=0.009). That was a fault in the
measurement, not the model: the buckets were assigned by module name, and the
architect files the content categories in a module of their own while the
drafter files them under upload, so one boundary disagreement was charged
twice. Bucketing on the sub-feature name instead — which leaves nothing on the
sheet unclassified, against 10% before — gives 55.1% -> 50.5%, p=0.128.
Unchanged, if anything better.

**A second instruction earns its place: enumerate what the description names,
not the machinery under it.** Told to enumerate, the model elaborated — an
upload became a drag-and-drop line, a resumable-upload line, a progress
line, a storage line and a secure-transfer line, none of which the description
asked for as separate concerns. Measured with the paragraph in and out, on ten
drafts each, those invented lines fall from 38h to 5h a draft (p=0.037) and the
run-to-run spread nearly halves, sd 309 -> 162 (F=3.67, p=0.044). Worst single
draft: 702h off the sheet, down to 333h.

**What none of it did.** Not one of these changes moved accuracy. Per-concept
error against the sheet is flat across every version measured, and so is the
mean total. What they move is *spread* — 336 -> 169 for the enumeration rule,
309 -> 162 for this one — and that is the whole case for them. A drafter whose
answer swings 1251 hours between identical calls is not usable; one that swings
500 is. Every accuracy claim made about this prompt has evaporated under n=10
and every variance claim has held.

**Three instructions were written, measured and removed. Do not retry them
without reading this.** All three targeted the authentication over-count: the
architect gives authentication and authorisation one 45-hour line, the drafter
gives it four lines and 140-190 hours.

*Splitting conserves effort* (the parts of a split line should sum to about the
whole): measured at 1.17x against 1.10x, no shape gain. Removed.

*One mechanism is one line, however many places it applies*: auth 147.6h ->
139.5h, p=0.71. Removed.

*Where a capability already exists, price connecting to it*: auth 142.2h ->
144.0h, p=0.92. Removed.

The last two failed the same way, and it is worth knowing why before writing a
fourth. **Naming a pattern in the prompt makes the model write lines using that
pattern's vocabulary, whether the instruction was "do" or "don't".** The
mechanism rule listed "access control" among the phrasings to avoid and
"access control" lines went from 17h to 50h a draft (p=0.016). The integration
rule was then deliberately phrased for what to price rather than against what
not to, and "integration" in line names still went from 1.7 to 3.5 a draft
(p=0.0002). Both times the vocabulary went up and the hours did not go down.

And the residue is probably not a prompt fault at all. The drafter prices
corporate SSO at exactly 45h — the architect's whole line — and then adds
70-100h of role management and access-control enforcement on top. The architect
does not, because he has written down that two roles are in scope and knows two
roles is configuration on top of SSO rather than a subsystem. That is a
judgement about what a well-understood pattern costs, and three framings did
not shift it. It is a good thing for a person to correct on the form, which is
what the form is for.

That second half was nearly a flat refusal, and a flat refusal was wrong. The
first draft of this file left volume out of the schema entirely on the grounds
that requests-per-day multiplies straight into every infrastructure and token
figure, so a plausible-looking number invented from a sentence that never
mentioned traffic is a fabricated figure wearing a computed one's clothes. All
true — but a brief that says "roughly 900 invoices a day" is not an invitation
to infer, it is a stated fact, and dropping it on the floor to make someone
retype it is precisely the friction the button exists to remove. So the rule
is the sharper one: **transcribe a number that is there; never infer one that
is not.** Nulls survive, and a thin brief still shows up as low confidence
rather than as a confident answer built on a guess.

Two things stay out of reach whatever the prose says. `minutes_per_call` is
the dial the entire ROI model turns on, and the rate card is what the
organisation pays its own people. Neither is a fact about the project, both
are policy, and a paragraph describing a piece of software is not evidence
about either.

Why this is one call and not an agent session. The Pipelines builder is a full
tool-calling loop because it edits a live graph over many turns and has to read
it before writing. This reads one paragraph and produces one object. A session
would be machinery for a job that is a single structured completion, and every
extra turn is another chance to drift from what was actually written.
"""

from __future__ import annotations

import json
import logging

from compass.common.gateway.azure_client import get_model_client

from .catalog import SIZE_BAND_UNITS, TASK_TYPES
from .types import ProjectInput

logger = logging.getLogger("compass.estimate")

_SYSTEM = """\
You turn a short description of a software project into a structured brief for \
a costing tool. You are filling in a form that a person is about to read and \
correct.

Extract only what the description supports. An empty field is a correct answer \
and a person will fill it in; an invented one is a wrong answer they have to \
notice first. Never state, estimate or imply a cost, a duration, a team size \
or a price — a deterministic engine computes all of those from what you return, \
and a number from you would corrupt figures that are supposed to be \
reproducible.

`project_name` is the exception, and always has a value: give it a short, \
plain name for what is being built, in the words of the description — \
"Claims document capture", not "AI-Powered Claims Transformation Platform". A \
name is a label rather than a claim, so naming something the description did \
not name costs nothing, while leaving it blank breaks the list it will appear \
in.

The work breakdown is the estimate, and it is the part worth getting right. \
Break the system into modules — the eight to twenty areas a delivery plan \
would have — and break each module into the sub-features somebody would \
actually sit down and build. "Video processing" is a module; "FFmpeg \
transcoding", "HLS/DASH renditions", "player integration" are its \
sub-features. A module with one sub-feature is usually a module you have not \
finished thinking about.

Size each sub-feature with a band, not a number. The band is a judgement \
about this particular line:

  xs  two days     — one validation rule, a toggle, a config flag, a static \
page, a CRUD screen over a table that already exists
  s   three days   — a simple screen or endpoint: a form, a list, a read model
  m   four days    — a screen or service with real logic behind it
  l   five days    — an integration with something you do not control, or a \
non-trivial pipeline stage
  xl  seven days   — a substantial component with several moving parts
  xxl ten days     — work you cannot yet split, because the description does \
not say enough about it

**Use the whole range.** A breakdown where most lines land on the same band \
has not been thought about line by line — it prices the average of a system \
rather than the system, and the total comes out plausible while no single \
line in it is worth reading. Real plans are lopsided, and the largest group \
is usually the smallest work, because most of what a system needs is one \
screen, one rule, one flag. If you have written ten sub-features and none of \
them is xs, you have rounded the cheap work up.

Small lines come from naming things separately, not from shaving days off a \
big line. Two habits produce them:

**When the description names a set, enumerate its members.** Six content \
categories are six lines, not one line called "content categorisation". Four \
languages, three roles, two upload types, five report types — each member is \
its own line and most of them are xs, because the second one is a repeat of \
the first. This is where a real plan's small work comes from, and folding a \
set into one averaged line is the single biggest way a breakdown loses its \
grip on a system.

**A line joined by "and" is often two lines.** "Sorting and autocomplete" is \
two, and each is smaller than the pair sounded. Split it, then size each half \
on its own — the halves are rarely the same size, and that difference is \
information you are throwing away by leaving them joined. But not every \
"and" is two: "type and size validation" is one check written with two \
nouns, and splitting it invents a line. Split where the halves are separate \
work; leave them joined where they are one job.

**Enumerate what the description names, not the machinery under it.** A set \
the description gives you is lines — seven categories, four languages, three \
roles. The plumbing beneath one line is not. "Employees upload a video" is an \
upload line; it does not also become a drag-and-drop line, a resumable-upload \
line, a progress-indicator line, a storage-integration line and a \
secure-transfer line, because the description asked for none of those as \
separate concerns and a reader cannot tell which of them you invented. Where \
the description is more detailed, that is a reason to enumerate what it \
details — not a licence to elaborate around it.

**Nothing may be bigger than xxl.** If a piece of work feels larger than ten \
days, it is not one sub-feature — split it until each part fits. That rule is \
what makes the estimate worth anything: sizing a bucket is a guess, and \
enumerating what is in it is an estimate.

**But xxl is a signal, not a failure to avoid.** When the description \
genuinely does not tell you enough to split something, size it xxl and leave \
it whole. Every xxl line is flagged by name in the report so that a person \
breaks it down before anyone commits to the number. Splitting work you do not \
understand into three confident m's hides exactly the uncertainty this \
breakdown exists to surface.

Mark `ai_candidate` on the sub-features where a model actually does the work, \
and put that work in the breakdown like any other — a subtitle generator is a \
line item, not a surcharge.

AI use cases are the specific jobs a model would do. Give each one the \
`task_type` that fits, and be willing to use the deterministic ones \
(`rules_workflow`, `crud_lookup`, `threshold_alerting`) when a job plainly \
needs no model — saying so is more useful than finding AI in everything.

Put each module in a phase. Phase 1 is what has to exist for the thing to be \
usable at all; phase 2 is what makes it good; phase 3 is what can wait. If the \
description does not suggest a staging, put everything in phase 1 rather than \
inventing one.

`assumptions` is what the estimate takes as given and what it leaves out — the \
sentences somebody will argue with later if they are not written down now. \
Draw them from the description: platforms named or excluded, languages, roles, \
integrations depended on, anything the description is silent about that would \
change the number. Write them as flat statements: "Excludes native Android and \
iOS applications." Ten to twenty is normal, and the ones about what is *out* \
of scope matter most.

`scale` is about reach, not ambition: small is one team or a pilot, medium a \
department, large an organisation, enterprise mission-critical.

The volume fields are the one place a wrong value does real damage: they \
multiply into every infrastructure and token figure. Fill one in **only** when \
the description gives you the number — "900 invoices a day" is a number, "a \
lot of traffic" is not. Leave the rest null. A null is read as "not known \
yet" and lowers the stated confidence, which is the correct outcome; a \
plausible guess is indistinguishable from a measurement once it is in the \
form.\
"""

#: What the model may fill. A deliberate subset of `ProjectInput`: the rate
#: card and the delivery platform are absent entirely, and the volume block is
#: present but every figure in it is nullable, so "not stated" is expressible
#: rather than something the schema forces a guess for.
_SCHEMA = {
    "type": "object",
    "properties": {
        "project_name": {"type": "string"},
        "description": {"type": "string"},
        "industry_domain": {"type": "string"},
        "target_users": {"type": "string"},
        "scale": {"type": "string", "enum": ["small", "medium", "large", "enterprise"]},
        "modules": {
            "type": "array",
            "description": "The work breakdown. Eight to twenty modules, each with its sub-features.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "phase": {"type": "integer", "enum": [1, 2, 3]},
                    "sub_features": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "size": {
                                    "type": "string",
                                    "enum": list(SIZE_BAND_UNITS),
                                    "description": (
                                        "Effort band. Use the whole range: xs "
                                        "for one rule or one screen, xxl for "
                                        "work the description does not say "
                                        "enough about to split."
                                    ),
                                },
                                "ai_candidate": {"type": "boolean"},
                            },
                            "required": ["name", "description", "size", "ai_candidate"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "phase", "sub_features"],
                "additionalProperties": False,
            },
        },
        "assumptions": {
            "type": "array",
            "description": "What the estimate takes as given and what it excludes.",
            "items": {"type": "string"},
        },
        "volume_and_scale": {
            "type": "object",
            "description": "Only the figures the description actually states. Null otherwise.",
            "properties": {
                "expected_daily_users": {"type": ["integer", "null"]},
                "requests_per_day": {"type": ["integer", "null"]},
                "data_volume_gb": {"type": ["number", "null"]},
                "peak_load_pattern": {"type": "string"},
                "growth_rate_percent": {"type": ["number", "null"]},
            },
            "required": [
                "expected_daily_users", "requests_per_day", "data_volume_gb",
                "peak_load_pattern", "growth_rate_percent",
            ],
            "additionalProperties": False,
        },
        "ai_use_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "task_type": {"type": "string", "enum": list(TASK_TYPES)},
                    "priority": {
                        "type": "string",
                        "enum": ["must_have", "nice_to_have", "exploratory"],
                    },
                },
                "required": ["name", "description", "task_type", "priority"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "project_name", "description", "industry_domain", "target_users",
        "scale", "modules", "assumptions", "volume_and_scale", "ai_use_cases",
    ],
    "additionalProperties": False,
}


async def draft_brief(description: str, project_type: str = "new") -> ProjectInput:
    """Draft a brief from prose. Raises on failure — the caller says so.

    Unlike the classifier and the architect, this has no deterministic
    fallback and should not pretend to: those two degrade to a rule that
    produces the same estimate, whereas there is no rule that reads a
    paragraph. A failure here means the form stays empty and the person types
    it, which is exactly where they were before the button existed.
    """
    raw = await get_model_client().complete_utility(
        _SYSTEM,
        description.strip(),
        # Room for the answer, not just for the thinking. A work breakdown is
        # fifteen modules and sixty sub-features with names and descriptions —
        # several thousand tokens of output — and a reasoning model bills its
        # thinking against the same budget. At 4,000 the first attempt spent
        # 3,456 of them thinking and returned a JSON document cut off mid-
        # string, which fails as a parse error and looks like a bad model
        # rather than a small cap.
        max_tokens=16_000,
        # Low effort on purpose: this is enumeration, not deduction. The work
        # is reading a description carefully and naming what is in it, and the
        # budget is better spent on the naming.
        effort="low",
        schema=_SCHEMA,
        schema_name="project_brief",
    )
    data = json.loads(raw)

    # Ids are minted here rather than asked for: they only have to be unique
    # within one brief, and a model asked for identifiers spends tokens
    # inventing them and occasionally repeats one.
    for i, m in enumerate(data.get("modules") or []):
        m["id"] = f"m{i + 1}"
        for j, sub in enumerate(m.get("sub_features") or []):
            sub["id"] = f"m{i + 1}s{j + 1}"
    for i, u in enumerate(data.get("ai_use_cases") or []):
        u["id"] = f"u{i + 1}"
        u["linked_feature_ids"] = []
        # The ROI dial stays at the task type's default until a person moves
        # it deliberately. See the module docstring.
        u["minutes_per_call"] = None

    # A last resort for the one field that cannot be empty. The prompt asks
    # for it directly, but "extract only what is supported" is a strong
    # instruction and a description that never names the project can still
    # come back with a blank — and a nameless estimate breaks the portfolio
    # row, the report heading and the export filename at once.
    if not (data.get("project_name") or "").strip():
        first = description.strip().split(".")[0].strip()
        data["project_name"] = (first[:57] + "…") if len(first) > 58 else (first or "Untitled project")

    data["project_type"] = project_type
    # Validated through the same model the endpoint accepts, so a draft cannot
    # carry a shape a typed brief could not.
    return ProjectInput.model_validate(data)


# ─────────────────────────────────────────────────── reading a requirements document
#
# The same act as drafting from a paragraph, on a longer and more careful
# input — which is why it lives here rather than in a file of its own. The
# list of files that may call a model is a checked, deliberate three, and a
# document reader is not a new kind of model use: it fills the same form,
# under the same rules, for a person to correct.
#
# What it adds is the reading. A paragraph is already somebody's summary; a
# BRD is the raw requirement, and pricing it well is the job a senior
# architect does before anyone opens a spreadsheet — finding the scenarios the
# document walks through and the ones it does not, the edge cases that break
# naive builds, the questions whose answers move the number, and the stack.
# So it runs at high effort, where the paragraph drafter runs at low: that one
# is enumeration, this one is deduction.
#
# It still never states a figure. Headcount and skill are asked of the person,
# because they are facts about the organisation that no document contains.

_BRD_ARCHITECT = """\
You are a senior solution architect preparing a delivery estimate from a \
business requirements document (BRD). Read the whole document before you \
write anything. You produce two things: an analysis a delivery lead would \
walk the client through, and the structured brief the estimate is costed \
from. A person reads and corrects both before anything is priced.

Think the way an architect who has delivered systems like this thinks. For \
every capability ask who uses it, what data it touches, what happens when \
that goes wrong, and what the document leaves unsaid.

**The analysis.**
- summary: three to five sentences — what is being built, for whom, and why.
- functional_requirements: the capabilities, one per line. Stated ones first; \
start an implied one with "(implied)".
- user_roles: every role, and what it may and may not do.
- scenarios: the key journeys, each with its alternate and failure path — \
"Adjuster uploads a claim pack, fields are extracted, the adjuster corrects \
them; alternate: the pack is a scan; failure: extraction confidence is too \
low to trust".
- edge_cases: the situations that break naive builds and that this document \
makes likely. Consider, and keep only what applies: concurrent edits, \
duplicate submissions and idempotency, partial failure and retries, very \
large inputs and pagination, time zones and dates, localisation, \
accessibility, bad or missing data, permission boundaries, audit and \
traceability, retention and deletion, migrating existing data, the limits of \
the systems being integrated, and scheduled or batch work.
- non_functional_requirements: security and privacy, compliance, \
performance, availability and recovery, scalability, observability, \
accessibility, supported browsers and devices, data residency — as the \
document states or clearly implies them.
- integrations: each external system, what flows which way, and what is \
unknown about it.
- risks: what could move the estimate or sink the delivery, each with a \
severity and a mitigation.
- open_questions: every ambiguity that would change the estimate if it were \
answered differently. For each, why it matters and the assumption you are \
pricing under. Every assumed answer must also appear in `assumptions`, so the \
estimate says what it rests on.
- out_of_scope: what the document excludes, or what you exclude and why.
- technologies: the skill areas a delivery lead would rate a team on — \
usually four to eight, never a parts list. People score their team's skill \
against each one, so name them at that level: "Azure" covers App Service, \
Functions, Service Bus, Blob Storage, Key Vault and monitoring; ".NET", \
"Angular", "React", "Python" and "SQL Server" are each one area. Give a \
service its own row only when it needs expertise the platform does not imply \
— "Azure AI" for Azure OpenAI and Document Intelligence, "Power BI", \
"Kubernetes". Put the specific services in `reason`, not in the name. Name \
what the document states; where it states nothing, name what the context most \
plausibly implies and say in `reason` that it is assumed.

**The brief.**
The brief follows the rules below, which were written for a short \
description; here the document is the description. Two additions:

- A module the document does not name but clearly implies — sign-in and \
roles, an audit trail, administration, notifications, reporting, migrating \
existing data, environments and deployment pipelines, performance testing, \
user acceptance support — is planned the way an architect plans it and marked \
`source: "implied"`, with the evidence in its sub-feature descriptions. Add \
one only when the document gives a reason for it; an implied module with no \
evidence behind it is an invented one. Everything the document names is \
`source: "stated"`.
- Each module lists the `technologies` it is built with, using only names \
from your `technologies` list.

Never state or imply hours, days, cost, a timeline, a team size, headcount or \
anyone's skill level. People enter who is available and how well they know \
each technology, and a deterministic engine turns that and your breakdown \
into every figure.

## Rules for the brief

"""

#: The architect's reading, then the drafter's rules verbatim — the band
#: guidance is the part that was measured against a real sheet, and a second
#: paraphrase of it would drift from the first.
_BRD_SYSTEM = _BRD_ARCHITECT + _SYSTEM

#: The most of a document read in one pass. About forty thousand tokens —
#: a long BRD, with room left for the reasoning and a full breakdown. A longer
#: one is read to here and the estimate says so rather than silently pricing
#: the first half.
BRD_MAX_CHARS = 150_000


def _brd_schema() -> dict:
    """The drafter's schema, plus the analysis and a module's technologies and
    source. Built from `_SCHEMA` rather than copied, so a field added to the
    paragraph drafter reaches the document reader too — and so the rate card
    and the team stay out of both for the same reason."""
    import copy

    schema = copy.deepcopy(_SCHEMA)
    module = schema["properties"]["modules"]["items"]
    module["properties"]["technologies"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": "Names taken from analysis.technologies.",
    }
    module["properties"]["source"] = {"type": "string", "enum": ["stated", "implied"]}
    module["required"] = [*module["required"], "technologies", "source"]

    lines = {"type": "array", "items": {"type": "string"}}

    def objects(fields: dict) -> dict:
        return {
            "type": "array",
            "items": {
                "type": "object",
                "properties": fields,
                "required": list(fields),
                "additionalProperties": False,
            },
        }

    analysis = {
        "summary": {"type": "string"},
        "functional_requirements": lines,
        "non_functional_requirements": lines,
        "user_roles": lines,
        "integrations": lines,
        "scenarios": lines,
        "edge_cases": lines,
        "out_of_scope": lines,
        "risks": objects({
            "description": {"type": "string"},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "mitigation": {"type": "string"},
        }),
        "open_questions": objects({
            "question": {"type": "string"},
            "why_it_matters": {"type": "string"},
            "assumed_answer": {"type": "string"},
        }),
        "technologies": objects({
            "name": {"type": "string"},
            "category": {"type": "string"},
            "reason": {"type": "string"},
        }),
    }
    schema["properties"]["analysis"] = {
        "type": "object",
        "properties": analysis,
        "required": list(analysis),
        "additionalProperties": False,
    }
    schema["required"] = [*schema["required"], "analysis"]
    return schema


_BRD_SCHEMA = _brd_schema()


def _unescape(name: str) -> str:
    """Drop Markdown backslash escapes: `\\.NET` -> `.NET`, `C\\#` -> `C#`."""
    import re

    return re.sub(r"\\(.)", r"\1", name or "").strip()


def _tech_key(name: str) -> str:
    return " ".join((name or "").casefold().split())


async def analyse_brd(document: str, document_name: str = "",
                      project_type: str = "new") -> ProjectInput:
    """Read a requirements document the way a senior architect would, and
    draft the brief from it. Raises on failure — the caller says so.

    Returns a brief carrying `brd_analysis`, and nothing costed: the same
    two-step as the paragraph drafter, for the same reason. No team and no
    rate card come out of a document, whatever it says about either.
    """
    text = (document or "").strip()
    truncated = len(text) > BRD_MAX_CHARS
    if truncated:
        text = text[:BRD_MAX_CHARS]

    raw = await get_model_client().complete_utility(
        _BRD_SYSTEM,
        f"Document: {document_name or 'requirements document'}\n\n{text}",
        # An analysis and a full breakdown, after the thinking a whole
        # document deserves. The paragraph drafter's 16,000 was set after a
        # reasoning model spent most of a 4,000 budget thinking and returned
        # JSON cut off mid-string; this output is roughly twice that one.
        max_tokens=32_000,
        effort="high",
        schema=_BRD_SCHEMA,
        schema_name="brd_brief",
    )
    data = json.loads(raw)
    analysis = data.pop("analysis", None) or {}

    # A module may only name technologies the analysis lists, spelled the way
    # the analysis spells them: the skill table is built from that list, and a
    # tag with no row beside it is a score nobody can enter.
    # Names arrive with the odd Markdown escape left in — measured on a real
    # read, ".NET" came back as "\\.NET" and went straight into the skill table.
    for tech in analysis.get("technologies") or []:
        tech["name"] = _unescape(tech.get("name") or "")
    known = {_tech_key(t["name"]): t["name"]
             for t in analysis.get("technologies") or [] if t["name"]}
    for i, m in enumerate(data.get("modules") or []):
        m["id"] = f"m{i + 1}"
        tags: list[str] = []
        for name in m.get("technologies") or []:
            canonical = known.get(_tech_key(_unescape(name)))
            if canonical and canonical not in tags:
                tags.append(canonical)
        m["technologies"] = tags
        for j, sub in enumerate(m.get("sub_features") or []):
            sub["id"] = f"m{i + 1}s{j + 1}"
    for i, u in enumerate(data.get("ai_use_cases") or []):
        u["id"] = f"u{i + 1}"
        u["linked_feature_ids"] = []
        # The ROI dial stays at the task type's default, as in draft_brief.
        u["minutes_per_call"] = None

    # Every question's assumed answer is an assumption the estimate rests on.
    # The prompt asks for them in both places; this makes it true when it
    # forgets, because a priced-under answer missing from the assumptions list
    # is the one disagreement nobody can find later.
    assumptions = [a.strip() for a in data.get("assumptions") or [] if (a or "").strip()]
    seen = {a.casefold() for a in assumptions}
    for q in analysis.get("open_questions") or []:
        answer = (q.get("assumed_answer") or "").strip()
        if answer and answer.casefold() not in seen:
            assumptions.append(answer)
            seen.add(answer.casefold())
    if truncated:
        assumptions.append(
            f"Only the first {BRD_MAX_CHARS:,} characters of the document were "
            "read; anything after that is not in this estimate."
        )
    data["assumptions"] = assumptions

    if not (data.get("project_name") or "").strip():
        stem = (document_name or "").rsplit(".", 1)[0].strip()
        data["project_name"] = stem[:58] or "Untitled project"

    data["project_type"] = project_type
    analysis["document_name"] = document_name
    data["brd_analysis"] = analysis
    return ProjectInput.model_validate(data)
