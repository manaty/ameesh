# SPDX-License-Identifier: AGPL-3.0-only
"""Le plafond d'organisation d'une approbation permanente (#7629, spec §8.3).

Le canon porte, par connecteur et par opération, ce qu'une personne peut
déléguer durablement : qui peut signer, jusqu'à quelle durée et quel montant,
si une approbation SANS bénéficiaire est tolérée, et si la charte et les
cibles sont obligatoires. Le bloc vit sous `extensions.ameesh` de
`federation.yaml` — le schéma OKF Federation n'admet aucune clé libre au
premier niveau :

    extensions:
      ameesh:
        standing_approvals:
          schema: ameesh-standing-approvals/1
          rules:
            - connector: git
              operations: [merge]
              approvers: [human:alice]
              max_duration_hours: 720
              max_amount: 0
              currency: null
              allow_missing_beneficiary: false
              require_charter: true
              require_targets: false

Sans bloc, la politique est VIDE : aucune approbation permanente n'est
acceptée (défaut sûr ; aucune n'est en service, spec §3.5). Un bloc illisible
ou une clé inconnue est refusé, jamais interprété comme un élargissement.

Le plafond est contrôlé à l'ENREGISTREMENT du grant (`receipts.
register_standing`) : le grant est ensuite immuable, seule sa révocation ou
la consommation de son plafond l'écrivent. La couverture d'une action (à la
liaison et à chaque réservation) est décidée en SQL, sur le grant enregistré.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SCHEMA = "ameesh-standing-approvals/1"

#: une durée maximale de 10 ans : au-delà, c'est une erreur de saisie
MAX_DURATION_HOURS = 10 * 365 * 24

_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_APPROVER_RE = re.compile(r"^human:%s$" % _NAME)
_OPERATION_RE = _IDENT_RE
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

_RULE_KEYS = frozenset({"connector", "operations", "approvers", "max_duration_hours",
                        "max_amount", "currency", "allow_missing_beneficiary",
                        "require_charter", "require_targets"})
_REQUIRED = frozenset({"connector", "operations", "approvers", "max_duration_hours",
                       "max_amount"})
_TOP_KEYS = frozenset({"schema", "rules"})


class StandingPolicyError(ValueError):
    """Bloc `standing_approvals` du canon illisible : toujours une raison lisible."""


@dataclass(frozen=True)
class Rule:
    """Une règle par connecteur et opérations, pour des approbateurs nommés."""

    connector: str
    operations: tuple[str, ...]
    approvers: tuple[str, ...]
    max_duration: int                    # secondes, entre iat et until
    max_amount: int                      # unités mineures
    currency: str | None
    allow_missing_beneficiary: bool = False
    require_charter: bool = True
    require_targets: bool = False

    def matches(self, approver: str, connector: str, operation: str) -> bool:
        return (self.connector == connector and operation in self.operations
                and approver in self.approvers)


@dataclass(frozen=True)
class Policy:
    """Les règles du canon ; vide = aucune approbation permanente acceptée."""

    rules: tuple[Rule, ...] = field(default_factory=tuple)
    schema: str = SCHEMA

    def __bool__(self) -> bool:
        return bool(self.rules)

    def matches(self, approver: str, connector: str, operation: str) -> list[Rule]:
        return [rule for rule in self.rules
                if rule.matches(approver, connector, operation)]

    def max_duration(self) -> int:
        """La plus longue durée que le canon permet, en secondes (0 sans règle).

        #7684 : c'est le canon qui fixe le plafond d'un grant, pas la CLI ;
        le défaut de `receipts.Policy` (30 jours) ne doit pas refuser un grant
        que le canon autorise. La règle applicable est ensuite vérifiée
        opération par opération (`check`), la plus stricte gagnant.
        """
        return max((rule.max_duration for rule in self.rules), default=0)

    def check(self, *, approver: str, standing: dict, iat: int) -> str | None:
        """None si le grant demandé tient sous le plafond, sinon la raison.

        Chaque opération du reçu doit être permise par au moins une règle, et
        le reçu doit tenir sous CHACUNE des règles qui la permettent : la plus
        stricte gagne. Le bénéficiaire absent n'est accepté que si toutes les
        règles applicables l'autorisent explicitement.
        """
        connector = standing.get("connector")
        operations = standing.get("operations") or []
        until = standing.get("until")
        if not isinstance(connector, str) or not operations:
            return "standing approval without connector or operations"
        if not isinstance(until, int) or isinstance(until, bool):
            return "standing approval without an integer until"
        if iat <= 0 or until <= iat:
            return "standing approval: until must be after iat"
        duration = until - iat
        amount = standing.get("max_amount")
        currency = standing.get("currency")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            return "standing approval: max_amount must be a non-negative integer"
        beneficiary = standing.get("beneficiary")
        targets = standing.get("targets") or []
        charter_line = standing.get("charter_line") or ""
        charter_version = standing.get("charter_version") or ""
        applicable: list[Rule] = []
        for operation in operations:
            rules = self.matches(approver, connector, operation)
            if not rules:
                return ("no standing-approval rule of the canon allows %s %s by %s"
                        % (connector, operation, approver))
            applicable.extend(rule for rule in rules if rule not in applicable)
        for rule in applicable:
            if not rule.allow_missing_beneficiary and not beneficiary:
                return ("a standing approval must name its beneficiary "
                        "(the canon does not allow a missing beneficiary)")
            if rule.require_charter and not (charter_line and charter_version):
                return ("a standing approval must cite its charter line and version")
            if rule.require_targets and not targets:
                return "a standing approval must bound its target"
            if duration > rule.max_duration:
                return ("standing approval too long for %s %s: %ss > %ss"
                        % (connector, ",".join(rule.operations), duration,
                           rule.max_duration))
            if amount > rule.max_amount:
                return ("standing approval above the canon cap for %s %s: %s > %s"
                        % (connector, ",".join(rule.operations), amount,
                           rule.max_amount))
            if amount > 0:
                if rule.currency is None:
                    return ("the canon allows no amount for %s %s"
                            % (connector, ",".join(rule.operations)))
                if currency != rule.currency:
                    return ("standing approval currency %r: %s expected for %s %s"
                            % (currency, rule.currency, connector,
                               ",".join(rule.operations)))
            elif currency is not None and rule.currency is not None \
                    and currency != rule.currency:
                return ("standing approval currency %r: %s expected for %s %s"
                        % (currency, rule.currency, connector, ",".join(rule.operations)))
        return None

    def describe(self) -> list[dict]:
        """Vue lisible (CLI, tests) : une entrée par règle."""
        return [{"connector": rule.connector, "operations": list(rule.operations),
                 "approvers": list(rule.approvers),
                 "max_duration_hours": rule.max_duration // 3600,
                 "max_amount": rule.max_amount, "currency": rule.currency,
                 "allow_missing_beneficiary": rule.allow_missing_beneficiary,
                 "require_charter": rule.require_charter,
                 "require_targets": rule.require_targets} for rule in self.rules]


def _int(value, where: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise StandingPolicyError("%s: integer >= %d expected" % (where, minimum))
    if maximum is not None and value > maximum:
        raise StandingPolicyError("%s: at most %d expected" % (where, maximum))
    return value


def _bool(value, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise StandingPolicyError("%s: boolean expected" % where)
    return value


def parse(data, *, where: str = "standing_approvals") -> Policy:
    """Lit et valide le bloc `standing_approvals` d'un canon (strict)."""
    if data is None:
        return Policy()
    if not isinstance(data, dict):
        raise StandingPolicyError("%s: mapping expected" % where)
    extra = set(data) - _TOP_KEYS
    if extra:
        raise StandingPolicyError("%s: unknown keys %s" % (where, sorted(extra)))
    if data.get("schema") != SCHEMA:
        raise StandingPolicyError("%s.schema: %r expected" % (where, SCHEMA))
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise StandingPolicyError("%s.rules: non-empty list expected" % where)
    parsed: list[Rule] = []
    for index, raw in enumerate(rules):
        at = "%s.rules[%d]" % (where, index)
        if not isinstance(raw, dict):
            raise StandingPolicyError("%s: mapping expected" % at)
        extra = set(raw) - _RULE_KEYS
        missing = _REQUIRED - set(raw)
        if extra or missing:
            raise StandingPolicyError("%s: missing keys %s, extra keys %s"
                                      % (at, sorted(missing), sorted(extra)))
        connector = raw.get("connector")
        if not isinstance(connector, str) or not _IDENT_RE.fullmatch(connector):
            raise StandingPolicyError("%s.connector: identifier expected" % at)
        operations = raw.get("operations")
        if not isinstance(operations, list) or not operations:
            raise StandingPolicyError("%s.operations: non-empty list expected" % at)
        cleaned = []
        for operation in operations:
            if not isinstance(operation, str) or not _OPERATION_RE.fullmatch(operation):
                raise StandingPolicyError("%s.operations: identifiers expected" % at)
            if operation in cleaned:
                raise StandingPolicyError("%s.operations: duplicate %r" % (at, operation))
            cleaned.append(operation)
        approvers = raw.get("approvers")
        if not isinstance(approvers, list) or not approvers:
            raise StandingPolicyError("%s.approvers: non-empty list expected" % at)
        for approver in approvers:
            if not isinstance(approver, str) or not _APPROVER_RE.fullmatch(approver):
                raise StandingPolicyError("%s.approvers: human:<id> expected, not %r"
                                          % (at, approver))
        if len(set(approvers)) != len(approvers):
            raise StandingPolicyError("%s.approvers: duplicate approver" % at)
        duration = _int(raw.get("max_duration_hours"), "%s.max_duration_hours" % at,
                        minimum=1, maximum=MAX_DURATION_HOURS)
        amount = _int(raw.get("max_amount"), "%s.max_amount" % at)
        currency = raw.get("currency")
        if currency is not None:
            if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
                raise StandingPolicyError("%s.currency: ISO 4217 code or null expected" % at)
            if amount == 0:
                raise StandingPolicyError("%s.currency: no currency without an amount" % at)
        elif amount > 0:
            raise StandingPolicyError("%s.currency: required with a non-zero amount" % at)
        parsed.append(Rule(
            connector=connector, operations=tuple(cleaned), approvers=tuple(approvers),
            max_duration=duration * 3600, max_amount=amount, currency=currency,
            allow_missing_beneficiary=_bool(raw.get("allow_missing_beneficiary"),
                                            "%s.allow_missing_beneficiary" % at, False),
            require_charter=_bool(raw.get("require_charter"),
                                  "%s.require_charter" % at, True),
            require_targets=_bool(raw.get("require_targets"),
                                  "%s.require_targets" % at, False)))
    return Policy(rules=tuple(parsed))


def from_federation(federation) -> Policy:
    """Le bloc `extensions.ameesh.standing_approvals` ; absent = politique vide.

    Le schéma OKF Federation n'admet aucune clé libre à la racine : un bloc
    `standing_approvals` de premier niveau est refusé nommément, jamais lu en
    silence (même défaut que #7630). Un conteneur `extensions` ou
    `extensions.ameesh` mal formé est refusé, pas interprété comme une absence.
    """
    if not isinstance(federation, dict):
        return Policy()
    if "standing_approvals" in federation:
        raise StandingPolicyError(
            "standing_approvals: top-level key refused by the OKF Federation schema; "
            "declare it under extensions.ameesh.standing_approvals")
    extensions = federation.get("extensions")
    if extensions is None:
        return Policy()
    if not isinstance(extensions, dict):
        raise StandingPolicyError("extensions: mapping expected")
    ameesh = extensions.get("ameesh")
    if ameesh is None:
        return Policy()
    if not isinstance(ameesh, dict):
        raise StandingPolicyError("extensions.ameesh: mapping expected")
    return parse(ameesh.get("standing_approvals"),
                 where="extensions.ameesh.standing_approvals")


def empty() -> Policy:
    return Policy()
