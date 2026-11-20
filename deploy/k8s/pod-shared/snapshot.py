#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#7699 (P6 de #7633) : sauvegardes et restaurations d'un hôte pod partagé.

`render.py` décrit l'état désiré d'un pod partagé ; ce script outille les
**gestes de sauvegarde** que le chart ne peut pas porter comme objets
statiques : un `VolumeSnapshot` est daté et créé à la demande, jamais à chaque
`kubectl apply`. Il rend, depuis le même `host.exemple.yaml` :

* en mode snapshot (défaut) : un `VolumeSnapshot` par PVC de tenant, nommé
  `pvc-<slug>-<horodatage>` ;
* en mode `--restore` : un PVC par tenant, du **même nom** que l'original, avec
  `dataSource` vers le snapshot — le runbook impose d'avoir arrêté le pod et
  supprimé l'ancien PVC avant d'appliquer (aucune donnée n'est écrasée par ce
  script).

Aucun secret n'est lu. La classe de snapshot est celle du cluster (TPK/IMA) :
`--class` l'épingle, sinon la classe par défaut du cluster s'applique. Le
script est un outil d'opérateur : il imprime des manifestes, il n'appelle pas
l'API Kubernetes et n'applique rien.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

try:
    import yaml
except ImportError:  # pragma: no cover - l'opérateur installe PyYAML
    sys.exit("snapshot.py: PyYAML est requis (pip install pyyaml)")

# Charger render.py du même dossier sans dépendre du répertoire courant.
_spec = importlib.util.spec_from_file_location("ameesh_pod_shared_render",
                                               HERE / "render.py")
render = importlib.util.module_from_spec(_spec)
sys.modules["ameesh_pod_shared_render"] = render
_spec.loader.exec_module(render)

STAMP_RE = re.compile(r"^[0-9]{8}t[0-9]{6}z$")
DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class SnapshotError(ValueError):
    """Horodatage, classe ou configuration incohérents ; rien n'est rendu."""


def stamp(value: str | None = None) -> str:
    """L'horodatage UTC compact, trié lexicographiquement (`20261120t190000z`)."""
    if value:
        text = str(value).strip().lower()
        if not STAMP_RE.match(text):
            raise SnapshotError("horodatage illisible : %r (attendu "
                                "AAAAMMJJtHHMMSSz)" % (value,))
        return text
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dt%H%M%SZ").lower()


def _snapshot_class(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not DNS_LABEL_RE.match(text):
        raise SnapshotError("classe de snapshot illisible : %r" % (text,))
    return text


def _snapshot_name(pvc: str, stamp_value: str) -> str:
    name = "%s-%s" % (pvc, stamp_value)
    if not DNS_LABEL_RE.match(name):
        raise SnapshotError("nom de snapshot illisible : %r" % (name,))
    return name


def snapshot_documents(config: dict, *, stamp_value: str,
                       snapshot_class: str | None = None) -> list[dict]:
    """Un `VolumeSnapshot` par PVC de tenant, dans l'ordre du chart."""
    stamp_value = stamp(stamp_value)
    snapshot_class = _snapshot_class(snapshot_class)
    labels = render._labels(config, component="pod-snapshot")
    documents = []
    for entry in config["tenants"]:
        plan = entry["plan"]
        slug = render._slug(plan["tenant"].split(":", 1)[1])
        claim = plan["volumes"]["home"]["persistentVolumeClaim"]
        pvc = claim["claimName"] if isinstance(claim, dict) else str(claim)
        name = _snapshot_name(pvc, stamp_value)
        spec = {
            "source": {"persistentVolumeClaimName": pvc},
        }
        if snapshot_class:
            spec["volumeSnapshotClassName"] = snapshot_class
        documents.append({
            "apiVersion": "snapshot.storage.k8s.io/v1",
            "kind": "VolumeSnapshot",
            "metadata": {
                "name": name,
                "labels": dict(labels, **{
                    "ameesh.dev/tenant": slug,
                    "ameesh.dev/snapshot-of": pvc,
                }),
            },
            "spec": spec,
        })
    return documents


def restore_documents(config: dict, *, stamp_value: str,
                      snapshot_class: str | None = None) -> list[dict]:
    """Un PVC par tenant, recréé depuis le snapshot, **même nom**.

    Le PVC d'origine doit avoir été supprimé (pod arrêté) : appliquer ce
    manifeste avant serait refusé par l'API (le PVC existe). Le runbook porte
    la séquence et la vérification du témoin de compte après restauration.
    """
    stamp_value = stamp(stamp_value)
    _snapshot_class(snapshot_class)
    labels = render._labels(config, component="pod-host")
    documents = []
    for entry in config["tenants"]:
        plan = entry["plan"]
        slug = render._slug(plan["tenant"].split(":", 1)[1])
        claim = plan["volumes"]["home"]["persistentVolumeClaim"]
        pvc = claim["claimName"] if isinstance(claim, dict) else str(claim)
        snapshot = _snapshot_name(pvc, stamp_value)
        documents.append({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": pvc,
                "labels": dict(labels, **{
                    "ameesh.kubernetes.io/tenant": slug,
                    "ameesh.dev/restored-from": snapshot,
                }),
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "dataSource": {
                    "name": snapshot,
                    "kind": "VolumeSnapshot",
                    "apiGroup": "snapshot.storage.k8s.io",
                },
                "resources": {"requests": {
                    "storage": plan["volumes"]["home"]["size"]}},
            },
        })
    return documents


def _text(documents, source: str) -> str:
    return render._text(documents, source, tool="snapshot.py")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="snapshot.py",
        description="Rend les VolumeSnapshot (ou les PVC de restauration) "
                    "d'un hôte pod partagé (#7699).")
    parser.add_argument("--config", default=str(HERE / "host.exemple.yaml"))
    parser.add_argument("--stamp", default=None,
                        help="horodatage AAAAMMJJtHHMMSSz (défaut : maintenant, UTC)")
    parser.add_argument("--class", dest="snapshot_class", default=None,
                        help="VolumeSnapshotClass du cluster (défaut : classe "
                             "par défaut)")
    parser.add_argument("--restore", action="store_true",
                        help="rend les PVC de restauration, pas les snapshots")
    parser.add_argument("--write", default=None, metavar="DIR",
                        help="écrit dans DIR au lieu d'imprimer")
    args = parser.parse_args(argv)
    path = Path(args.config).resolve()
    try:
        config = render.load_config(path)
        stamp_value = stamp(args.stamp)
        if args.restore:
            documents = restore_documents(
                config, stamp_value=stamp_value,
                snapshot_class=args.snapshot_class)
            filename = "restore-pvcs-%s.yaml" % stamp_value
        else:
            documents = snapshot_documents(
                config, stamp_value=stamp_value,
                snapshot_class=args.snapshot_class)
            filename = "snapshots-%s.yaml" % stamp_value
        source = os.path.relpath(path, ROOT)
        text = _text(documents, source)
    except (SnapshotError, render.RenderError) as exc:
        print("snapshot refusé : %s" % exc, file=sys.stderr)
        return 2
    if args.write:
        out = Path(args.write).resolve()
        out.mkdir(parents=True, exist_ok=True)
        (out / filename).write_text(text, encoding="utf-8")
        print("écrit : %s" % (out / filename))
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
