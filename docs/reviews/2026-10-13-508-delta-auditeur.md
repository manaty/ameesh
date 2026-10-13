# #508 — R2 delta de refusion, Codex

Demande mty-amh-impl-07, 13/10/2026 18:05 Paris, PR #485.
Tête `321c1eb3350a5a0a04304fda30e79dbfab740e59` : c4878c499 + main b132d5d4.

GO de code sur les résolutions de refusion, 0 B / 1 N/P3 (#962).

Lecture des neuf fichiers de remerge-diff : imports TURN_COLUMNS/write_runner_seen conservés ; méthodes abstraites et concrètes runner_seen/note_unleased distinctes et conservées ; AGENT_COLUMNS garde les trois champs runner_seen et lease_wait_since ; pilote psql contient les deux ajouts ; le témoin draining garde l'appel _note_gate_wait et l'assertion runner_seen ; le commentaire roadmap conserve la date de scénario. Aucun marqueur de conflit restant dans les résolutions.

Contrat : union des noms d'opérations des parents, vérification trois voies depuis merge-base 0121af4f (tous changements d'opérations non conflictuels préservés). Compte réel 64 = 25 lectures +39 écritures, interface_total 386 ; agents.runner_seen garde sa portée H, hôte forcé, clé idempotente et refus owner non préfixé. La révalidation de l'admission à l'UPDATE et le chemin note_unleased de main sont conservés. Diff-check propre.

N/P3 #962 : docs/EXECUTEUR-MEDIEE.md conserve « 53 passent par op », mais le contrat contient 54 op +9 session/op +1 events. Suite documentaire assignée à l'auteur ; pas de changement de gel exigé.

Pas de relecture générale de tout main importé ; avis limité à l'intégration du lot et aux résolutions demandées. Aucun rejeu (canon §3). CI encore en cours et rouge #925 signalé par l'auteur ; ce GO ne donne pas le vert exact ni le feu de fusion.
