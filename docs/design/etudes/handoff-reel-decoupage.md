# Câbler le handoff réel d'un agent

Proposition de découpage du 25 octobre 2026, lot #3429, demandée par
mesh-design (courrier #135293). Les lignes ci-dessous sont des lots proposés,
à ouvrir par le pilote ; elles n'autorisent aucune opération d'hôte.
Le pilote #3392 (manaty/ameesh#2569) reste distinct et en attente.

Sur `main` 84127f59aee8, [HOST-HANDOFFS](../../HOST-HANDOFFS.md) décrit
encore des adaptateurs simulés. Dans [runner.py](../../../src/ameesh/runner.py),
les appels à `readiness` et `advance_host` lisent `_lifecycle_adapter` et
`_lifecycle_requirements` par `getattr(..., None)` ; aucun enrôlement réel
n'est raccordé. [host_lifecycle.py](../../../src/ameesh/host_lifecycle.py)
refuse alors par préalables nommés `helper:L232b` et `requirements:L232c`.
La CLI handoff expose `plan/show` ; `advance/cancel/retry` existent dans
[handoffs.py](../../../src/ameesh/handoffs.py), avec adaptateur injecté.

La [fabrique adaptateur-hote-release.py](../../../deploy/mise-a-jour/adaptateur-hote-release.py)
est celle du contrôleur indépendant de **release**, avec son propre enrôlement.
Elle n'est pas une fabrique de handoff. Réutiliser un helper de déploiement ou
de santé exige un pont explicite et testé, sans confondre leurs autorités.

Les estimations sont du temps de développement et de tests ciblés ; les
revues, attentes de décision et créneaux opérateur viennent en plus. Les
lots sont sensibles ; secrets, droits et sécurité demandent deux avis
indépendants selon le canon. Chaque livraison reste honnête sur ses capacités :
une capacité absente refuse, elle ne rend jamais une réussite simulée.

| Ordre proposé | Lot à ouvrir et dépendances | Estimation | Critère de livraison |
| --- | --- | --- | --- |
| H1 | Contrat et fabrique de handoff enrôlée ; aucune dépendance nouvelle | 4 h | Une fabrique à source épinglée fournit l'adaptateur et `requirements_for(agent, host)`. Elle refuse une identité, un dépôt, une méthode ou un document manquant ; aucun chargement de code choisi par un message. Contrat et tests négatifs livrés. |
| H2 | Effets de préparation et Git ; après H1 | 6 h | `inventory`, `repository`, `revalidate`, `prepare`, `stop_source`, `publish`, `transfer`, `session` agissent via helpers admis, avec délais, reçus et reprise prouvés. SHA exact propre et poussé, caps et session sans cache OAuth transféré ; aucun démarrage cible. |
| H3 | Activation, santé et réconciliation ; après H2 | 6 h | `admit`, `enable`, `verify`, `restore_source`, `reconcile_effect` respectent les fences, prouvent le premier tour authentifié et traitent résultat incertain, crash et retour arrière. Une seule exécution possible, même après appel tardif. |
| H4 | Pont secrets et santé des comptes ; après H1, nécessaire à H3 pour un compte réel | 4 h | Livraison L232c par référence/politique, compte et destination exacts, rotation liée au tour et santé fraîche `credential_health`. Secret absent/révoqué ou login manquant refuse ; aucune connexion automatique ni repli payant. |
| H5 | Raccord au runner ; après H3 et H4 | 4 h | Construction et affectation de `_lifecycle_adapter`/`_lifecycle_requirements` avant les usages, mêmes préalables dans `readiness` et `advance_host`, connexions contrôleur dédiées. Tests runner sur drain, retour, perte et absence d'enrôlement ; rôles élus laissés à l'élection. |
| H6 | CLI opérateur `advance`, `cancel`, `retry` ; après H3 et H4 | 3 h | Résolution de la même fabrique, identité autorisée, un pas borné par appel, erreurs et métadonnées filtrées ; `retry` exige réconciliation et renouvellement explicite du TTL expiré. Pas de duplication de machine d'états. |
| H7 | Enrôlement et qualification staging ; après H5 et H6 | 3 h | Opérateur, utilisateur/unité et canaux existants nommés ; documents protégés posés par l'opérateur autorisé, contrôle des permissions et canari sans secret réel. Installation, refus et retour à la version précédente prouvés. |

Total indicatif : **30 h**, chemin H1 → H2 → H3 → H5/H6 → H7 ; H4 peut
être préparé après H1 et doit être terminé avant la qualification réelle de
H3. Le pilote #3392 vient ensuite : il porte le déplacement réel et sa recette,
pas une nouvelle implémentation cachée dans le créneau navigateur.

Le contrat commun H1–H3 doit porter l'identifiant d'opération, le bail et son
époque, ainsi que `controller_epoch` à chaque effet. Les helpers conservent
les intentions et reçus de façon durable, refusent les époques révoquées et
rendent le rejeu idempotent. Revalider capacité/réservations, canon, release,
identités et état du compte avant effet ; l'inventaire authentifié contient
hôte, fraîcheur, connexion, outils et disponibilité de références, jamais leurs
valeurs. `Requirements` nomme hôte, harness, **compte exact**, outils et alias
`secret_refs` admis ; la fabrique associe ces alias aux références privées
`fichier#CLE` de L232c sans modifier implicitement la grammaire du preflight.

`enable` doit finir dans la limite actuelle de 60 s et respecter la porte
cible ; un timeout ne prouve pas l'arrêt du travail externe. Révoquer et
quiescer l'ancienne époque, y compris ses requêtes retardées, avant tout rejeu.
Saturation des workers, perte de bail et preuve de santé absente laissent
l'opération tenue. Avant switch, `cancel` exige restauration source prouvée ;
après switch, reprise en avant sur la cible, jamais réveil aveugle de la source.
La récupération d'une source perdue est soit supportée avec preuve de
quiescence et de récupération, soit explicitement refusée ; pas de contournement
par un simple heartbeat. Tester coupure à chaque intention et effet ambigu.

H7 doit produire un manifeste d'enrôlement, **noms, chemins et permissions
seulement** : hôtes source/cible, dépôt/SHA et code des helpers, agent de test,
compte/harness/home privé, unité et caps ; opérateur et canal déjà autorisé ;
inventaire et Requirements ; délégation/restart si utilisés. Pour L232c :
`/etc/ameesh-secrets/policy.json` et `helper.json` protégés selon
[le contrat existant](../../L232C-SECRET-STORE.md), référence `fichier#CLE`,
destination autorisée, transport `ameesh-secrets` distinct ; bridge compte
`0600` dans `0700`, ou magasin système `0640` dans `0750` selon la politique.
Les documents `/etc/ameesh-release` ne sont requis que si le pont release est
retenu et autorisé. Aucun accès root, compte, privilège ni canal SSH nouveau
n'est implicitement accordé par ces lots.

Pour le login, appliquer [L232d](../../L232D-OAUTH-ADAPTERS.md) : superviseur
sous utilisateur Unix dédié hors des processus agents, token login distinct,
JSON du service **et parents** non modifiables par les agents, token illisible
par eux. Nommer le compte pilote distinct du primaire, la garde, la portée du
grant et sa révocation fournisseur. La relecture sécurité de la version et du
montage installés, les décisions owner signées et le créneau navigateur restent
les préalables de #3392 (note v2 et avis auditeur #135285 ; demande de décision
#135297 remplaçant #134341). Aucun token, code ni URL fournisseur dans Git ou
les preuves publiques. Un login seul ne livre pas le pilote de déplacement.
