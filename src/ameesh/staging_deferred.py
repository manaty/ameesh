# SPDX-License-Identifier: AGPL-3.0-only
"""#7365: a reported staging server is a version lag, not a permanent state.

The staging sequence deploys the four VMs of the mesh (``STAGING_VMS``) and
may report the workstation, which updates at its return, and a server without
root access. Canon §14 allows a report, never a permanent one: the next cutoff
never goes back below the last commit, and a reported host is caught up.

The sequence cannot catch up a server without root itself: in server mode
``vm.sh`` runs ``ssh root@$VM``, installs ``/opt/ameesh`` and writes
``/root/.cache/ameesh``, and the staging account holds no root path. The
gesture is the bureau's, after each cutoff, under an owner root right (§10).
This module is its guard, run by the release timer: the latest tagged staging
attestation names each reported host except the workstation, its reason and
the last tag that proved it, and mails the pilot and the bureau once per
release. The gesture is declared after each cutoff instead of being forgotten.

A workstation report is normal and is never alerted. No readable attestation,
no tag, an unreadable state: nothing is concluded, no mail is sent.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "ameesh-staging-deferred/1"
#: the state slot key holding the last alert sent, per recipient
SLOT = "staging_deferred_alerts"


def _registry_rows(db):
    from . import registry
    try:
        return list(registry.overview(db))
    except Exception:  # noqa: BLE001 - an unreadable registry never blocks the alert
        return []


def _recipients(cfg, db, project) -> list[str]:
    """The bureau, which performs the gesture, then the project's pilot."""
    from . import alert_briefs
    names = ["bureau"]
    try:
        pilots = alert_briefs.project_pilots(project.name, _registry_rows(db),
                                             cfg=cfg, db=db)
    except Exception:  # noqa: BLE001 - the bureau still receives the gesture
        pilots = []
    for name in pilots:
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _rows(db) -> list[dict]:
    """The shared staging attestations, newest first (unreadable: empty)."""
    from . import release_cutoff as rc
    return [row for row in rc.stored_attestations(db) if isinstance(row, dict)]


def _tagged(rows, by_commit):
    """(index, tag, attestation) of the latest attestation of a release tag."""
    for index, data in enumerate(rows):
        sha = data.get("sha")
        tag = by_commit.get(sha) if isinstance(sha, str) else None
        if tag is not None:
            return index, tag, data
    return None


def _deferred(data) -> dict:
    """The hosts reported by an attestation, except the workstation.

    A `pc-smichea` report is the normal "updated at its return": it is a
    report, not a lag. Any other reported host is a runtime the sequence
    never deploys, which §14 forbids to leave permanent.
    """
    from . import release_cutoff as rc
    reportes = data.get("reportes") or {}
    if not isinstance(reportes, dict):
        return {}
    out = {}
    for host, detail in reportes.items():
        if not isinstance(host, str) or not host or host == rc.STAGING_POSTE:
            continue
        out[host] = detail if isinstance(detail, dict) else {}
    return out


def _proved_since(rows, index, host) -> bool:
    """A newer attestation proves the host: the report is caught up (#7365).

    This is the other outcome — the root path was given and `--absent`
    removed. A report older than a proof is history, not a lag.
    """
    for data in rows[:index]:
        hotes = data.get("hotes") or {}
        if isinstance(hotes, dict) and host in hotes:
            return True
    return False


def _last_proof(rows, start, hosts, by_commit) -> dict:
    """For each host, the newest attestation before `start` that proved it."""
    out = {}
    for host in hosts:
        for data in rows[start + 1:]:
            hotes = data.get("hotes") or {}
            if isinstance(hotes, dict) and host in hotes:
                sha = str(data.get("sha") or "")
                tag = by_commit.get(sha)
                out[host] = {"sha": sha, "date": str(data.get("date") or ""),
                             "tag": (tag or {}).get("tag", ""),
                             "version": (tag or {}).get("version", "")}
                break
    return out


def _text(project, tag, data, deferred, proofs) -> str:
    """The alert body: the tag not deployed, the reason, the gesture."""
    hosts = sorted(deferred)
    lines = ["Staging %s : le tag %s (%s) n'est pas déployé sur %s."
             % (project.name, tag["tag"], tag.get("version") or "?", ", ".join(hosts))]
    for host in hosts:
        reason = " ".join(str((deferred[host] or {}).get("raison") or "absent").split())
        lines.append("- %s : reporté — %s" % (host, reason))
        proof = proofs.get(host)
        if proof and proof.get("tag"):
            lines.append("  dernière preuve de staging sur cet hôte : %s (%s, %s)"
                         % (proof["tag"], (proof.get("sha") or "")[:12],
                            proof.get("date") or "date inconnue"))
        elif proof:
            lines.append("  dernière preuve de staging sur cet hôte : commit %s (%s)"
                         % ((proof.get("sha") or "")[:12],
                            proof.get("date") or "date inconnue"))
        else:
            lines.append("  aucune attestation de staging n'a jamais prouvé cet hôte")
    lines += [
        "§14 : un hôte reporté est rattrapé, jamais reporté en permanence ; la coupure "
        "suivante ne redescend jamais sous ce commit. Ce n'est pas le report du poste "
        "(mis à jour à son retour) : ce serveur n'a pas d'accès root, la séquence ne "
        "peut pas l'installer (vm.sh serveur passe par root).",
        "Geste bureau, après chaque coupure (droit root §10) : depuis le compte de "
        "staging, avec son BASCULE_ENV (VM = adresse du serveur, PERSONAS_VM vide), "
        "deploy/mise-a-jour/vm.sh verifier ; vm.sh installer %s ; vm.sh redemarrer ; "
        "vm.sh controler. Retour arrière : vm.sh retour. Postgres n'est jamais arrêté."
        % str(data.get("sha") or "?")[:12],
        "Procédure : docs/DEPLOIEMENT-HOTE-AGENTS.md, « Serveur sans accès root : "
        "geste bureau après chaque coupure ».",
    ]
    return "\n".join(lines)


def tick(cfg, db, project) -> dict:
    """Alert the pilot and the bureau once per release whose staging reported
    a server. Never blocks a release: an unreadable input concludes nothing."""
    from . import release_cutoff as rc
    report = {"schema": SCHEMA, "project": project.name, "result": "none"}
    rows = _rows(db)
    if not rows:
        return report
    by_commit = rc._tags_by_commit(project)
    chosen = _tagged(rows, by_commit)
    if chosen is None:
        return report
    index, tag, data = chosen
    deferred = {host: detail for host, detail in _deferred(data).items()
                if not _proved_since(rows, index, host)}
    if not deferred:
        return report
    proofs = _last_proof(rows, index, deferred, by_commit)
    key = json.dumps({"tag": tag["tag"], "hosts": sorted(deferred)}, sort_keys=True)
    report.update({"tag": tag["tag"], "version": tag.get("version") or "",
                   "hosts": sorted(deferred), "key": key})
    lock = str(Path(cfg.state_dir) / "release" / "release.lock")
    with rc._locked(lock):
        state = rc.load_state(cfg)
        slot = rc.project_state(state, project.name)
        sent = slot.setdefault(SLOT, {})
        targets = [name for name in _recipients(cfg, db, project)
                   if sent.get(name) != key]
        if not targets:
            report["result"] = "already-sent"
            return report
        text = _text(project, tag, data, deferred, proofs)
        from . import mail, work
        for target in targets:
            mail.send(db, work.SYSTEM_SENDER, target, text, kind="event",
                      payload={"urgent": True, "staging_deferred": report})
            sent[target] = key
        rc.save_state(cfg, state)
        report.update({"result": "alerted", "recipients": targets})
    return report
