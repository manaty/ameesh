---
type: Index
title: "Canon d'exemple — acme"
description: "Organisation fictive acme : deux humains, deux hôtes, trois agents placés, un plan de travail (jalon, epics, lots). Sert aux tests et à la démonstration d'ameesh (profil ameesh d'OKF, spec §4)."
---

# Canon d'exemple — acme

Organisation **fictive**. Rien ici ne désigne une personne, une machine ou un
client réels.

- [Membres](membres/) — les humains : `alice` (responsable du projet),
  `bruno` (responsable de l'infrastructure).
- [Hôtes](hotes/) — `atelier` (poste de travail, tous harnais) et `banc`
  (serveur de test : DeepSeek et Codex seulement, clé d'API seulement).
- [Agents](agents/) — `orchestre` (claude), `relecteur` (codex),
  `ouvrier` (deepseek).
- [Placements](placements/) — quel agent tourne sur quel hôte, avec quel mode
  d'identifiants.
- [Plan](plan/) — le plan de travail (fiches `WorkPackage`, L29) : le jalon
  `v1`, les epics `catalogue` et `paiement`, les lots `cat-recherche`,
  `cat-export` et `pay-webhook`.
- [Décisions](decisions/) — un exemple de fiche d'un autre type, ignorée par
  ameesh.

Essai :

```bash
git init -q /tmp/acme && cp -r examples/canon/. /tmp/acme/   # puis commit et push
AMEESH_CANON=/tmp/acme ameesh canon check
AMEESH_CANON=examples/canon AMEESH_CANON_UNTRUSTED=1 ameesh canon show   # prototype
```
