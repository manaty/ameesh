# SPDX-License-Identifier: AGPL-3.0-only
"""Grant d'attach distant (#7119, avatar D1 ; conception 5322 §5.2).

Décision owner Q3 (#213938, option a, signée, approbation #345) :

* **qui** : le `responsible` de l'agent, ou un humain de la liste explicite
  du canon (`avatar.attach_authorized: [human:…]` sur la carte de l'agent).
  Un deputy agit sous sa propre identité : il n'est autorisé que s'il est
  dans la liste (ou le `responsible`). Un agent ne l'est jamais ;
* **preuve** : un reçu `ameesh-receipt/1` de nature `attach`, signé en
  WebAuthn par ameesh-approve (aucune autre façade : une clé d'appareil ne
  signe pas son propre grant), de 5 min au plus, à usage unique (nonce
  consommé), lié à `{agent, host, tenant, device_key}` ;
* **appareil** : `device_key` est le credential_id d'un authentificateur
  `device-es256` ACTIF du même humain, déclaré par le canon de l'agent ; le
  poste prouve qu'il détient cette clé en signant
  `"ameesh-attach-device/2\\0" ‖ challenge du reçu ‖ hash du transcript`
  (ES256), le hash étant celui du canal E2E (D2). La preuve est donc liée à
  CETTE session chiffrée : un relais qui terminerait deux canaux distincts
  ne peut pas la transposer.

`redeem` est la porte de l'hôte de l'agent : refus par défaut, chaque issue
(grant accepté ou refus, avec son code stable) est écrite dans
`attach_grant_events` (0574). Le grant accepté n'ouvre rien à lui seul : le
relais (D2) et la session (D3) le demandent avant de relier un terminal.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import time
from dataclasses import dataclass

from . import receipts, storage
from .db import Db, DbError

#: durée maximale d'un grant (décision Q3 : 5 min)
GRANT_TTL = 300
DEVICE_DOMAIN = b"ameesh-attach-device/2\x00"
#: clé de la carte d'agent qui porte la liste explicite
CARD_SECTION = "avatar"
CARD_LIST = "attach_authorized"
AUDIT_LIMIT = 2000

# codes propres au grant (les autres viennent de receipts : format, expired,
# replay, bad_signature, attach_mismatch…)
OK = receipts.OK
UNKNOWN_AGENT = "unknown_agent"
NOT_AUTHORIZED = "not_authorized"
DEVICE = "device_not_enrolled"
DEVICE_PROOF = "device_proof"
TTL = receipts.TTL
AUDIT = "audit_unavailable"

_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def new_attach_id() -> str:
    """`att_` + 26 caractères (130 bits aléatoires, base32 Crockford)."""
    value = int.from_bytes(os.urandom(17), "big") >> 6
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "att_" + "".join(reversed(chars))


def canon_key(canon) -> str:
    """Identifiant de registre du canon : '' pour le canon par défaut (L44)."""
    return "" if getattr(canon, "is_default", True) else (canon.id or "")


def authorized(canon, agent_name: str) -> tuple[list[str], list[str]]:
    """Les humains admis à s'attacher à `agent_name`, et les constats.

    Le `responsible` résolu (un Member de CE canon), puis chaque entrée de
    `avatar.attach_authorized` qui résout aussi ; une entrée qui ne résout
    pas (agent, inconnue, mal formée) est écartée et signalée — jamais
    admise. Les deputies du responsable ne sont PAS ajoutés : un deputy doit
    figurer dans la liste.
    """
    agent = canon.agent(agent_name) if canon is not None else None
    if agent is None:
        return [], ["agent %s unknown to the canon" % agent_name]
    humans: list[str] = []
    problems: list[str] = []
    responsible = canon.resolve_human(agent.responsible)
    if responsible:
        humans.append(responsible)
    elif agent.responsible:
        problems.append("responsible %r does not resolve to a Member" % agent.responsible)
    section = (agent.fiche.data or {}).get(CARD_SECTION)
    listed = section.get(CARD_LIST) if isinstance(section, dict) else None
    if section is not None and not isinstance(section, dict):
        problems.append("%s: mapping expected" % CARD_SECTION)
    if listed is not None and not isinstance(listed, list):
        problems.append("%s.%s: list expected" % (CARD_SECTION, CARD_LIST))
        listed = None
    for entry in listed or ():
        human = canon.resolve_human(entry) if isinstance(entry, str) else None
        if human is None:
            problems.append("%s.%s: %r is not a Member (ignored)"
                            % (CARD_SECTION, CARD_LIST, entry))
        elif human not in humans:
            humans.append(human)
    return humans, problems


def check_authorized(canon, agent_name: str, human: str) -> tuple[str, str]:
    """('', '') si `human` peut s'attacher à `agent_name`, sinon (code, raison)."""
    if canon is None or canon.agent(agent_name) is None:
        return UNKNOWN_AGENT, "agent %s unknown to the canon" % agent_name
    humans, _problems = authorized(canon, agent_name)
    if human not in humans:
        return NOT_AUTHORIZED, (
            "%s is neither the responsible of %s nor in its %s.%s list"
            % (human, agent_name, CARD_SECTION, CARD_LIST))
    return "", ""


def build_request(*, approver: str, agent: str, host: str, tenant: str, device_key: str,
                  requested_by: str, ttl: int = GRANT_TTL,
                  now: float | None = None) -> tuple[dict, str]:
    """La demande signable d'un grant (sans `decision`) et son résumé.

    `summary_digest` est celui du résumé rendu ici (`summary_text`) ; la
    demande est validée par receipts.check_request ; `ttl` est borné par
    GRANT_TTL.
    """
    if type(ttl) is not int or not 0 < ttl <= GRANT_TTL:
        raise receipts.ReceiptError("attach grant: duration between 1 and %d s" % GRANT_TTL)
    iat = int(time.time() if now is None else now)
    request = {
        "v": receipts.REQUEST_VERSION,
        "approver": approver,
        "attach_id": new_attach_id(),
        "agent": agent,
        "host": host,
        "tenant": tenant,
        "device_key": device_key,
        "summary_digest": "",
        "requested_by": requested_by,
        "nonce": receipts.b64u_encode(os.urandom(16)),
        "iat": iat,
        "exp": iat + ttl,
    }
    summary = summary_text(request)
    request["summary_digest"] = summary_digest(summary)
    receipts.check_request(dict(request, decision="approve"))
    return request, summary


def device_message(challenge: bytes, transcript_hash: bytes) -> bytes:
    """Ce que signe la clé d'appareil du poste : le challenge du reçu et le
    hash du transcript E2E, sous un domaine propre (jamais réutilisable comme
    reçu `device-es256` ni hors de cette session)."""
    if (not isinstance(transcript_hash, (bytes, bytearray))
            or len(transcript_hash) != 32):
        raise receipts.ReceiptError(
            "attach device proof: a 32-byte transcript hash is required")
    return DEVICE_DOMAIN + bytes(challenge) + bytes(transcript_hash)


@dataclass(frozen=True)
class Grant:
    """Issue de `redeem` ; `ok` seulement si tout est vérifié, le nonce
    consommé et le grant journalisé."""

    ok: bool
    code: str
    reason: str
    human: str = ""
    agent: str = ""
    host: str = ""
    tenant: str = ""
    device_key: str = ""
    attach_id: str = ""
    exp: int = 0
    event_id: int | None = None

    def __bool__(self) -> bool:
        return self.ok

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _record(db: Db, grant: Grant, *, nonce: str, checked_by: str) -> int | None:
    if grant.ok:
        raise ValueError("a granted attach is written by record_granted")
    return storage.of(db).attach_grants.record(
        outcome="granted" if grant.ok else "refused", code=grant.code,
        reason=grant.reason[:2000], human=grant.human[:200], agent=grant.agent[:200],
        host=grant.host[:253], tenant=grant.tenant[:200],
        device_key=grant.device_key[:1024], attach_id=grant.attach_id[:64],
        nonce=nonce[:128], exp=grant.exp or None, checked_by=checked_by)


def redeem(db: Db, receipt, *, canon, agent: str, host: str, tenant: str, device_key: str,
           device_signature: bytes, transcript_hash: bytes, policy: receipts.Policy,
           checked_by: str, now: float | None = None) -> Grant:
    """Vérifie et CONSOMME un grant d'attach sur l'hôte de l'agent.

    `agent`, `host`, `tenant` sont ceux de l'hôte qui vérifie (jamais lus dans
    le reçu) ; `device_key` et `device_signature` sont présentés par le poste.
    `transcript_hash` est celui du canal E2E où la preuve a circulé (D2) : le
    poste l'a signé avec le challenge, donc la preuve ne vaut que pour cette
    session. `policy` donne RP ID et origines ; la façade est forcée à
    `webauthn`, la durée à GRANT_TTL et le canon à celui de l'agent. Refus par
    défaut : tout écart rend un Grant refusé, journalisé avec son code.
    """
    base = {"agent": agent, "host": host, "tenant": tenant, "device_key": device_key}
    nonce = ""
    device_id = 0

    def finish(grant: Grant) -> Grant:
        try:
            if grant.ok:
                # la clé d'appareil est relue sous verrou, dans la transaction
                # qui écrit le grant : une révocation validée entre sa première
                # lecture et ici n'est jamais contournée
                state, event_id = storage.of(db).attach_grants.record_granted(
                    device_authenticator_id=device_id, human=grant.human,
                    agent=grant.agent, host=grant.host, tenant=grant.tenant,
                    device_key=grant.device_key, attach_id=grant.attach_id, nonce=nonce,
                    exp=grant.exp, reason=grant.reason[:2000], checked_by=checked_by)
                if state == "device_revoked":
                    return finish(dataclasses.replace(
                        grant, ok=False, code=DEVICE,
                        reason="device %s revoked during verification"
                               % grant.device_key[:16]))
                if state != "ok":
                    return finish(dataclasses.replace(
                        grant, ok=False, code=receipts.REPLAY,
                        reason="grant %s already used" % grant.attach_id))
            else:
                event_id = _record(db, grant, nonce=nonce, checked_by=checked_by)
        except DbError as exc:
            deadline = receipts._deadline_error(exc) if grant.ok else None
            if deadline is not None:
                # échéance franchie pendant l'attente du verrou : rien d'écrit
                return finish(dataclasses.replace(
                    grant, ok=False, code=deadline.code, reason=str(deadline)))
            # grant déjà consommé : sans trace, il ne vaut rien (fail closed)
            return dataclasses.replace(
                grant, ok=False, code=AUDIT,
                reason="attach journal unavailable (%s): %s"
                       % (type(exc).__name__, grant.reason))
        return dataclasses.replace(grant, event_id=event_id)

    def refuse(code: str, reason: str, **fields) -> Grant:
        return finish(Grant(False, code, reason, **dict(base, **fields)))

    try:
        parsed = receipts.parse_receipt(receipt)
    except receipts.ReceiptError as exc:
        return refuse(receipts.FORMAT, str(exc))
    request = parsed.request
    if parsed.kind != "attach":
        return refuse(receipts.KIND, "receipt of kind %s, attach expected" % parsed.kind,
                      human=request.get("approver", ""))
    nonce = request["nonce"]
    seen = {"human": request["approver"], "attach_id": request["attach_id"],
            "exp": request["exp"]}
    if request["exp"] - request["iat"] > GRANT_TTL:
        return refuse(TTL, "grant valid %d s, maximum %d s"
                      % (request["exp"] - request["iat"], GRANT_TTL), **seen)
    code, reason = check_authorized(canon, agent, request["approver"])
    if code:
        return refuse(code, reason, **seen)

    registry = canon_key(canon)
    try:
        row, code, reason = receipts.active_authenticator(
            db, request["approver"], "device-es256", device_key, registry)
    except DbError as exc:
        return refuse(AUDIT, "trust registry unreachable (%s)" % type(exc).__name__, **seen)
    if row is None:
        return refuse(DEVICE, "device %s: %s" % (device_key[:16], reason), **seen)
    device_id = int(row["id"])
    try:
        key = receipts.parse_public_key("device-es256", receipts.b64u_decode(
            row["public_key"], "stored device key"))
    except receipts.ReceiptError as exc:
        return refuse(DEVICE, "device key unusable: %s" % exc, **seen)
    if not isinstance(transcript_hash, (bytes, bytearray)) or len(transcript_hash) != 32:
        return refuse(DEVICE_PROOF, "transcript hash: 32 bytes expected", **seen)
    if not isinstance(device_signature, (bytes, bytearray)) or not key.verify(
            device_message(parsed.challenge, transcript_hash), bytes(device_signature)):
        return refuse(DEVICE_PROOF, "the station does not prove possession of device %s "
                      "for this transcript" % device_key[:16], **seen)

    gate = dataclasses.replace(policy, allow_facades=frozenset({"webauthn"}),
                               max_ttl=GRANT_TTL, canon=registry)
    expected = dict(base, attach_id=request["attach_id"], exp=request["exp"])
    try:
        verdict = receipts.verify_receipt(
            db, receipt, gate, kind="attach", expected_attach=expected,
            expect_decision="approve", consume_by=checked_by, now=now)
    except DbError as exc:
        return refuse(AUDIT, "trust registry unreachable (%s)" % type(exc).__name__, **seen)
    if not verdict.ok:
        return refuse(verdict.code, verdict.reason, **seen)
    return finish(Grant(True, OK, "attach granted to %s on %s for %s"
                        % (request["approver"], agent, device_key[:16]),
                        **dict(base, **seen)))


def audit(db: Db, *, agent: str | None = None, limit: int = 50) -> list[dict]:
    """Les dernières issues de grant (acceptés et refus)."""
    return storage.of(db).attach_grants.recent(agent=agent,
                                               limit=max(1, min(limit, AUDIT_LIMIT)))


def summary_text(request: dict) -> str:
    """Résumé déterministe d'une demande (ce que l'humain lit et signe)."""
    lines = [
        ("Decision", "remote attach to an agent session"),
        ("Agent", request["agent"]),
        ("Agent host", request["host"]),
        ("Tenant", request["tenant"] or "(dedicated host)"),
        ("Device", request["device_key"]),
        ("Approver", request["approver"]),
        ("Requested by", request["requested_by"]),
        ("Valid until", time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                      time.gmtime(request["exp"]))),
    ]
    return "ameesh-attach-summary/1\n" + "".join("%s: %s\n" % pair for pair in lines)


def summary_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
