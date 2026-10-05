---
type: Decision
title: "Bascule automatique vers un compte secondaire, retour au primaire après la remise à zéro"
description: "Pour chaque fournisseur, une liste ordonnée de comptes ; quand le compte actif approche sa limite, les tours suivants passent au compte suivant, et reviennent au primaire dès que sa fenêtre est remise à zéro."
status: stable
tags: [budgets, forfaits, comptes, routage]
decided_by: human:smichea
decision_date: 2026-10-05
attestation: "demande du propriétaire dans sa conversation avec mesh-design (80 % du forfait Claude sur 7 jours consommés) ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-05T09:40:00+02:00" }
---

# Décision

1. Pour chaque fournisseur (Claude, Codex, DeepSeek, …), un hôte ou une équipe
   déclare une **liste ordonnée de comptes** (primaire, secondaire, …). Chaque
   compte a ses identifiants (secrets de l'hôte, jamais dans le canon) et ses
   jauges.
2. **Bascule automatique** : quand le compte actif atteint le seuil de la garde
   de budget ([0019](0019-budgets-et-routage.md) : `min(90 %, part écoulée + 10
   points)` pour un forfait, plafond ou solde pour un compte payé au token), les
   **tours suivants** utilisent le compte suivant de la liste au lieu de mettre
   les agents en pause. La pause ne survient que lorsque tous les comptes sont
   au seuil.
3. **Retour automatique** au compte primaire dès que sa fenêtre est remise à
   zéro (ou son solde rechargé), au tour suivant.
4. Jamais au milieu d'un tour ; chaque bascule est journalisée (événement, fil
   de l'équipe) et visible dans `ameesh cost` et `ameesh progress`.
5. La continuité des sessions est recherchée (même session reprise sous l'autre
   compte quand le harnais le permet) ; sinon rotation avec résumé de reprise
   ([0025](0025-une-session-par-lot.md)).

# Conséquences

- Lot L30. Le respect des conditions d'utilisation de chaque fournisseur pour
  l'usage de plusieurs comptes reste de la responsabilité de l'équipe.
- Les gestes de connexion à un compte (`/login`, `codex login`) restent
  humains ; ameesh ne stocke ni ne copie de mot de passe.
