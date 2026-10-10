# SPDX-License-Identifier: AGPL-3.0-only
"""Exécuteur médié : contrat de l'API d'exécuteur `/api/exec/v1` (L107).

* `contract.json` : la table FERMÉE des 61 opérations admises (livrée dans la
  roue, lue par le serveur et par l'exécuteur de la VM) ;
* `contrat` : chargeur, enveloppes, idempotence, erreurs ;
* `evenements` : flux SSE et attente longue ;
* `porte` : `HostGate` et `ameesh-host-state/1` ;
* `interfaces` : interfaces figées entre L108, L109, L110 et L112.

Référence : `docs/EXECUTEUR-MEDIEE.md`.
"""
from .contract import Contract, Fence, OpRequest, OpResult, Operation, load

__all__ = ["Contract", "Fence", "OpRequest", "OpResult", "Operation", "load"]
