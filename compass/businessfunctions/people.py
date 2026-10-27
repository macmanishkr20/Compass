"""Who somebody is, and what that lets them see.

Three facts were scattered across one feature before this: which employee a
login is, where to write to them, and whether they may review. They are the
same fact — a directory entry — and keeping them apart meant a role was a
`frozenset` inside a gift tracker, which is not where a firm's roles live.

So this is the directory, standing in for the real one. It is deliberately
the only place in the section that knows a person, and everything else asks
it.

ROLES ARE NOT PERMISSIONS. A role says what somebody IS — compliance,
finance, a manager. What that lets them see is declared per feature in its
manifest, and what it lets them do is declared per action. The same person
is firm-wide on one screen and self-only on another without anything here
changing, which is the point: a role that carried its own permissions would
have to be re-granted every time a feature was added.

BREADTH, NOT A FLAG. The useful question about a persona is not "may they
open this" but "how much of it is theirs to see":

    self    their own record, and nothing about anybody else
    team    the people who report to them
    firm    everybody

Most of the seven personas in the requirement want the same screen at a
different breadth, so breadth is the model and a boolean is not. An
employee opening the gift register sees one row — their own — which is a
useful screen rather than a locked door.

EVERYBODY IS AN EMPLOYEE. The floor role is implicit: somebody the
directory knows holds `employee` whether or not it is written down, because
the thing every one of these features is ultimately about is what happened
to a person. Somebody the directory does NOT know holds nothing, sees
nothing, and is told so — inventing an identity for them would put a gift
on a person-shaped hole.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from compass.common.config import get_settings

#: The personas the requirement names. A manifest may only mention these, so
#: a typo is a function that will not load rather than a rule that silently
#: applies to nobody.
ROLES = frozenset({
    # The eight personas the business named, one role each. The names are
    # theirs, not ours: a role called `manager` where the business says
    # "Service Line Approver" is a translation somebody has to do in their
    # head every time they read a screen or a manifest.
    "employee",          # confirms receipt where the workflow asks for it
    "requester",         # Business Requester: raises it, records what was given
    "service_line",      # Service Line Approver: need, budget, recipient list
    "finance",           # Finance Team: policy thresholds, exceptions, controls
    "payroll",           # Payroll Team: tax logic and employee-level values
    "procurement",       # Procurement / SCS: buys through approved channels
    "accounts_payable",  # Accounts Payable: the payment
    "operations",        # Operations / SPOC: hands the gifts out and keeps
                         # the recipient list straight. Not a Business
                         # Requester: the person who ASKS for two hundred
                         # hampers is rarely the person who distributes
                         # them, and until this existed the chain made
                         # them the same person.
    "compliance",        # Leadership / Compliance: dashboards, alerts, audit
    "leadership",        # the aggregate view; audit duties sit here too
    # Not a persona the business named. `admin` administers the SYSTEM
    # rather than taking part in the work it records: the thresholds every
    # other persona is judged against, and the history of what was done.
    # It was granted in the directory without being declared here, so a
    # manifest naming it was refused while a Person holding it was not.
    "admin",
})

#: What each persona is called where a person reads it. The manifest says
#: `payroll`; a screen says "Payroll Team".
LABELS = {
    "employee": "Employee",
    "requester": "Business Requester",
    "service_line": "Service Line Approver",
    "finance": "Finance Team",
    "payroll": "Payroll Team",
    "procurement": "Procurement / SCS",
    "operations": "Operations / SPOC",
    "accounts_payable": "Accounts Payable",
    "compliance": "Leadership / Compliance",
    "leadership": "Leadership",
}

#: How much of a screen is somebody's to see, narrowest first. Ordered so
#: that "the widest of their roles" is a `max`.
BREADTHS = ("none", "self", "team", "firm")


@dataclass(frozen=True)
class Person:
    """One directory entry."""

    employee_id: str
    name: str
    email: str = ""
    roles: frozenset[str] = field(default_factory=frozenset)
    #: Employee ids reporting to them. Only meaningful with `manager`.
    manages: frozenset[str] = field(default_factory=frozenset)
    #: Which team they BELONG to — not which team pays for their gifts.
    #: One team, Procurement, buys everything; the people who receive it
    #: work all over the firm, and "which part of the firm is receiving
    #: this" is a question only the recipient's own team answers. Empty
    #: for the shared mailboxes, which are desks rather than people.
    team: str = ""

    @property
    def all_roles(self) -> frozenset[str]:
        """Their roles, with the floor every known person holds."""
        return self.roles | {"employee"}


#: login name -> who they are. The stand-in. Addresses are all under
#: `.invalid`, which RFC 2606 reserves so they can never resolve: these are
#: fixture people, and a fixture that could reach a real inbox if somebody
#: configured a mail server is a fixture waiting to do it.
#:
#: Priya Nair has no address on purpose — "nobody knows where to write" is a
#: real state, and a screen that cannot show it will one day claim somebody
#: was emailed when they were not.
DIRECTORY: dict[str, Person] = {
    # A compliance reviewer who is also a recipient, so the rule about not
    # deciding your own record has something to catch.
    "mk": Person("E-1041", "Manish K.", "manish.k@example.invalid",
                 frozenset({"compliance"}), team="Risk & Compliance"),
    # The signed-in administrator on a local deployment: compliance and
    # leadership, and ₹12,000 of their own, one ordinary gift from the limit.
    "admin": Person("E-1052", "Aisha Khan", "aisha.khan@example.invalid",
                    frozenset({"compliance", "leadership", "admin"}),
                    team="Risk & Compliance"),
    # An ordinary employee. Holds nothing else, which is what makes the
    # employee view worth looking at rather than assuming.
    "enduser": Person("E-1066", "Vikram Rao", "vikram.rao@example.invalid",
                      frozenset(), team="Service line · Advisory"),
    # A service line lead who also raises requests — the common real case,
    # and the reason the rule is "not the one you raised" rather than "not
    # your team's".
    "servicelead": Person("E-1033", "Rahul Desai", "rahul.desai@example.invalid",
                          frozenset({"service_line", "requester"}),
                          frozenset({"E-1007", "E-1019"}),
                          team="Service line · Advisory"),
    # A second service line approver. Not padding: with one, a request the
    # approver raised themselves had nowhere to go — the rule says they
    # cannot sign off their own, and there was nobody else holding the
    # persona to sign it off instead, so the chain stalled at step one. A
    # firm with one approver per service line has this problem in real life
    # and solves it the same way.
    "leadtwo": Person("E-1077", "Priya Nair", "priya.nair@example.invalid",
                      frozenset({"service_line", "requester"}),
                      team="Service line · Tax"),
    # The teams in the chain. None of them has an employee record: they act
    # on other people's gifts and receive none in this fixture, so there is
    # nothing for the "not your own" rules to trip over.
    "requester": Person("", "Business Requester", "", frozenset({"requester"})),
    "finance": Person("", "Finance Team", "", frozenset({"finance"})),
    "payroll": Person("", "Payroll Team", "", frozenset({"payroll"})),
    "procurement": Person("", "Procurement / SCS", "",
                          frozenset({"procurement"})),
    "payments": Person("", "Accounts Payable", "",
                       frozenset({"accounts_payable"})),
    # The desk that actually hands things out. No employee record: they
    # distribute other people's gifts and receive none in this fixture.
    "spoc": Person("", "Operations / SPOC", "", frozenset({"operations"})),
    "compliance": Person("", "Leadership / Compliance", "",
                         frozenset({"compliance", "leadership"})),
}

#: Which team the employees who have NO LOGIN belong to. The directory is
#: keyed by login because that is what a person reading this file
#: recognises, and somebody who receives gifts without ever signing in to
#: Compass has no key in it — they are a recipient, not a user.
#:
#: Deliberately not a second Person in DIRECTORY: there is already a Priya
#: Nair with a login (E-1077) and a different Priya Nair on the register
#: (E-1019), and adding the second to a dict that `_employee_named` reads
#: in order would quietly change which of them a typed name resolves to.
#: A team is the only fact we need about these two, so a team is all this
#: holds.
TEAMS_BY_EMPLOYEE: dict[str, str] = {
    "E-1007": "Service line · Advisory",
    "E-1019": "Service line · Advisory",
}


def _logins_for(user: str) -> set[str]:
    """Every login that resolves to this identity, and the identity itself.

    `require_user` hands back the CANONICAL identity, and a deployment may
    alias a login onto something else entirely — `admin` onto an address.
    The directory is keyed by login because that is what a person reading
    this file recognises, so the aliases are followed back first.

    This was not theoretical: keyed on the login alone, every role matched
    nobody on a deployment that aliases its accounts, and the screens looked
    perfectly fine while the rules that should have fired stayed quiet.
    """
    aliases = get_settings().auth.identity_aliases
    return {user} | {login for login, ident in aliases.items() if ident == user}


def find(user: str) -> Person | None:
    """The directory entry for whoever is asking, or None."""
    for login in _logins_for(user):
        if (person := DIRECTORY.get(login)) is not None:
            return person
    return None


def roles_of(user: str) -> frozenset[str]:
    """What this person is. Empty for somebody the directory cannot place."""
    person = find(user)
    return person.all_roles if person else frozenset()


def employee_of(user: str) -> str:
    """Which employee record is theirs, or "" when there is not one.

    A role with no employee record is ordinary here — a compliance mailbox
    reviews other people's gifts and receives none — so this being empty is
    a fact about them, not a failure.
    """
    person = find(user)
    return person.employee_id if person else ""


def address_of(user: str) -> str:
    """Where to write to them, or "" when nobody knows."""
    person = find(user)
    return person.email if person else ""


def find_by_employee(employee_id: str) -> Person | None:
    """The directory entry for an EMPLOYEE NUMBER, or None.

    The directory is keyed by login, and a recipient is reached by their
    employee id — they are somebody a gift was recorded against, which is
    not the same as somebody who signs in. Several people on the register
    have no login at all, so None here is an ordinary answer and the
    caller holds the notice with a reason rather than guessing an address.
    """
    if not employee_id:
        return None
    return by_employee().get(employee_id)


def by_employee() -> dict[str, Person]:
    """The directory keyed the other way: employee number to person.

    Built on every call rather than cached, because the directory is
    rebuilt when a deployment's users change and a stale index would hand
    back somebody who is no longer there.

    It exists because `find_by_employee` is a SCAN, and a distribution
    calls it once per recipient: at a hundred thousand recipients against
    a directory of any size that is the one place in this module where an
    ordinary-looking loop turns into a quadratic one. Callers with a list
    of ids take this once and read it instead.

    First entry wins on a duplicate employee number, which matches what
    the scan returned.
    """
    out: dict[str, Person] = {}
    for person in DIRECTORY.values():
        if person.employee_id and person.employee_id not in out:
            out[person.employee_id] = person
    return out


def holders(role: str) -> list[Person]:
    """Who to write to when something is this role's to know.

    An ALERT has to reach a person, and a role is not a person. Several of
    the entries here are team mailboxes with no address — they act on other
    people's gifts and receive none — so writing to the role alone would
    produce a notice held for "nobody knows their address" while three
    people who hold that role sit in the directory with addresses on them.

    So: everybody holding the role who can actually be written to, and the
    role's own mailbox when it can. Empty when nobody can be reached, which
    the caller turns into one held notice rather than silence — "nobody was
    told" is a fact somebody needs, and a missing alert is not.
    """
    return [person for person in DIRECTORY.values()
            if role in person.roles and person.email]


def team_of_employee(employee_id: str) -> str:
    """Which part of the firm this employee works in, or "" if nobody knows.

    One team buys everything — Procurement — so the paying team stopped
    being a useful axis the moment the budgets became a single per-person
    allowance. The RECEIVING team did not: "which part of the firm is this
    going to" is still the question leadership asks of a year's gifts, and
    it is a fact about the recipient rather than about the purchase.

    Empty rather than guessed. A chart with an "unknown" bar is a chart
    somebody can go and fix; a chart that quietly files strangers under
    the biggest team is one nobody can.
    """
    for person in DIRECTORY.values():
        if person.employee_id == employee_id and person.team:
            return person.team
    return TEAMS_BY_EMPLOYEE.get(employee_id, "")


def employees_by_team() -> dict[str, list[str]]:
    """Every employee the firm knows, grouped by the team they work in.

    What a team's gift ALLOWANCE is, now that there is one number per
    person: this many people times that number. It is a headcount read off
    the directory rather than a budget somebody typed, which means a team
    that hires has a bigger allowance the same day — and nobody has to
    remember to change a figure.

    People with no team are left out rather than bucketed, for the reason
    `team_of_employee` gives.
    """
    out: dict[str, list[str]] = {}
    for person in DIRECTORY.values():
        if person.employee_id and person.team:
            out.setdefault(person.team, []).append(person.employee_id)
    for employee_id, team in TEAMS_BY_EMPLOYEE.items():
        out.setdefault(team, []).append(employee_id)
    return {team: sorted(set(ids)) for team, ids in sorted(out.items())}


def team_of(user: str) -> frozenset[str]:
    """The employee ids this person is responsible for. Empty unless a manager."""
    person = find(user)
    if person is None or "service_line" not in person.all_roles:
        return frozenset()
    return person.manages


def breadth(user: str, visibility: dict[str, list[str]]) -> str:
    """How much of a screen is theirs, given what the manifest declared.

    The WIDEST of their roles wins, because a person who is both a manager
    and in compliance is in compliance — narrowing them to their own team
    would be a rule nobody asked for and everybody would work around.

    A feature that declares no visibility is visible in full, which keeps
    every feature written before this one behaving exactly as it did.
    """
    if not visibility:
        return "firm"
    mine = roles_of(user)
    if not mine:
        return "none"
    widest = "none"
    for level in BREADTHS:
        if level == "none":
            continue
        if mine & set(visibility.get(level, [])):
            widest = level
    return widest


def scope_for(user: str, feature, *, entity: str = "", period: str = "",
              since: str = "", until: str = "") -> "Scope":
    """The scope for one person on one screen. The ONE place that builds one.

    Routes used to assemble this and the monthly report assembled its own,
    which held for exactly as long as a scope meant "who and what period".
    The moment it also meant "how much of this is yours", the report was
    still building the old one and quietly rendered firm-wide figures for
    somebody entitled to see a single row. The check written for that caught
    it, which is the only reason this is a function rather than a story.

    `feature` may be None for a function's overview, where no one screen is
    open and nobody's records are being listed.
    """
    from compass.businessfunctions.features.base import Scope

    return Scope(
        user=user, entity=entity, period=period, since=since, until=until,
        roles=roles_of(user),
        employee=employee_of(user),
        team=team_of(user),
        sees=breadth(user, feature.visibility) if feature is not None else "firm",
    )
