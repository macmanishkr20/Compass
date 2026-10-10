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
    "employee",     # everybody the directory knows
    "manager",      # a service line lead, for the people who report to them
    "procurement",  # buys and distributes
    "compliance",   # reviews, queries, records breaches
    "finance",      # tax thresholds and what was referred
    "audit",        # reads everything, changes nothing
    "leadership",   # the aggregate view
})

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
                 frozenset({"compliance"})),
    # The signed-in administrator on a local deployment: compliance and
    # leadership, and ₹12,000 of their own, one ordinary gift from the limit.
    "admin": Person("E-1052", "Aisha Khan", "aisha.khan@example.invalid",
                    frozenset({"compliance", "leadership"})),
    # An ordinary employee. Holds nothing else, which is what makes the
    # employee view worth looking at rather than assuming.
    "enduser": Person("E-1066", "Vikram Rao", "vikram.rao@example.invalid",
                      frozenset()),
    # A service line lead, for the team breadth.
    "servicelead": Person("E-1033", "Rahul Desai", "rahul.desai@example.invalid",
                          frozenset({"manager"}),
                          frozenset({"E-1007", "E-1019"})),
    # Roles with no person behind them: compliance, finance and audit review
    # other people's records and have none of their own in this fixture, so
    # there is nothing for a conflict rule to trip over.
    "compliance": Person("", "Compliance", "", frozenset({"compliance"})),
    "finance": Person("", "Finance", "", frozenset({"finance"})),
    "audit": Person("", "Audit", "", frozenset({"audit"})),
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


def team_of(user: str) -> frozenset[str]:
    """The employee ids this person is responsible for. Empty unless a manager."""
    person = find(user)
    if person is None or "manager" not in person.all_roles:
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


def scope_for(user: str, feature, *, entity: str = "",
              period: str = "") -> "Scope":
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
        user=user, entity=entity, period=period,
        roles=roles_of(user),
        employee=employee_of(user),
        team=team_of(user),
        sees=breadth(user, feature.visibility) if feature is not None else "firm",
    )
