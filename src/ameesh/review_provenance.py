# SPDX-License-Identifier: AGPL-3.0-only
"""#1465 — le fournisseur exact d'origine d'un commit et d'un jalon.

Contrat importable pour le lecteur du train (#1467/#2681,
`review_authors` et `merge_queue.review_gate`). Pour chaque sujet, il rend un reçu aux champs de
`train_review.Receipt` — `subject`, `commit`, `actor`, `session`, `turn`,
`harness`, `record`, `milestone` — ou `None` quand la source manque.

    from ameesh import review_provenance as prov
    recu = prov.receipt_for_commit(db, sha)          # ou None (inconnu)
    recu = prov.receipt_for_milestone(db, jalon_id)  # ou None (inconnu)

Ce que le fournisseur ne fait JAMAIS :

* deviner un tour par proximité temporelle (`turn_costs.recorded_at` voisin),
  par le harnais déclaré au registre, ou par le nom d'auteur Git : ce ne sont
  pas des liens, mesh-design 15/10 03:53 ;
* compléter une preuve partielle : une ligne `provenance_receipts` sans son
  fait (`work_item_milestones`) est une origine inconnue ;
* réécrire une preuve : la première écriture fait foi, une divergence d'identité
  est une erreur (`ProvenanceError`), pas un remplacement silencieux.

La preuve est écrite à l'instant du fait (migration 0445) : le commit par
`ameesh provenance record --commit` (hook post-commit), le jalon dans la même
transaction que `work_item_milestones`. Le `turn` est l'identifiant du tour
managé (`AMEESH_TURN_ID`), la `session` celle du registre vivant de l'acteur,
et le `harness` celui du tour (`AMEESH_HARNESS`), recontrôlé contre le registre.

Une session de gouvernance **hors tour** (le bureau, canon §10) n'a ni bail ni
`AMEESH_TURN_ID` ; elle est liée à son agent par `ameesh mail bind` (L41,
décision 0030). Sa liaison est une preuve aussi forte qu'un tour tant qu'elle
porte un PID — le processus est alors vérifié comme descendant du harnais lié
(L46/L63) — et que l'agent est `externe` au registre, sans bail vivant, sous le
même harnais que la liaison. Le reçu porte alors `turn = "session:<id>"` : un
tour synthétique qui dit l'origine sans la confondre avec un tour managé. La
paire de hooks Git installée, les commandes `ameesh provenance
intent|created|record` et le commit mécanique de `resolutions.finish_resolution`
suivent ce chemin : un commit de résolution créé dans une session liée a un
reçu exact, et le gel en lit la famille. Sans liaison vérifiable, rien n'est
écrit : le commit reste « origine inconnue », jamais deviné.

Une session de gouvernance peut aussi tourner sur un hôte **sans ameesh** (le
bureau sur `bureau-goal`), d'où elle appelle ameesh par ssh : aucun PID local
n'y est vérifiable, et la liaison de session ne peut pas la couvrir (#4485).
Hors tour, une clé `owner` (le propriétaire, ou son délégataire du §10) peut
alors **signer une attestation du commit** : SHA complet, acteur, session,
harnais, hôte et lot. `record --signed` vérifie la signature, la consomme et
écrit le reçu (`turn = "signed:<approbation>"`). Ce n'est pas une preuve
d'ascendance : c'est une attestation d'autorité, et le garde reste le même —
pas de signature valide, pas de reçu. L'acteur doit être `externe` au registre,
sans bail vivant, sous le harnais que le registre lui connaît.

Un commit n'est enregistrable que s'il a été **créé dans ce tour** : le hook
`pre-commit` écrit avant le commit une attestation (identité du tour, HEAD et
arbre de départ, aléa), puis le hook `post-commit` écrit le **fait de création**
de l'objet né (`sha`, arbre, parents), repris de la dernière entrée de reflog de
HEAD — qui n'est un `git commit` que si l'objet vient de naître. `record_commit`
exige les deux, liés par le même aléa, et les consomme. Un objet seulement
restauré par `reset` ou `checkout`, même committé par le même agent, ne produit
pas de fait de création : son origine reste inconnue (écarts cx3, 15/10 04:37,
puis B1 cx2/Codex, 15/10 12:12).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess

from . import storage

#: un identifiant de commit complet : SHA-1 (40) ou SHA-256 (64)
FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
#: une abréviation Git citée par un jalon (au moins 7 caractères)
ABBREV = re.compile(r"[0-9a-f]{7,64}")

#: préfixe du `turn` d'un reçu écrit par une session liée hors tour : le reçu
#: dit ainsi qu'il ne vient pas d'un tour managé (`AMEESH_TURN_ID`).
SESSION_TURN_PREFIX = "session:"

#: #4485 : préfixe du `turn` d'un reçu écrit hors tour sur une attestation
#: signée par une clé `owner` — l'approbation consommée le suit.
SIGNED_TURN_PREFIX = "signed:"
#: action et type d'artefact de l'approbation qui porte l'attestation signée.
SIGNED_ACTION = "provenance"
SIGNED_KIND = "commit"
#: version du texte canonique signé ; la changer invalide les anciennes preuves.
SIGNED_VERSION = 1
#: un nom d'hôte dans l'attestation : simple et borné (jamais une option).
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")


class ProvenanceError(Exception):
    """Preuve incohérente ou identité d'écriture incomplète : on refuse."""


def full_sha(value) -> str:
    """Le commit complet, en minuscules ; refuse tout le reste."""
    text = str(value or "").strip().lower()
    if not FULL_SHA.fullmatch(text):
        raise ProvenanceError("full commit id expected, got %r" % (value,))
    return text


def subject_of_commit(value) -> str:
    return "commit:" + full_sha(value)


def subject_of_milestone(milestone_id) -> str:
    try:
        number = int(milestone_id)
    except (TypeError, ValueError) as exc:
        raise ProvenanceError("milestone id expected, got %r" % (milestone_id,)) from exc
    if number <= 0:
        raise ProvenanceError("milestone id expected, got %r" % (milestone_id,))
    return "milestone:%d" % number


def _receipt(row, *, commit: str, milestone) -> dict:
    """Le reçu aux champs de `train_review.Receipt`."""
    return {
        "subject": row["subject"],
        "commit": commit,
        "actor": row["actor"],
        "session": row["session"],
        "turn": row["turn"],
        "harness": row["harness"],
        # la ligne elle-même est le reçu exact, jamais une note libre
        "record": "provenance_receipts:%s" % row["subject"],
        "milestone": milestone,
    }


def receipt_for_commit(db, sha) -> dict | None:
    """Le reçu exact du commit `sha`, ou None si son origine est inconnue.

    `sha` doit être complet : le train qualifie des commits complets, et une
    abréviation ne désigne un commit qu'en présence du dépôt — le fournisseur
    ne l'a pas."""
    subject = subject_of_commit(sha)
    row = storage.of(db).provenance.get(subject)
    if row is None:
        return None
    if row["sha"] != subject[len("commit:"):]:
        # Une ligne existe pour ce sujet mais enregistre un AUTRE commit :
        # ce n'est pas une absence (unknown), c'est une preuve corrompue.
        raise ProvenanceError(
            "%s: proof records commit %s, not the subject" % (subject, row["sha"]))
    return _receipt(row, commit=row["sha"], milestone=None)


def receipt_for_milestone(db, milestone_id) -> dict | None:
    """Le reçu exact du jalon, ou None si son origine est inconnue.

    Le reçu recopie les faits du jalon (`id`, `actor`, `at_ts`, `sha`,
    `verdict`) et porte le commit COMPLET que le `sha` déclaré résout. Une
    incohérence entre la preuve et le fait — acteur différent, abréviation qui
    ne résout pas le commit enregistré — est refusée, pas rabotée."""
    subject = subject_of_milestone(milestone_id)
    row = storage.of(db).provenance.milestone(subject)
    if row is None:
        return None
    # #4299: after a rename the milestone names the new primary and the
    # immutable receipt the old spelling, now an alias of the same identity.
    if (str(row["mark_actor"]).strip().casefold() != str(row["actor"]).strip().casefold()
            and not row.get("same_identity")):
        raise ProvenanceError(
            "%s: proof actor %r differs from the milestone actor %r"
            % (subject, row["actor"], row["mark_actor"]))
    mark_sha = str(row["mark_sha"] or "").strip().lower()
    if mark_sha:
        if not ABBREV.fullmatch(mark_sha) or not row["sha"].startswith(mark_sha):
            raise ProvenanceError(
                "%s: declared sha %r does not resolve the recorded commit %s"
                % (subject, row["mark_sha"], row["sha"]))
    return _receipt(row, commit=row["sha"], milestone={
        "id": row["milestone_id"],
        "actor": row["mark_actor"],
        "at_ts": row["mark_at_ts"],
        "sha": row["mark_sha"],
        "verdict": row["verdict"],
    })


def _binding_hint(db, cfg, actor: str) -> str:
    """La voie à suivre quand une liaison existe mais ne prouve pas le processus.

    `by_ancestry` ignore une liaison sans PID : sans elle, le refus générique
    ne dirait pas qu'un `--pid` manque. On nomme les liaisons actives de cet
    hôte (celles de l'acteur quand il est connu), jamais leur session."""
    try:
        rows = storage.of(db).session_bindings.listing(
            host=cfg.host, agent=actor or None)
    except Exception:  # noqa: BLE001 - indice seulement, jamais une erreur
        return ""
    without = [row for row in rows or () if not row.get("pid")]
    if not without:
        return ""
    names = ", ".join(sorted({str(row.get("agent") or "?") for row in without})[:3])
    return ("; %d active binding(s) on this host carry no pid (%s): "
            "rebind with --pid" % (len(without), names))


def _bound_session(db, *, actor: str = "", cfg=None) -> dict:
    """L'identité d'une session liée (`ameesh mail bind`), ou un refus nommé.

    La liaison est lue par l'ascendance du processus : elle doit porter un PID
    (contrôle L46/L63 de `session_bindings.by_ancestry`) et être active sur cet
    hôte. L'agent doit être `externe` au registre, sans bail vivant, et le
    harnais de la liaison doit être celui que le registre lui connaît : deux
    sources qui divergent ne prouvent rien. Le `turn` rendu est synthétique
    (`session:<id>`) — un reçu de session ne se fait jamais passer pour un tour
    managé.

    #4485 : la liaison est lue **même si `AGENT_MAIL_NAME` est posé**. Ce nom
    annoncé ne prouve rien à lui seul ; quand il est fourni (`actor`), il doit
    être celui de la liaison, sinon on refuse. C'était l'écart du bureau : un
    shell portant encore le nom annoncé masquait une liaison valide."""
    from . import config as config_mod
    from . import identity as identity_mod
    from . import registry
    cfg = cfg or getattr(db, "cfg", None) or config_mod.load()
    try:
        binding = identity_mod.resolve_session_binding(cfg, db)
    except Exception as exc:  # noqa: BLE001 - liaison illisible : origine inconnue
        raise ProvenanceError(
            "no managed turn (AMEESH_TURN_ID): outside a turn, and the session "
            "binding cannot be read (%s)" % " ".join(str(exc).split())[:160]) from exc
    if binding.source != "session":
        raise ProvenanceError(
            "no managed turn (AMEESH_TURN_ID): outside a turn, and %s%s"
            % (binding.reason or "no session binding (ameesh mail bind)",
               _binding_hint(db, cfg, actor)))
    if not binding.ok:
        raise ProvenanceError(
            "no managed turn (AMEESH_TURN_ID): outside a turn, and the session "
            "binding of %s does not hold (%s)"
            % (binding.name or "?", binding.reason or "not bound"))
    row = binding.session or {}
    name = str(binding.name or "").strip()
    session = str(row.get("session_id") or "").strip()
    harness = str(row.get("harness") or "").strip().lower()
    if not name or not session or not harness:
        raise ProvenanceError(
            "session binding is incomplete (agent, session and harness are required)")
    if actor and str(actor).strip() != name:
        raise ProvenanceError(
            "actor %r is not the agent of this bound session (%r)"
            % (actor, name))
    if not row.get("pid"):
        raise ProvenanceError(
            "session %s %s of %s carries no pid: the bound process is not "
            "proven (re-bind with --pid)" % (harness, session, name))
    reg = registry.get(db, name) or {}
    if not reg:
        raise ProvenanceError(
            "session binding names %s, absent from the registry" % name)
    if str(reg.get("mode") or "execute").strip() != "externe":
        # Liste d'admission, comme le registre (`mode` absent = `execute`).
        raise ProvenanceError(
            "%s is an execute agent (runner-driven): a session binding cannot "
            "prove a family outside a managed turn" % name)
    declared = str(reg.get("harness") or "").strip().lower()
    if declared and declared != harness:
        raise ProvenanceError(
            "agent %s is registered under %r but its session binding declares "
            "%r: rebind the session or correct the registry"
            % (name, declared, harness))
    return {"actor": name, "turn": SESSION_TURN_PREFIX + session,
            "harness": harness, "session": session}


def _identity(environ=None, *, actor: str = "", db=None, cfg=None) -> dict:
    """L'identité brute d'écriture d'un fait, sans contrôle de famille.

    Sous tour managé (`AMEESH_TURN_ID` posé), l'identité est celle du runner :
    `AGENT_MAIL_NAME` et `AMEESH_HARNESS`, jamais un nom choisi par le dépôt.
    Hors tour, et seulement si une base est fournie, une session liée peut
    attester la sienne (`_bound_session`) ; sans liaison, on refuse.

    Les hooks `pre-commit`/`post-commit` lisent la base seulement hors tour
    (ils restent muets et non bloquants si elle manque) ; c'est l'écriture de
    la preuve (`capture`, avec les familles du canon) qui refuse un harnais
    inconnu (#2664).

    `actor` n'est qu'une **attente** : quand il est fourni, il doit être celui
    du tour (`AGENT_MAIL_NAME`, posé par le runner) ou de la session liée — un
    tour ne signe pas la preuve d'un autre agent (écart relevé par la relecture
    cx3, 15/10 04:27).

    #4485 : hors tour, un nom annoncé (`AGENT_MAIL_NAME`) ne dispense pas de
    lire la liaison de session ; il doit au contraire **être couvert** par
    elle. Un shell qui a gardé le nom du bureau masquait une liaison valide et
    faisait échouer toute provenance."""
    env = os.environ if environ is None else environ
    turn_actor = str(env.get("AGENT_MAIL_NAME") or "").strip()
    who = str(actor or turn_actor).strip()
    turn = str(env.get("AMEESH_TURN_ID") or "").strip()
    harness = str(env.get("AMEESH_HARNESS") or "").strip().lower()
    if not turn:
        announced = bool(turn_actor or env.get("AMEESH_RUNNER_ID")
                         or env.get("AGENT_MESH_RUNNER_ID"))
        if db is not None:
            try:
                # Le nom annoncé (s'il existe) doit être celui de la liaison :
                # une liaison valide le couvre, une autre identité le refuse.
                return _bound_session(db, actor=who, cfg=cfg)
            except ProvenanceError as exc:
                if announced:
                    raise ProvenanceError(
                        "no managed turn (AMEESH_TURN_ID): outside a turn, an "
                        "agent identity is announced but nothing proves it (%s)"
                        % exc) from exc
                raise
        if announced:
            # Un nom d'agent sans tour n'est pas une preuve, et sans base la
            # voie « session liée » ne peut pas le couvrir.
            raise ProvenanceError(
                "no managed turn (AMEESH_TURN_ID): outside a turn, an agent "
                "identity is announced but nothing proves it")
        raise ProvenanceError("no managed turn (AMEESH_TURN_ID): outside a turn")
    if not who:
        raise ProvenanceError("no agent identity (AGENT_MAIL_NAME)")
    if turn_actor and who != turn_actor:
        raise ProvenanceError(
            "actor %r is not the agent of this turn (%r)" % (who, turn_actor))
    return {"actor": who, "turn": turn, "harness": harness}


def compiled_families(db) -> tuple:
    """Les familles du canon compilé, ou une `ProvenanceError` nommée (#2664).

    Le harnais d'un reçu se contrôle contre le canon, au dernier commit audité
    de l'hôte (`review_lot.compiled_families`) ; aucun repli local, et un
    canon illisible refuse l'écriture de la preuve avec son diagnostic."""
    from . import review_lot
    try:
        return review_lot.compiled_families(db)
    except review_lot.FamiliesError as exc:
        raise ProvenanceError("model families unreadable: %s" % exc) from exc


def capture(environ=None, *, actor: str = "", families, db=None, cfg=None) -> dict:
    """L'identité d'écriture d'un fait, contrôlée contre les familles du canon.

    Refuse hors tour (`AMEESH_TURN_ID` absent) sans session liée vérifiable, ou
    sur un harnais que le canon ne déclare pas : une preuve sans identité ou
    sans famille n'est pas une preuve. `families` vient de `compiled_families`
    (#2664) ; une liste vide refuse (aucune famille n'est supposée). `db` est
    requis pour la voie « session liée » ; sans lui, le comportement d'avant
    (tour only) est conservé."""
    ident = _identity(environ, actor=actor, db=db, cfg=cfg)
    if not families:
        raise ProvenanceError("no model families: the canon cannot be read")
    if ident["harness"] not in families:
        raise ProvenanceError("unknown harness %r (expected %s)"
                              % (ident["harness"], ", ".join(sorted(families))))
    return ident


# --------------------------------------------------------------------------
# #4485 : provenance signée d'un auteur hors runner, sans ameesh local
# --------------------------------------------------------------------------
#
# Un commit créé sur un hôte où ameesh n'est pas installé (le bureau sur
# bureau-goal) n'a ni tour managé ni PID local à faire vérifier : la liaison de
# session de #3985 ne peut pas le couvrir. La preuve est alors une **attestation
# signée par une clé `owner`** (le propriétaire ou son délégataire, canon §10).
# Elle couvre le SHA complet, l'acteur, la session, le harnais, l'hôte et le lot.
# `record_signed_commit` vérifie la signature, la consomme et écrit le reçu ; le
# `turn` synthétique `signed:<approbation>` dit qu'il ne vient pas d'un tour
# managé. Sans signature valide, rien n'est écrit.

def signed_statement(*, commit, actor: str, session: str, harness: str,
                     host: str, lot=None) -> str:
    """Le texte canonique que la clé `owner` signe pour un commit (#4485).

    Le SHA complet lie le contenu ; acteur, session, harnais et hôte disent
    l'origine ; le lot (facultatif) rattache la preuve sans la conditionner.
    JSON trié sans espace : la même entrée rend toujours les mêmes octets, donc
    la même empreinte signée. `version` permet d'invalider un ancien format.
    """
    fields = {
        "version": SIGNED_VERSION,
        "commit": full_sha(commit),
        "actor": str(actor or "").strip(),
        "session": str(session or "").strip(),
        "harness": str(harness or "").strip().lower(),
        "host": str(host or "").strip(),
        "lot": int(lot) if lot is not None else 0,
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def signed_digest(**fields) -> str:
    """L'empreinte sha256 de `signed_statement` : la valeur que l'approbation signe."""
    return hashlib.sha256(
        signed_statement(**fields).encode("utf-8")).hexdigest()


def _signed_identity(db, *, actor, session, harness, host, families) -> dict:
    """L'identité d'un auteur signé, contrôlée comme une session externe.

    L'acteur doit être au registre en mode `externe` (liste d'admission : mode
    absent = `execute`), sans bail vivant — sinon le tour managé est la preuve,
    pas une signature — et sous le harnais que le registre lui connaît. Le
    harnais doit être une famille du canon. L'hôte est un nom borné : il ne
    prouve rien à lui seul, il est couvert par la signature avec le reste."""
    from . import agent_names
    from . import registry
    from . import session_bindings
    name = agent_names.resolve(str(actor or "").strip(), db=db,
                               operation="review_provenance.signed")
    session = str(session or "").strip()
    harness = str(harness or "").strip().lower()
    host = str(host or "").strip()
    if not (name and session and harness and host):
        raise ProvenanceError(
            "a signed receipt requires an actor, a session, a harness and a host")
    if not session_bindings.SESSION_RE.match(session):
        raise ProvenanceError("invalid session id: %r" % session)
    if harness not in session_bindings.HARNESSES:
        raise ProvenanceError("invalid harness: %r (%s)"
                              % (harness, "|".join(session_bindings.HARNESSES)))
    if not HOST_RE.fullmatch(host):
        raise ProvenanceError("invalid host: %r" % host)
    if not families:
        raise ProvenanceError("no model families: the canon cannot be read")
    if harness not in families:
        raise ProvenanceError("unknown harness %r (expected %s)"
                              % (harness, ", ".join(sorted(families))))
    reg = registry.get(db, name) or {}
    if not reg:
        raise ProvenanceError("signed author %s is absent from the registry" % name)
    if str(reg.get("mode") or "execute").strip() != "externe":
        raise ProvenanceError(
            "%s is an execute agent (runner-driven): a signed statement cannot "
            "prove a family outside a managed turn" % name)
    if session_bindings.lease_holder(db, name):
        raise ProvenanceError(
            "%s holds a live lease (runner-driven): its turns are the proof, "
            "not a signed statement" % name)
    declared = str(reg.get("harness") or "").strip().lower()
    if declared and declared != harness:
        raise ProvenanceError(
            "agent %s is registered under %r but the signed statement declares %r"
            % (name, declared, harness))
    return {"actor": name, "session": session, "harness": harness, "host": host}


def _approval_row(db, approval_id, digest: str) -> dict:
    """L'approbation `approval_id`, si elle couvre exactement `digest`."""
    try:
        wanted = int(approval_id)
    except (TypeError, ValueError) as exc:
        raise ProvenanceError("approval id expected, got %r" % (approval_id,)) from exc
    rows = storage.of(db).approvals.recent(
        action=SIGNED_ACTION, artifact_hash=digest, limit=20)
    for row in rows or ():
        if int(row["id"]) == wanted:
            return row
    raise ProvenanceError(
        "approval #%s does not cover this signed statement (%s)"
        % (wanted, digest[:12]))


def record_signed_commit(db, sha, *, actor: str, session: str, harness: str,
                         host: str, families, lot=None,
                         approval_id: int | None = None,
                         approver: str = "", key_path: str = "",
                         cwd: str | None = None,
                         expires: str | None = None) -> dict:
    """Écrit la provenance signée d'un commit, hors tour et sans PID local.

    #4485 : la preuve n'est pas l'ascendance du processus, mais une attestation
    d'autorité. `key_path` signe une approbation neuve (clé `owner`) et la
    consomme dans le même geste ; `approval_id` consomme une approbation déjà
    signée (elle doit couvrir le même texte canonique). Dans les deux cas, une
    seule écriture : la première preuve du commit fait foi, une identité
    divergente est refusée. Le commit peut être n'importe quel objet du dépôt
    (`cwd`), pas seulement HEAD : il a été créé sur un autre hôte.

    Ne jamais appeler sans `families` issues de `compiled_families` (ou du canon
    épinglé du banc) : une famille supposée n'est pas une preuve."""
    from . import authority, signing
    from . import work as work_mod
    full = resolve_commit(sha, cwd=cwd)
    if full is None:
        raise ProvenanceError(
            "commit %r does not resolve to a commit in %s (fetch it first)"
            % (sha, cwd or os.getcwd()))
    ident = _signed_identity(
        db, actor=actor, session=session, harness=harness, host=host,
        families=families)
    statement = signed_statement(
        commit=full, actor=ident["actor"], session=ident["session"],
        harness=ident["harness"], host=ident["host"], lot=lot)
    digest = hashlib.sha256(statement.encode("utf-8")).hexdigest()
    subject = subject_of_commit(full)
    existing = storage.of(db).provenance.get(subject)
    if existing is not None:
        if (existing["actor"] == ident["actor"]
                and existing["session"] == ident["session"]
                and existing["harness"] == ident["harness"]):
            return dict(existing, recorded=False)
        raise ProvenanceError(
            "%s: already recorded for %s/%s, not %s/%s"
            % (subject, existing["actor"], existing["session"],
               ident["actor"], ident["session"]))
    work_item_id = None
    if lot is not None:
        item = work_mod.get(db, int(lot))
        if item is None:
            raise ProvenanceError("work item #%s not found" % lot)
        work_item_id = int(item["id"])
    if approval_id is not None and key_path:
        raise ProvenanceError("--key and --approval are mutually exclusive")
    if approval_id is None and not key_path:
        raise ProvenanceError(
            "a signed receipt requires --key (sign now) or --approval ID")
    if key_path:
        if not approver:
            raise ProvenanceError("--key requires --as <owner>")
        try:
            seed = signing.read_private(os.path.expanduser(key_path))
        except (OSError, ValueError) as exc:
            raise ProvenanceError("cannot read the signing key: %s" % exc) from exc
        try:
            built = authority.create_approval(
                db, seed, approver=approver, action=SIGNED_ACTION,
                artifact_kind=SIGNED_KIND, artifact_hash=digest,
                ttl=authority.parse_ttl(expires),
                meta={"statement": statement, "host": ident["host"],
                      "lot": work_item_id or 0, "source": "provenance --signed"})
        except authority.AuthorityError as exc:
            raise ProvenanceError(str(exc)) from exc
        approval_id = int(built["id"])
    row = _approval_row(db, approval_id, digest)
    if (str(row.get("action") or "") != SIGNED_ACTION
            or str(row.get("artifact_kind") or "") != SIGNED_KIND):
        raise ProvenanceError(
            "approval #%s is not a signed provenance statement" % approval_id)
    if str(row.get("decision") or "") != "approved":
        # Un refus signé est une signature valide (authority.build_approval
        # accepte `rejected`) : la décision portée par la ligne et signée dans
        # son payload doit être « approved ». Sinon le refus deviendrait une
        # preuve de provenance (B P1 de la relecture cx3, #4485).
        raise ProvenanceError(
            "approval #%s is %r, not an approved statement"
            % (approval_id, row.get("decision") or "?"))
    if str(row.get("artifact_hash") or "").lower() != digest:
        raise ProvenanceError(
            "approval #%s does not cover this signed statement" % approval_id)
    verdict = authority.verify_approval(db, row)
    if not verdict.ok:
        raise ProvenanceError(
            "approval #%s is not valid: %s" % (approval_id, verdict.reason))
    if not authority.consume_approval(db, int(approval_id),
                                      "provenance:%s" % full):
        raise ProvenanceError(
            "approval #%s was already consumed" % approval_id)
    receipt = storage.of(db).provenance.add(
        subject, full, ident["actor"], ident["session"],
        SIGNED_TURN_PREFIX + str(approval_id), ident["harness"],
        work_item_id=work_item_id)
    if not receipt.get("recorded"):
        if (receipt["actor"] == ident["actor"]
                and receipt["session"] == ident["session"]
                and receipt["harness"] == ident["harness"]):
            return receipt
        raise ProvenanceError(
            "%s: already recorded for %s/%s, not %s/%s"
            % (subject, receipt["actor"], receipt["session"],
               ident["actor"], ident["session"]))
    return receipt


def live_session(db, actor: str, *, harness: str = "") -> str:
    """La session VIVANTE de l'acteur, au registre ; refuse si aucune.

    La preuve nomme la session qui a produit le fait : une session absente ne
    se remplace pas par un identifiant vide. Le harnais attendu est recontrôlé
    contre celui de la session vivante (`registry.live_harness`, #1037) : un
    harnais emprunté ne peut pas signer une preuve sous son étiquette."""
    from . import registry
    row = registry.get(db, actor) or {}
    session = str(row.get("session_id") or "").strip()
    if not session:
        raise ProvenanceError("no live session for %s" % actor)
    if harness:
        live = str(registry.live_harness(row) or "").strip().lower()
        if live and live != str(harness).strip().lower():
            raise ProvenanceError(
                "harness %r does not match the live session of %s (%r)"
                % (harness, actor, live))
    return session


def resolve_commit(value, *, cwd: str | None = None) -> str | None:
    """Le commit complet qu'une abréviation désigne, dans le dépôt sous la main.

    Rend None quand `git` ne résout pas — jamais une supposition : la preuve
    exige le commit complet, et une abréviation ambiguë ou inconnue laisse
    l'origine inconnue. `--end-of-options` borne la valeur : même si elle
    commence par `-`, elle reste une révision, jamais une option de `git`
    (N2 de la relecture Claude de #1465)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        run = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "--end-of-options",
             text + "^{commit}"],
            capture_output=True, text=True, cwd=cwd or os.getcwd(), timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (run.stdout or "").strip().lower()
    if run.returncode != 0 or not FULL_SHA.fullmatch(sha):
        return None
    return sha


def _git(cwd: str | None, *args: str) -> str | None:
    """La sortie de `git` sous la main, ou None (échec, git absent)."""
    try:
        run = subprocess.run(["git", *args], capture_output=True, text=True,
                             cwd=cwd or os.getcwd(), timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode != 0:
        return None
    return (run.stdout or "").strip()


#: L'attestation de création du hook `pre-commit` vit dans le répertoire git DU
#: worktree (`--absolute-git-dir`) : les hooks sont partagés entre worktrees,
#: l'attestation ne l'est pas.
INTENT_NAME = "ameesh-provenance-intent.json"

#: Le fait de création du hook `post-commit`, à côté de l'attestation : il nomme
#: l'objet né (`sha`, arbre, parents) et reprend l'aléa de l'attestation. Un
#: `reset` ou un `checkout` vers un objet ancien n'en produit pas — la dernière
#: entrée de reflog de HEAD n'est alors pas un `git commit`.
CREATED_NAME = "ameesh-provenance-created.json"

#: Les messages de reflog que SEUL `git commit` écrit pour l'objet qu'il vient
#: de créer : initial, ordinaire, amend, résolution de fusion. `reset`,
#: `checkout`, `merge --no-ff` automatique, `rebase` et `cherry-pick` écrivent
#: d'autres messages (origine inconnue, jamais devinée).
CREATION_RELOG = ("commit:", "commit (initial):", "commit (amend):",
                  "commit (merge):")


def intent_path(cwd: str | None = None) -> str | None:
    """Le chemin de l'attestation de ce worktree, ou None hors dépôt."""
    root = _git(cwd, "rev-parse", "--absolute-git-dir")
    return os.path.join(root, INTENT_NAME) if root else None


def clear_commit_intent(cwd: str | None = None) -> None:
    """Retire l'attestation du worktree (au mieux : absente n'est pas une erreur)."""
    path = intent_path(cwd)
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def prepare_commit_intent(cwd: str | None = None, *, environ=None,
                          actor: str = "", db=None, cfg=None) -> dict | None:
    """Écrit l'attestation du commit qui vient (hook `pre-commit`).

    Elle lie l'identité courante au HEAD et à l'arbre d'où le commit va partir.
    Elle ne bloque jamais un commit : hors tour managé sans session liée
    vérifiable, sans identité ou sans arbre calculable, elle n'est pas écrite
    (et toute attestation précédente est retirée) — le commit naîtra sans
    preuve, son origine restera inconnue. Une base n'est lue que hors tour
    managé, pour retrouver la liaison de session ; c'est le hook `post-commit`
    qui écrit la preuve.
    """
    env = os.environ if environ is None else environ
    path = intent_path(cwd)
    try:
        ident = _identity(env, actor=actor, db=db, cfg=cfg)
    except ProvenanceError:
        ident = None
    tree = _git(cwd, "write-tree") if ident is not None else None
    if path is None or ident is None or not tree:
        if path:
            clear_commit_intent(cwd)
        return None
    intent = {
        "version": 1,
        "actor": ident["actor"],
        "turn": ident["turn"],
        "harness": ident["harness"],
        "session": ident.get("session") or "",
        "head": _git(cwd, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") or "",
        "tree": tree,
        # L'état de reflog AVANT le commit : le fait de création devra constater
        # une entrée NOUVELLE (un commit qui n'apporte rien au reflog n'a pas
        # créé d'objet dans ce tour).
        "reflog": list(_head_reflog(cwd) or []),
        "nonce": secrets.token_hex(16),
    }
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(intent, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        return None
    return intent


def read_commit_intent(cwd: str | None = None) -> dict | None:
    """L'attestation en cours de ce worktree, ou None si absente ou illisible."""
    path = intent_path(cwd)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("actor", "turn", "harness", "head", "tree", "nonce"):
        if not isinstance(data.get(key), str):
            return None
    return data


def _parents(cwd: str | None, sha: str) -> list[str] | None:
    """Les parents du commit, ou None s'il ne se lit pas."""
    out = _git(cwd, "rev-list", "--parents", "-n", "1", sha)
    if out is None:
        return None
    fields = out.split()
    if not fields or fields[0] != sha:
        return None
    return fields[1:]


def _attests_creation(cwd: str | None, commit: str, intent: dict) -> bool:
    """Le commit est-il exactement la création annoncée par l'attestation ?

    Structurel, jamais temporel : le commit part du HEAD attesté (commit posé
    dessus, y compris une fusion en cours résolue par `git commit`), ou en est
    la réécriture (amend : mêmes parents que le HEAD attesté), et porte l'arbre
    attesté. Le commit attesté lui-même n'est jamais sa propre création."""
    parents = _parents(cwd, commit)
    if parents is None:
        return False
    tree = _git(cwd, "rev-parse", "%s^{tree}" % commit)
    if not tree or tree != intent["tree"]:
        return False
    head = intent["head"]
    if not head:
        return not parents
    if commit == head:
        return False
    head_parents = _parents(cwd, head)
    if head_parents is None:
        return False
    if parents and parents[0] == head:
        return True
    # amend/rewrite : le commit remplace le HEAD attesté, il en reprend les parents
    return parents == head_parents


def _same_identity(row, identity: dict) -> bool:
    return (row["actor"] == identity["actor"] and row["session"] == identity["session"]
            and row["turn"] == identity["turn"] and row["harness"] == identity["harness"])


def created_path(cwd: str | None = None) -> str | None:
    """Le chemin du fait de création de ce worktree, ou None hors dépôt."""
    root = _git(cwd, "rev-parse", "--absolute-git-dir")
    return os.path.join(root, CREATED_NAME) if root else None


def clear_commit_creation(cwd: str | None = None) -> None:
    """Retire le fait de création du worktree (au mieux : absent n'est pas une erreur)."""
    path = created_path(cwd)
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def read_commit_creation(cwd: str | None = None) -> dict | None:
    """Le fait de création en cours de ce worktree, ou None si absent ou illisible."""
    path = created_path(cwd)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("actor", "turn", "harness", "sha", "tree", "nonce"):
        if not isinstance(data.get(key), str):
            return None
    parents = data.get("parents")
    if not isinstance(parents, list) or any(not isinstance(p, str) for p in parents):
        return None
    return data


def _head_reflog(cwd: str | None = None) -> tuple[str, str] | None:
    """(sha, message) de la dernière entrée de reflog de HEAD, ou None."""
    out = _git(cwd, "reflog", "-1", "--format=%H%x00%gs", "HEAD")
    if not out or "\x00" not in out:
        return None
    sha, _, message = out.partition("\x00")
    return sha.strip().lower(), message.strip()


def prepare_commit_creation(cwd: str | None = None, *, environ=None,
                            actor: str = "", db=None, cfg=None) -> dict | None:
    """Écrit le fait de création de l'objet qui vient de naître (hook `post-commit`).

    Il lie le tour courant à l'objet créé : `sha`, arbre et parents de HEAD,
    repris de la dernière entrée de reflog — qui doit être un `git commit` de
    CE HEAD. Un `reset`/`checkout` vers un objet ancien, un `merge --no-ff`
    automatique, un `rebase` ou un `cherry-pick` écrivent un autre message :
    aucun fait de création n'est écrit, et `record_commit` refusera l'objet.

    Il exige l'attestation `pre-commit` du MÊME tour (même aléa repris) et une
    entrée de reflog **nouvelle** par rapport à celle que l'attestation
    portait : un HEAD inchangé, qui portait déjà son `commit:` d'un tour
    antérieur, n'est pas une création. Sans ces deux conditions, rien n'est
    écrit. Ne bloque jamais un commit ; aucune base n'est requise (c'est
    `record_commit` qui écrit la preuve). Hors tour, une base peut être
    fournie pour retrouver la liaison de session ; sans elle, rien n'est
    constaté."""
    env = os.environ if environ is None else environ
    path = created_path(cwd)
    if path is None:
        return None
    try:
        ident = _identity(env, actor=actor, db=db, cfg=cfg)
    except ProvenanceError:
        return None
    intent = read_commit_intent(cwd)
    if intent is None or (intent["actor"], intent["turn"], intent["harness"]) != (
            ident["actor"], ident["turn"], ident["harness"]):
        return None
    head = _git(cwd, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    reflog = _head_reflog(cwd)
    before = intent.get("reflog")
    before = tuple(before) if isinstance(before, list) and len(before) == 2 else None
    if not head or reflog is None or reflog[0] != head \
            or not reflog[1].startswith(CREATION_RELOG) or reflog == before:
        return None
    parents = _parents(cwd, head)
    tree = _git(cwd, "rev-parse", "%s^{tree}" % head)
    if parents is None or not tree:
        return None
    creation = {
        "version": 1,
        "actor": ident["actor"],
        "turn": ident["turn"],
        "harness": ident["harness"],
        "session": ident.get("session") or "",
        "sha": head,
        "tree": tree,
        "parents": parents,
        "nonce": intent["nonce"],
    }
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(creation, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        return None
    return creation


def proof_for_current_turn(db, *, actor: str = "", sha: str = "",
                           cwd: str | None = None, required: bool = False) -> dict | None:
    """L'identité exacte du tour courant, ou None si le fait n'y est pas déclaré.

    None couvre ces cas, tous « origine inconnue » : ni tour managé
    (`AMEESH_TURN_ID` absent) ni session liée vérifiable, pas de session vivante
    au registre pour l'acteur d'un tour, un `sha` déclaré qui n'est pas une
    abréviation hexadécimale (`HEAD`, nom de branche), ou un `sha` hexadécimal
    que le dépôt ne résout pas. Un jalon hors tour — ou dont le `sha` n'est pas
    un commit lisible — s'écrit donc sans preuve, jamais avec une preuve que
    `receipt_for_milestone` devrait ensuite refuser à jamais (N1 de la
    relecture Claude de #1465). Une session liée écrit un reçu à `turn`
    synthétique (`session:<id>`), jamais un faux tour managé.

    `required` (#3104) refuse ces cas par une `ProvenanceError` qui en nomme
    la cause : un verdict de tour managé ne doit jamais réussir sans reçu.
    Les gels et les déclarations hors tour gardent le comportement facultatif.
    """
    try:
        ident = capture(actor=actor, families=compiled_families(db), db=db)
        if "session" not in ident:
            # Tour managé : la session est celle du registre vivant. Une session
            # liée porte déjà la sienne, vérifiée par sa liaison.
            ident["session"] = live_session(db, ident["actor"],
                                            harness=ident["harness"])
    except ProvenanceError:
        if required:
            raise
        return None
    declared = str(sha or "").strip().lower()
    if declared:
        # La preuve porte le commit COMPLET, mais le fait (`work_item_milestones`)
        # garde le sha DÉCLARÉ : `receipt_for_milestone` les compare et refuse
        # une valeur non hexadécimale. On n'écrit donc aucune preuve pour un nom
        # symbolique — le jalon reste lisible, son origine inconnue.
        if not ABBREV.fullmatch(declared):
            if required:
                raise ProvenanceError("commit id expected, got %r" % sha)
            return None
        full = resolve_commit(declared, cwd=cwd)
        if full is None:
            if required:
                raise ProvenanceError(
                    "sha %s does not resolve to a commit in %s; "
                    "pass --repo <clone> pointing to the repository holding this commit"
                    % (declared, cwd or os.getcwd()))
            return None
        ident["sha"] = full
    else:
        ident["sha"] = ""
    return ident


def record_commit(db, sha, *, identity: dict, work_item_id: int | None = None,
                  cwd: str | None = None) -> dict:
    """Écrit la preuve exacte d'un commit créé dans ce tour, ou refuse.

    C'est le point d'écriture du hook `post-commit` : l'identité vient du tour
    courant (jamais de l'appelant). Trois bornes : le commit est **HEAD du
    dépôt sous la main**, son committer est l'acteur du tour
    (`<acteur>@ameesh.invalid`, posé par le runner), et il est **attesté comme
    création de ce tour** par la paire de hooks — l'attestation `pre-commit`
    (identité du tour, HEAD et arbre de départ, aléa) et le fait de création
    `post-commit` (l'objet né), liés par le même aléa ; les deux sont consommés
    par l'écriture. Un objet seulement restauré par `reset`/`checkout`, même
    committé par le même agent, n'a pas de fait de création : son origine reste
    inconnue (écart cx3, 15/10 04:37 ; B1 cx2/Codex, 15/10 12:12), jamais
    réattribuée au nouveau tour.

    Une preuve déjà écrite pour ce commit n'est pas réécrite : une identité
    différente est une erreur."""
    full = full_sha(sha) if FULL_SHA.fullmatch(str(sha or "").strip().lower()) \
        else resolve_commit(sha, cwd=cwd)
    if full is None:
        raise ProvenanceError("commit %r does not resolve to a full commit" % (sha,))
    head = resolve_commit("HEAD", cwd=cwd)
    if head is None or head != full:
        raise ProvenanceError(
            "only the current worktree HEAD can be recorded (HEAD is %s, not %s)"
            % ((head or "?")[:12], full[:12]))
    from .identity import GIT_EMAIL_DOMAIN
    committer = subprocess.run(
        ["git", "log", "-1", "--format=%ce", full], capture_output=True, text=True,
        cwd=cwd or os.getcwd(), timeout=20)
    email = (committer.stdout or "").strip().lower()
    expected = ("%s@%s" % (identity["actor"], GIT_EMAIL_DOMAIN)).lower()
    if committer.returncode != 0 or email != expected:
        raise ProvenanceError(
            "commit %s was not committed by %s (committer %r)"
            % (full[:12], identity["actor"], email or "?"))
    subject = subject_of_commit(full)
    ops = storage.of(db).provenance
    existing = ops.get(subject)
    if existing is not None:
        # La preuve est déjà écrite : rejouer la même identité ne réécrit rien
        # (l'attestation a été consommée par la première écriture).
        if not _same_identity(existing, identity):
            raise ProvenanceError(
                "%s: already recorded for %s/%s, not %s/%s"
                % (subject, existing["actor"], existing["turn"],
                   identity["actor"], identity["turn"]))
        return dict(existing, recorded=False)
    intent = read_commit_intent(cwd)
    if intent is None:
        raise ProvenanceError(
            "no creation attestation for %s: only a commit created in this turn"
            " can be recorded" % full[:12])
    if (intent["actor"] != identity["actor"] or intent["turn"] != identity["turn"]
            or intent["harness"] != identity["harness"]):
        raise ProvenanceError(
            "creation attestation of %s/%s/%s is not from this turn (%s/%s/%s)"
            % (intent["actor"], intent["turn"][:8], intent["harness"],
               identity["actor"], identity["turn"][:8], identity["harness"]))
    creation = read_commit_creation(cwd)
    if creation is None:
        raise ProvenanceError(
            "no creation record for %s: a restored object is not a creation"
            " of this turn" % full[:12])
    if (creation["actor"] != identity["actor"] or creation["turn"] != identity["turn"]
            or creation["harness"] != identity["harness"]):
        raise ProvenanceError(
            "creation record of %s/%s/%s is not from this turn (%s/%s/%s)"
            % (creation["actor"], creation["turn"][:8], creation["harness"],
               identity["actor"], identity["turn"][:8], identity["harness"]))
    if creation["sha"] != full:
        raise ProvenanceError(
            "creation record names %s, not %s: a restored object is not a creation"
            % (creation["sha"][:12], full[:12]))
    if creation["nonce"] != intent["nonce"]:
        raise ProvenanceError(
            "creation record and attestation of %s do not come from the same gesture"
            % full[:12])
    parents = _parents(cwd, full)
    tree = _git(cwd, "rev-parse", "%s^{tree}" % full)
    if parents is None or tree != creation["tree"] or parents != creation["parents"]:
        raise ProvenanceError(
            "creation record does not describe commit %s" % full[:12])
    if not _attests_creation(cwd, full, intent):
        raise ProvenanceError(
            "commit %s is not the creation attested for this turn" % full[:12])
    row = ops.add(subject, full, identity["actor"], identity["session"],
                  identity["turn"], identity["harness"], work_item_id=work_item_id)
    if not row["recorded"] and not _same_identity(row, identity):
        raise ProvenanceError(
            "%s: already recorded for %s/%s, not %s/%s"
            % (subject, row["actor"], row["turn"], identity["actor"], identity["turn"]))
    clear_commit_intent(cwd)
    clear_commit_creation(cwd)
    return row


#: les hooks posés par `ameesh provenance install` — ils ne bloquent JAMAIS un
#: commit : sans identité prouvable (tour managé ou session liée), sans base ou
#: sans ameesh, ils sortent 0. `pre-commit` atteste le commit qui vient
#: (identité, HEAD et arbre de départ) ; `post-commit` constate le fait de
#: création de l'objet né, puis écrit la preuve en consommant les deux. Ils
#: n'exigent pas `AMEESH_TURN_ID` : la commande cherche la liaison de session
#: quand le tour manque (#3985).
#:
#: Chaque hook porte une ligne de signature EXACTE : `_install_one` ne reconnaît
#: un hook comme le nôtre que sur cette ligne entière (ou sur un contenu
#: identique), jamais sur la seule mention « ameesh #1465 » — un hook étranger
#: qui la cite ne doit pas être écrasé (N4 de la relecture Claude de #1465).
PRE_COMMIT_MARK = "# ameesh #1465 — attestation de creation du commit, avant qu'il naisse."
POST_COMMIT_MARK = "# ameesh #1465 — fait de creation de l'objet ne, puis preuve d'origine."
PRE_COMMIT_HOOK = """#!/bin/sh
# ameesh #1465 — attestation de creation du commit, avant qu'il naisse.
# Ne bloque jamais un commit : sans preuve possible ou sans ameesh, sort 0.
command -v ameesh >/dev/null 2>&1 || exit 0
ameesh provenance intent >/dev/null 2>&1 || exit 0
exit 0
"""

HOOK = """#!/bin/sh
# ameesh #1465 — fait de creation de l'objet ne, puis preuve d'origine.
# Ne bloque jamais un commit : sans preuve possible ou sans base, sort 0.
command -v ameesh >/dev/null 2>&1 || exit 0
ameesh provenance created >/dev/null 2>&1
ameesh provenance record --commit HEAD --quiet >/dev/null 2>&1 || exit 0
exit 0
"""


def _install_one(path: str, content: str, marker: str) -> dict:
    """Pose un hook ; un hook ÉTRANGER n'est jamais écrasé.

    Un hook est le nôtre s'il est identique au contenu courant, ou s'il porte
    la ligne de signature `marker` à l'identique (les hooks d'une version
    antérieure la portent déjà : la mise à jour reste possible). Une simple
    sous-chaîne « ameesh #1465 » ne suffit pas."""
    name = os.path.basename(path)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                current = fh.read()
        except OSError as exc:
            return {"path": path, "installed": False,
                    "reason": "cannot read the existing %s hook: %s" % (name, exc)}
        if current != content and marker not in {
                line.strip() for line in current.splitlines()}:
            return {"path": path, "installed": False,
                    "reason": "a foreign %s hook is already installed" % name}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, 0o755)
    except OSError as exc:
        return {"path": path, "installed": False,
                "reason": "cannot install the %s hook: %s" % (name, exc)}
    return {"path": path, "installed": True, "reason": "installed"}


def install_hook(repo: str) -> dict:
    """Pose la paire `pre-commit`/`post-commit` dans le dépôt `repo`.

    Idempotent : un hook absent ou déjà le nôtre est (ré)écrit ; un hook
    ÉTRANGER n'est jamais écrasé — on le signale, l'agent décide. `installed`
    n'est vrai que si les DEUX hooks sont les nôtres : une preuve exacte a
    besoin de l'attestation de création et de son écriture. Rend
    `{"path", "installed", "reason", "hooks"}`.

    Le répertoire est celui que Git utilise réellement (`git rev-parse
    --git-path hooks`), donc `core.hooksPath` est suivi : poser les hooks dans
    `<git-common-dir>/hooks` quand `core.hooksPath` pointe ailleurs les rendrait
    muets tout en annonçant `installed=True` (N3 de la relecture Claude de
    #1465)."""
    repo = os.path.abspath(repo)
    hooks = _git(repo, "rev-parse", "--path-format=absolute",
                 "--git-path", "hooks")
    if not hooks:
        # git < 2.31 ne connaît pas --path-format : le chemin peut être relatif
        # au dépôt.
        hooks = _git(repo, "rev-parse", "--git-path", "hooks")
        if hooks and not os.path.isabs(hooks):
            hooks = os.path.join(repo, hooks)
    if not hooks:
        return {"path": "", "installed": False, "hooks": {},
                "reason": "not a git repository: %s" % (repo,)}
    out = {}
    for name, content, marker in (
            ("pre-commit", PRE_COMMIT_HOOK, PRE_COMMIT_MARK),
            ("post-commit", HOOK, POST_COMMIT_MARK)):
        out[name] = _install_one(os.path.join(hooks, name), content, marker)
    blocked = [name for name, row in out.items() if not row["installed"]]
    return {
        "path": out["post-commit"]["path"],
        "installed": not blocked,
        "hooks": out,
        "reason": "" if not blocked else "; ".join(
            "%s: %s" % (name, out[name]["reason"]) for name in blocked),
    }


def _connect():
    from . import config as config_mod, db as db_mod
    cfg = config_mod.load()
    db = db_mod.connect(cfg)
    db_mod.require_schema(db)
    return db


def _hook_db():
    """La base des hooks hors tour, ou None (hook muet, jamais bloquant).

    Un tour managé n'en a pas besoin : l'identité vient de l'environnement.
    Hors tour (session liée), la liaison ne se lit qu'en base ; une base
    absente ou illisible rend None et le hook n'atteste rien, comme avant."""
    if os.environ.get("AMEESH_TURN_ID"):
        return None
    try:
        return _connect()
    except Exception:  # noqa: BLE001 - hook non bloquant
        return None


def build_parser():
    import argparse
    parser = argparse.ArgumentParser(
        prog="ameesh provenance",
        description="#1465 : preuves exactes d'origine d'un commit et d'un jalon.")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="record the exact origin of a commit")
    rec.add_argument("--commit", required=True, help="commit id (HEAD accepted)")
    rec.add_argument("--lot", type=int, default=None, help="attach to a work item")
    rec.add_argument("--quiet", action="store_true")
    rec.add_argument("--json", action="store_true")
    # #4485 : provenance hors runner, attestée par une clé owner (bureau/owner
    # sur un hôte sans ameesh ni PID local). Les champs sont signés ensemble.
    rec.add_argument("--signed", action="store_true",
                     help="owner-signed provenance, outside a managed turn")
    rec.add_argument("--actor", default="", help="signed actor (external agent)")
    rec.add_argument("--session", default="", help="signed harness session id")
    rec.add_argument("--harness", default="", help="signed harness (claude|codex|deepseek)")
    rec.add_argument("--host", default="", help="host where the signed session ran")
    rec.add_argument("--repo", default=".", help="repository holding the commit")
    rec.add_argument("--key", default="",
                     help="owner private key file to sign now (0600, /dev/stdin allowed)")
    rec.add_argument("--as", dest="as_agent", default="",
                     help="owner key holder name (with --key)")
    rec.add_argument("--approval", type=int, default=None,
                     help="already-signed approval id (instead of --key)")
    rec.add_argument("--expires", default=None,
                     help="lifetime of the approval created with --key (24h default)")
    stmt = sub.add_parser(
        "statement",
        help="print the canonical signed statement and its digest (#4485)")
    stmt.add_argument("--commit", required=True)
    stmt.add_argument("--actor", required=True)
    stmt.add_argument("--session", required=True)
    stmt.add_argument("--harness", required=True)
    stmt.add_argument("--host", required=True)
    stmt.add_argument("--lot", type=int, default=None)
    stmt.add_argument("--json", action="store_true")
    inst = sub.add_parser("install", help="install the provenance hooks")
    inst.add_argument("--repo", default=".", help="repository (default: cwd)")
    inst.add_argument("--json", action="store_true")
    att = sub.add_parser("intent",
                         help="write the creation attestation (pre-commit hook)")
    att.add_argument("--json", action="store_true")
    cre = sub.add_parser("created",
                         help="write the creation record of the new object"
                              " (post-commit hook)")
    cre.add_argument("--json", action="store_true")
    show = sub.add_parser("show", help="read the receipt, or nothing if unknown")
    show.add_argument("--commit", default="")
    show.add_argument("--milestone", type=int, default=None)
    show.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    import json
    import sys
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "statement":
        fields = dict(commit=args.commit, actor=args.actor, session=args.session,
                      harness=args.harness, host=args.host, lot=args.lot)
        try:
            text = signed_statement(**fields)
            digest = signed_digest(**fields)
        except ProvenanceError as exc:
            print("provenance refused: %s" % exc, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"statement": text, "digest": digest}))
        else:
            print("statement: %s" % text)
            print("digest   : %s" % digest)
        return 0
    if args.command == "install":
        out = install_hook(args.repo)
        if args.json:
            print(json.dumps(out))
        elif out["installed"]:
            print("provenance hooks installed: pre-commit %s, post-commit %s"
                  % (out["hooks"]["pre-commit"]["path"],
                     out["hooks"]["post-commit"]["path"]))
        else:
            print("provenance hooks NOT installed: %s" % out["reason"],
                  file=sys.stderr)
        return 0 if out["installed"] else 1
    if args.command == "intent":
        # Aucune base sous tour managé ; hors tour, la liaison de session se
        # lit en base. Le hook ne bloque jamais un commit.
        db = _hook_db()
        try:
            attestation = prepare_commit_intent(db=db)
        finally:
            if db is not None:
                db.close()
        if args.json:
            print(json.dumps(attestation, sort_keys=True))
        return 0
    if args.command == "created":
        db = _hook_db()
        try:
            creation = prepare_commit_creation(db=db)
        finally:
            if db is not None:
                db.close()
        if args.json:
            print(json.dumps(creation, sort_keys=True))
        return 0
    db = _connect()
    try:
        if args.command == "record" and args.signed:
            if not (args.actor and args.session and args.harness and args.host):
                print("error: --signed requires --actor, --session, --harness "
                      "and --host", file=sys.stderr)
                return 2
            if bool(args.key) == (args.approval is not None):
                print("error: --signed requires exactly one of --key or --approval",
                      file=sys.stderr)
                return 2
            row = record_signed_commit(
                db, args.commit, actor=args.actor, session=args.session,
                harness=args.harness, host=args.host,
                families=compiled_families(db), lot=args.lot,
                approval_id=args.approval, approver=args.as_agent,
                key_path=args.key, cwd=os.path.abspath(args.repo),
                expires=args.expires)
            if args.json:
                print(json.dumps(row, default=str))
            elif not args.quiet:
                print("provenance recorded (signed): %s by %s in %s, turn %s"
                      % (row["subject"], row["actor"], row["session"],
                         row["turn"]))
            return 0
        if args.command == "record":
            identity = capture(families=compiled_families(db), db=db)
            if "session" not in identity:
                identity["session"] = live_session(db, identity["actor"],
                                                   harness=identity["harness"])
            row = record_commit(db, args.commit, identity=identity,
                                work_item_id=args.lot)
            if args.json:
                print(json.dumps(row, default=str))
            elif not args.quiet:
                print("provenance recorded: %s by %s (turn %s)"
                      % (row["subject"], row["actor"], row["turn"][:8]))
            return 0
        if args.commit:
            recu = receipt_for_commit(db, args.commit)
        elif args.milestone is not None:
            recu = receipt_for_milestone(db, args.milestone)
        else:
            print("error: --commit or --milestone is required", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(recu, default=str))
        elif recu is None:
            print("unknown origin (no exact receipt)")
        else:
            print("%s: %s in %s, turn %s, %s"
                  % (recu["subject"], recu["actor"], recu["session"],
                     recu["turn"][:8], recu["record"]))
        return 0 if recu is not None else 3
    except ProvenanceError as exc:
        print("provenance refused: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()
