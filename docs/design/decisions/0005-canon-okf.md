---
type: Decision
title: "Le canon est en OKF dans git, fédéré selon OKF Federation"
description: "Équipes, responsabilités, structure des projets sont décrites en OKF ; Nexlink et les outils n'en sont que des vues ou des interfaces de proposition."
status: stable
tags: [okf, canon, gouvernance]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Contexte

Le propriétaire maintient déjà la spécification OKF Federation (manaty/okf-federation) et des fédérations qui l'appliquent.

# Décision

- Les **équipes avec leurs responsabilités**, la **structure des projets** (modules, lots), les procédures et les décisions sont décrites dans des **dépôts git au format OKF v0.2**, fédérés selon **OKF Federation**.
- Les **pages Nexlink sont une vue rendue du canon OKF** ; une modification faite dans Nexlink devient une **proposition** (PR revue selon la politique de la fédération), jamais une seconde source éditable. Même règle pour tout outil open source (Plane, Forgejo issues…).
- Accord de principe pour une racine de fédération **manaty/home** (création non encore planifiée).

# Conséquences

- ameesh lit rôles, portées canoniques et `review_policies` dans `federation.yaml` et les fait respecter à l'exécution ; il n'écrit dans le canon que par proposition.
- L'état d'exécution (en cours, bloqué, attente d'un humain) reste dans ameesh et se projette sur une vue (issue par lot, page Nexlink).
- Il faut un **profil ameesh** d'OKF Federation (types Agent, Team, WorkPackage, Pipeline ; liaison identité → clé).
