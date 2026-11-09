---
type: Study
title: "Boucle d'auto-réparation : existant, écarts et mise en œuvre"
description: "Étude A1 à A12, mesures sur sept jours et découpage proposé après décision owner #200641 a."
status: draft
tags: [auto-reparation, signaux, triage, verification, cloture]
---

# Boucle d'auto-réparation : existant, écarts et mise en œuvre

## Décision et résultat attendu

Étude du lot #6308 ([ticket #4870](https://github.com/manaty/ameesh/issues/4870)),
autorisée par la décision owner #200641, option a, réponse signée du
08/11/2026 à 11:25 UTC, approbation #250. Vérification effectuée avec
`ameesh decide show 200641 --json` : `proof_check.ok = true`.
Le modèle de présentation est l'[étude #6173](versionnement-agents.md).
Le pilote crée les lots après relecture par l'auditeur ; les unités proposées
ici ne sont ni des lots créés ni des autorisations d'exécution.

Le résultat recherché est une chaîne observable : signal durable → qualification
et rapprochement → lot ou décision → correction et preuves → livraison active →
vérification de la résolution. Les mécanismes déjà actifs restent les points
d'entrée ; il ne faut pas construire un second orchestrateur de lots.
Le tableau initial du 04/10 annonçait « tout manque » pour plusieurs capacités :
ce constat est remplacé par la lecture du code et le relevé ci-dessous.

Bornes de cette étude et de son premier découpage : **ni prod réelle, ni argent,
ni secret** ; aucun nouveau connecteur public ou droit d'accès. Les essais actifs
visent ameesh et son staging (§14). Les mécanismes existants d'autres projets,
notamment leurs sources de production, ne sont ni modifiés ni activés par ce
chantier. Les formats restent génériques et déclarés par projet, sans client
en dur. A5 est une **proposition de politique** : son inscription au canon et
l'autorisation d'une nouvelle réparation automatique exigent une décision
séparée. A9 n'accorde jamais d'approbation par silence, délai ou escalade.

## Base examinée et règles applicables

Lecture sur `origin/main` à `97285a076dee285097c412d3bd0f2649438a3257`.
Canon examiné : `b8c4ea12ca5fcac799329dd5ac54386384cbe223`,
`processus/lots.md`, responsable human:smichea, mis à jour le 02/11/2026 :
§1/§8 classes, §2 revues et panier N, §3 non-concluants, §5 actif,
§7 mécanique déterministe, §9 SLO, §11 bis/§12 paquets, §14 staging,
§16 prise en compte et retraitement des demandes. La règle de suivi externe
est `processus/demandes-github.md`, human:smichea, 20/10, expiration 19/11.

La [décision 0022](../decisions/0022-auto-reparation.md) conserve le contexte
du 04/10 et son attestation alors non signée ; l'autorisation de cette étude
repose sur #200641, vérifiée ci-dessus. Le modèle historique observé dans
l'étude initiale suggère reproduction, correction minimale et surveillance ;
ses observations externes ne sont pas une preuve de fonctionnement d'ameesh.

## Écart mesuré sur une semaine

Relevé en lecture seule de la base du mesh (`public`), réalisé le 08/11 entre
11:31 et 11:36 UTC. Fenêtre fixe, bornes UTC : **[01/11 11:30, 08/11 11:30[**.
Les cohortes de lots sont définies par `created_at`, les occurrences par la
date de leur événement. Les états, liens et fermetures sont ceux relus à
l'extraction, pas leur reconstruction historique à 11:30. Les requêtes ne
forment pas un snapshot transactionnel commun. Aucun contenu de demande,
secret, journal privé ou donnée personnelle n'est exporté dans cette étude.

| Mesure | Résultat observé | Ce que cela permet de conclure |
| --- | --- | --- |
| Lots créés, `app='ameesh'`, `type='bug'` | 197 | Cohorte de défauts déclarés, pas de tous les incidents réels |
| Lots dont la source commence par `empreinte:` | 10 pour 2 clés distinctes | La primitive est utilisée, mais pas comme source unique des défauts |
| Occurrences « Empreinte revue » dans la fenêtre | 3 notes, sur #5700 | Déduplication constatée sur ce lot ; pas trois défauts supplémentaires |
| Doublons ouverts de même empreinte, toutes dates de création | 0 groupe au moment du relevé | Compatible avec l'index unique actif ; ne prouve pas l'absence de doublons sémantiques |
| Lots de la cohorte portant un lien typé `duplicates` | 4 : #5600→#5599, #5626→#5606, #5746→#5742, #5872→#5826 | Doublons reconnus et tracés ; borne basse, pas un taux global de déduplication |
| Lots de défaut de la cohorte avec `superseded_by` | 31 | Remplacements, pas nécessairement doublons : ne pas les ajouter aux quatre précédents |
| Récidives sur la clé générique du tic paquet | 8 récidives après #5740, jusqu'à #6261 | Une clé d'échec de commande peut réunir des causes différentes |
| Alertes `alert.raised`, `project='ameesh'` | 14 : 10 `decision_pending`, 3 `decision_overdue`, 1 `dead_runner` | 13 référencent un lot ; le `dead_runner` n'en référence pas dans son événement |
| Retours d'état QA → build sur les lots ameesh | 19 transitions sur 14 lots, acteurs agents | Reprises de travail constatées ; pas une mesure des redémarrages manuels de processus |
| Progression staging, journal global du mesh | 1 426 snapshots, 29 `run` distincts | Un snapshot n'est pas une tentative ; ne pas compter 1 426 reprises |
| Attestations staging ameesh / événements `staging.request` du mesh | 14 / 0 dans la fenêtre | Des livraisons attestées existent ; le chemin de demande élu n'est pas attesté par cette fenêtre |

Le cas #5740 ([#4371](https://github.com/manaty/ameesh/issues/4371)) est fermé
sur une panne externe GitHub ; #5872 ([#4467](https://github.com/manaty/ameesh/issues/4467))
sur un contrat négatif couvert par #5826 ; #6261
([#4825](https://github.com/manaty/ameesh/issues/4825)) sur un autre contrat
négatif de composition. Leurs notes de fermeture montrent des causes
différentes sous `empreinte:ameesh:paquet-horaire:tick:paquet-auto rc N pour h-*`.
Les huit récidives ne sont donc pas huit régressions démontrées d'un correctif,
ni huit réparations à automatiser. À la base examinée, #6272 a déjà séparé
les sorties : `COMPOSITION_RED_RC=5` pour un paquet rouge par son contenu,
sans lot d'empreinte ; rc 1 reste un échec opérationnel. Voir
[paquet-horaire.py](../../../scripts/paquet-horaire.py),
[paquet-auto.py](../../../scripts/paquet-auto.py) et
[test_6272_paquet_rouge_composition.py](../../../tests/test_6272_paquet_rouge_composition.py).
Ce garde est à conserver et à mesurer après livraison, pas à réimplémenter.
Les occurrences antérieures sont les témoins de non-régression A2/A3/A11.

**Défauts sans lot : nombre inconnu.** L'alerte sans référence de lot signale
un runner, pas nécessairement un défaut de code ; une alerte de décision n'est
pas une panne. Sans journal exhaustif des signaux avec leur issue de triage,
on ne connaît ni le dénominateur ni les défauts perdus avant leur entrée dans
le mesh. L'absence de `fingerprint_source_state` pour ameesh ne signifie pas
absence de détection : le tic ouvre ses empreintes directement ; les deux
curseurs relevés concernent un autre projet et ses environnements.

**Reprises manuelles : nombre total inconnu.** Les 19 retours QA → build sont
mesurables, mais leur acteur agent ne distingue pas commande de diagnostic,
automate ou retour de revue ; ils ne prouvent pas 19 reprises manuelles.
Une recherche textuelle dans les notes humaines retrouve une réponse signée
#189578 autorisant une interruption/relance de session (#5495), pas la preuve
du geste. Le journal n'unifie pas encore `origin=operator|agent|automatic`,
intention et résultat d'une reprise. A11 doit produire cette mesure avant
qu'un gain d'automatisation soit annoncé.

### Requêtes reproductibles

Lectures uniquement, à exécuter avec un profil `read_only=True` et une borne
de durée. Le résultat courant peut évoluer pour les états/liens ; les bornes
restent fixes. Les requêtes suivantes reprennent les unités de compte du relevé.

```sql
-- Cohorte des défauts, puis lots d'empreinte et clés.
WITH cohort AS (
  SELECT * FROM work_items WHERE app='ameesh' AND type='bug'
    AND created_at >= '2026-11-01T11:30:00Z'::timestamptz
    AND created_at <  '2026-11-08T11:30:00Z'::timestamptz
)
SELECT count(*) AS bugs,
       count(*) FILTER (WHERE source LIKE 'empreinte:%') AS empreinte_lots,
       count(DISTINCT lower(source)) FILTER (WHERE source LIKE 'empreinte:%') AS cles,
       count(*) FILTER (WHERE superseded_by IS NOT NULL) AS remplaces
FROM cohort;

-- Doublons sémantiques déjà reconnus : un lot, pas une ligne par événement.
SELECT count(DISTINCT w.id) FROM work_items w
JOIN work_item_links l ON l.work_item_id=w.id AND l.kind='duplicates'
WHERE w.app='ameesh' AND w.type='bug'
  AND w.created_at >= '2026-11-01T11:30:00Z'::timestamptz
  AND w.created_at <  '2026-11-08T11:30:00Z'::timestamptz;

-- Doublons encore ouverts par identité technique (toutes dates).
SELECT lower(source), count(*) FROM work_items
WHERE app='ameesh' AND source LIKE 'empreinte:%'
  AND state NOT IN ('merged','promoted','delivered','closed')
GROUP BY lower(source) HAVING count(*)>1;

-- Alertes journalisées et référence de lot absente de leur événement.
SELECT data->>'alert' AS alerte, count(*) AS levees,
       count(*) FILTER (WHERE data->>'lot' IS NULL) AS sans_reference_lot
FROM ameesh_events WHERE type='alert.raised' AND project='ameesh'
  AND created_at >= '2026-11-01T11:30:00Z'::timestamptz
  AND created_at <  '2026-11-08T11:30:00Z'::timestamptz
GROUP BY 1;

-- Calculer lag sur l'histoire entière avant de filtrer la fenêtre.
WITH transitions AS (
  SELECT e.*, lag(e.state) OVER (PARTITION BY work_item_id ORDER BY e.id) AS precedent
  FROM work_item_events e JOIN work_items w ON w.id=e.work_item_id
  WHERE w.app='ameesh'
)
SELECT count(*) AS reprises, count(DISTINCT work_item_id) AS lots
FROM transitions WHERE precedent='qa' AND state='build'
  AND created_at >= '2026-11-01T11:30:00Z'::timestamptz
  AND created_at <  '2026-11-08T11:30:00Z'::timestamptz;

-- Journal global : conserver les runs distincts et les types séparés.
SELECT type, count(*) AS evenements, count(DISTINCT data->>'run') AS runs
FROM ameesh_events
WHERE type IN ('staging.sequence','staging.attested','staging.request')
  AND created_at >= '2026-11-01T11:30:00Z'::timestamptz
  AND created_at <  '2026-11-08T11:30:00Z'::timestamptz
GROUP BY type;
```

## Capacités A1 à A12

Les coûts ci-dessous sont des estimations de travail auteur, tests ciblés
compris, hors attente de revue. Ils sont détaillés dans le découpage, pas
additionnels à celui-ci. La classe distingue le résultat de
`ameesh review-class --project ameesh --json <chemins>` (canon cité plus haut)
et le risque fonctionnel du §1 : un nouveau nom Python tombe souvent en
`light` par défaut, ce qui ne rend pas légère une modification de verrous,
d'autorité ou de politique. Reclasser le diff réel au gel.

### A1 — Réception durable des signaux

**Existant.** [github_watch.py](../../../src/ameesh/github_watch.py) (#2845,
#2955, #5506) reçoit tickets/commentaires, garde ses curseurs et rappelle les
entrées sans preuve de traitement. [fingerprints.py](../../../src/ameesh/fingerprints.py)
(#3885, #5968) lit des exports validés et garde un curseur par environnement
([stockage](../../../src/ameesh/storage/postgres/fingerprint_watch.py), migration
0549). [events.py](../../../src/ameesh/events.py) (L85) fournit le journal
durable ; le fil et la boîte de courrier restent des sources d'interactions.
Les adaptateurs et leurs états sont donc déjà présents ; le reçu commun reste à définir.

**Écart et proposition.** Aucun reçu commun ne relie toute entrée à son
triage, lot, panier ou décision ; le taux de perte reste inconnu. Définir une
enveloppe minimale `repair-signal/1` : projet, environnement, source et ID
natif, occurrence, clé de cause si connue, version observée, horodatages,
référence de preuve autorisée, résultat de traitement. D'abord adapter les
événements internes et la veille existante, sans nouveau webhook ni email.
Si une persistance manque, ajouter une table additive avec unicité
(projet, environnement, source, ID natif), état et curseur transactionnel.
Ne pas recopier de conversation ou de pile brute ; un signal est une donnée,
jamais une instruction ni une autorisation. Une entrée invalide est conservée
comme refus borné sans avancer le curseur à tort ; erreurs de source suivies
sans tempête de lots.

**Coût/risque/classe.** 7 h (lecteur, persistance, raccordement). Risques :
perte à la reprise, double écriture, amplification de texte non fiable.
Lecteur pur `repair_signals.py` : outil léger ; stockage/migration : outil et
classe finale **sensibles**, concurrence et données. Contrat N-1 obligatoire.

### A2 — Qualification, sévérité et routage

**Existant.** La priorité par environnement est dans `fingerprints._priority_for`
(#5987) ; [incident.py](../../../src/ameesh/incident.py) (#1681, #5627)
distingue faits, candidats et attribution inconnue ; les alertes ont déjà
leurs types et destinataires ([alert_briefs.py](../../../src/ameesh/alert_briefs.py),
L132). Le panier [backlog.py](../../../src/ameesh/backlog.py) (#2584) regroupe
les N sous le seuil de valeur du canon, sans suite d'avis automatique.

**Écart et proposition.** La clé générique des huit récidives ne dit pas
« défaut du composeur » ; #6272 qualifie déjà ce cas précis dans les scripts.
Généraliser la trace de qualification aux autres entrées, sans doubler cette
garde. Produire une qualification motivée : panne produit,
contrat négatif déjà couvert, infrastructure non concluante, saisie/droit voulu,
évolution, question ou inconnu. Règles déterministes sur signaux structurés ;
le jugement d'un agent reste une proposition avec preuve et confiance, jamais
une instruction exécutée. Inconnu va au triage du pilote ; évolution attend
sa décision ; N suit le panier existant. Conserver la P1 existante de §16
pour ses sources actuelles ; le nouveau pilote ameesh staging ne crée pas
une promotion P1 sur la seule présence d'un testeur.

**Coût/risque/classe.** 3 h. Risque : une panne masquée ou un incident externe
transformé en correction. Fonction pure `repair_triage.py` et fixtures :
outil et classe **légers** ; un changement de politique passe par A5 sensible.

### A3 — Identité, déduplication et récidive

**Existant.** `fingerprints.open_lot` (#3885) sérialise par verrou consultatif,
index unique partiel 0492/0518, note un lot ouvert et crée une récidive après
état terminal. #5968 sépare les curseurs d'environnement et conserve N-1.
`incident.covering_lots` (#5627) évite un incident CI si les tests ont déjà
un correctif ouvert. Les liens typés (#4167, migration 0501) et les références
idempotentes de lots (#6090) existent. Le relevé confirme trois notes, zéro
doublon technique ouvert, mais quatre doublons sémantiques reconnus.

**Écart et proposition.** Séparer identité d'occurrence et identité de cause.
Un code de sortie ne prouve pas la cause. Garder les clés actuelles comme
alias, enrichir les discriminateurs structurés (test/contrat, composant,
raison stable), conserver la révision comme contexte sans la confondre avec
l'autorité de rapprochement. Préfiltre exact, puis proposition conservatrice
de rapprochement ; pas de fusion sémantique par simple similarité de titre.
Les lots distincts restent liés explicitement. Distinguer récidive d'un même
symptôme, nouvelle cause et observation d'une ancienne version encore active.
Pas de réécriture massive des sources historiques ni de suppression d'index.

**Couverture réelle des raccords A3 au commit examiné (`aa300b26`, 09/11/2026).**
`fingerprints.observe_cause` accepte deux entrées optionnelles dont dépendent
les branches « alias ancien » et « ancien SHA » : `legacy_key` (une clé
d'empreinte historique, convertie en alias `empreinte:<projet>:<clé>`) et
`is_ancestor` (le prédicat d'ascendance stricte d'A4). Au balayage U6 de ce
commit, `repair_observation.scan` ne produit aucun `fingerprint_key` pour
`alerts` ni `github_watch`, et `ingest` ne fournit pas de prédicat : les deux
branches ne sont donc exercées que par injection directe dans les tests.
Leur absence reste conservatrice et n'attribue jamais une cause inconnue :
`cause_id` ne vient que des discriminateurs exacts d'U3 (identité vide →
relation `unknown`), et une récidive d'une cause exacte sans preuve de version
reste `same_cause_version_unknown`, jamais `old_version` ; le reçu conserve le
verdict A4 et sa raison. Un `fingerprint_key` invalide est isolé par signal
depuis `aa300b26` : la page U5 est soldée avant l'observation, et l'échec d'un
reçu n'interrompt ni les autres signaux ni le curseur. L'injection du
prédicat est faite par #6486. Depuis #6485, le balayage des alertes lit le
champ `fingerprint_key` de l'événement `alert.raised` et le porte jusqu'à A3
par le chemin de production. Une clé absente ou JSON `null` ne crée aucun
alias. Une clé inutilisable (non textuelle, vide, trop longue, secret ou
adresse) refuse sa ligne en `invalid_envelope`, comptée, au lieu de faire
perdre le reçu après réception. Aucun émetteur d'alerte n'écrit encore ce
champ : l'alias reste dormant en production jusqu'à ce qu'un producteur
l'écrive, et `github_watch` n'en a pas.

**Coût/risque/classe.** 5 h. Risques : deux causes fusionnées ou une rafale
de récidives. Outil léger pour `fingerprints.py`, sensible pour son stockage ;
classe finale **sensible** (§1 verrous/unicité), même si le diff Python seul
reste classé léger par défaut.

### A4 — Corrélation à la version active

**Existant.** Les empreintes portent `revision`. Les événements
`staging.attested` (#3981), [staging_watch.py](../../../src/ameesh/staging_watch.py)
(#4880, #5558, #5693) et [staging_pending.py](../../../src/ameesh/staging_pending.py)
connaissent SHA, hôtes, séquences et réparations par descendants. Quatorze
attestations existent dans la fenêtre ; une proximité temporelle n'est pas
une preuve que le correctif tournait sur l'hôte du signal.

**Écart et proposition.** Lecteur pur qui joint signal, environnement, hôte,
SHA observé et attestation explicite. Rendre `avant-correctif`,
`correctif-actif`, `mixte` ou `inconnu`, avec références ; déduire la causalité
du symptôme reste du diagnostic. Une flotte mixte n'est pas globalement guérie.
Une version absente/inconnue n'autorise ni fermeture ni relance de demande.

**Coût/risque/classe.** 2 h pour le lecteur, intégration dans A10. Risque :
fermeture sur une ancienne version ou mauvais environnement. Lecteur
`repair_correlation.py` : outil et classe **légers** ; effets de clôture en A10
sensibles. Pas de changement du déploiement dans cette unité.

### A5 — Politique d'auto-réparation proposée au canon

**Existant.** Canon §1/§8 (classes), §2 (revue/panier), §7 (mécanique),
§9 (SLO) ; [mechanics.py](../../../src/ameesh/mechanics.py) compile les
paramètres. Les budgets, pauses et capacités du runner restent leurs bornes
actuelles ; cette étude ne les relève pas. Il manque une politique commune
qui relie admission du signal et réparation autorisée.

**Proposition, non activée.** Une politique versionnée par projet déclarerait
sources/environnements admis, natures, sévérités, portée de fichiers,
capacités, nombre de tentatives, temps maximal, plafonds de ressources,
chemins sensibles exclus de la réparation sans décision et interrupteur
durable. Un motif de refus accompagne chaque décision d'admission.
Politique absente/inconnue ou version non comprise : observation et triage
continuent, **aucune nouvelle réparation automatique** n'est admise.
Le plan, le diff et le canon exact restent liés ; un champ du signal ne peut
pas accorder la capacité qu'il demande. L'interrupteur arrête les admissions,
pas brutalement une action engagée : sortie sûre par son protocole existant.

**Coût/risque/classe.** 3 h pour schéma/lecteur testé ; décision et écriture
canon chiffrées séparément par le pilote après relecture. Risque : accorder
une autorité par défaut ou par retard de lecteur. `repair_policy.py` ressort
léger, `mechanics.py` sensible ; classe finale **sensible** (consigne/admission).
Aucune modification du canon dans #6308 ni dans les lots d'observation.

### A6 — Réparation par un agent et garde sur le diff

**Existant.** [runner.py](../../../src/ameesh/runner.py), sessions et branches,
empreintes de fichiers des lots (#3185), transitions [work.py](../../../src/ameesh/work.py),
relectures et paquets existent. QA → build est déjà borné à deux retours
(`MAX_QA_LOOPS`) ; cela ne borne pas toutes les reprises d'exécution.
La reproduction avant correction n'est pas une preuve structurée commune.

**Écart et proposition.** Plan de réparation conservé sur le lot existant :
symptôme et témoin négatif reproductible, tête/base, périmètre autorisé,
critère de réussite, tests attendus. L'agent corrige et explique ; le code
compare le diff final au plan et à la classe, y compris renommages,
suppressions, dépendances et configuration. Une sortie de périmètre
redemande qualification/revue ; elle ne devient pas autorisée parce que le
correctif est petit. Aucune résolution autonome d'un conflit sémantique,
ni modification de droits/hôte par ce chemin. Ni token ni secret ajouté au
contexte de l'agent. Les portes existantes restent obligatoires.

**Coût/risque/classe.** 6 h, en deux unités. Risques : réparation sans
reproduction, diff élargi après revue, boucle de corrections. Module nouveau
léger par défaut, `runner.py` et `review.py` sensibles ; classe finale
**sensible** (exécution et garde de changement).

### A7 — Autorité liée au contenu exact

**Existant.** [actions.py](../../../src/ameesh/actions.py) (L6/L44) lie les
arguments canonisés et leur digest aux reçus ; un nonce ne lance qu'une
tentative. Une issue inconnue ne justifie pas un rejeu aveugle. Les gels,
verdicts et portes de [review.py](../../../src/ameesh/review.py) et `fire.py`
portent la tête ; les paquets portent leurs manifestes. Ce chantier ne crée
pas un nouveau système de signatures.

**Écart et proposition.** Relier le plan A6 à la tête gelée et aux preuves A8.
Pour tout geste déjà soumis à reçu, utiliser l'API existante : digest du
contenu autorisé, portée, identité, expiration et consommation vérifiés à
l'exécution. Changement de contenu = autorisation hors portée, sans convertir
un ancien accord en accord sur le nouveau diff. Une enveloppe de preuve ou
un hash Git n'est pas une signature. Toute action hors bornes de l'étude
reste hors du chemin d'exécution, même avec une demande reçue par courrier.

**Coût/risque/classe.** 3 h. Risque : confusion preuve/permission et course
entre contrôle et effet. `actions.py` ressort léger, `review.py` sensible ;
classe finale **sensible** (droits/porte), tests de concurrence si nécessaire.

### A8 — Preuves de vérification

**Existant.** [prevalidation.py](../../../src/ameesh/prevalidation.py) (#1770)
lie SHA, plan, modules réellement exécutés, environnement, exclusions et
résultats. [test_attestation.py](../../../src/ameesh/test_attestation.py)
(chantier #6173, unité F #6184) valide une enveloppe par contenu/arbre et
portée ; une empreinte de patch ne suffit pas. Les états QA/build ne sont
donc pas les seules preuves disponibles.

**Écart et proposition.** Réutiliser ces formats pour relier témoin avant,
test après et contrat de non-régression au plan A6 et à la tête exacte.
Conserver commande structurée, résultat, modules exécutés, digests et liens
vers sorties autorisées, sans copier des journaux bruts sur GitHub.
Rouge concluant, non-concluant et non exécuté sont séparés ; absence de
preuve n'est jamais vert. §3 continue de s'appliquer : le non-concluant ne
retient pas à lui seul paquet/release ; son manque est consigné et traité à
part. CI complète du paquet et revue indépendante restent inchangées.

**Coût/risque/classe.** 3 h. Risques : réutilisation sur autre arbre,
non-concluant converti en vert ou tests seulement annoncés. Format pur léger
selon l'outil ; intégration `prevalidation.py` sensible ; classe finale
**sensible** dès consommation dans la garde A6/A7.

### A9 — Décisions, échéances et escalades

**Existant.** [decisions.py](../../../src/ameesh/decisions.py) (L124/L233,
#4256) résout un destinataire, conserve échéance et réponse ; la file est la
source des attentes humaines. [slo.py](../../../src/ameesh/slo.py) (#789,
#3288) mesure les étapes et les escalades vers assigné, pilote puis bureau/
auditeur ; [alert_briefs.py](../../../src/ameesh/alert_briefs.py) adresse les
rappels. Les 13 alertes de décision de la fenêtre prouvent l'usage, pas une
absence de mécanisme d'échéance comme l'indiquait l'ancien tableau.

**Écart et proposition.** Relier le signal/plan à une demande unique existante,
avec les options et l'autorité requise, plutôt que multiplier les courriers.
L'absence du destinataire conduit au substitut déclaré par le canon, ou à un
routage non résolu ; ne pas inventer de délégation. Le dépassement déclenche
un rappel/escalade, **jamais une option choisie ni une approbation implicite**.
Un geste irréversible reste impossible sans l'autorité explicite valide ;
il demeure de toute façon exclu de la première activation. Une réaction,
un texte cité ou un accord d'agent ne sont pas une signature owner.

**Coût/risque/classe.** 2 h. Risques : attente sans question, doublon de
demande, destinataire périmé ou délai pris pour un feu. `decisions.py` :
outil et classe **sensibles**. Réutiliser ses échéances, pas une seconde file.

### A10 — Livraison, surveillance, récidive et avis au demandeur

**Existant.** §5/§16 fixent « actif », et pour les pannes concernées le
retraitement des demandes. `staging_watch` surveille déjà l'après-lancement,
les hôtes malsains, les retours et la réparation par séquence descendante.
`fingerprints` rouvre en récidive après état terminal ;
[plan_github.py](../../../src/ameesh/plan_github.py) et la veille assurent le
miroir et le suivi des tickets. L'attestation relue du 08/11 à 10:34 nomme
les trois VM actives et deux hôtes reportés : elle ne dit pas tout hôte actif.

**Écart et proposition.** Joindre A4/A8 à une observation de résolution,
référence de livraison et fenêtre de surveillance définie par politique.
L'absence de nouveaux signaux n'est une preuve utile que si la source est
fraîche et son exposition connue. Un signal d'ancienne version ne prouve pas
la régression du correctif. Récidive après version active : relier le lot
suivant et rouvrir le diagnostic, sans effacer l'historique ni livrer sur
simple silence. Notifier une fois par transition/révision au ticket existant,
avec motif d'attente utile. Pour le staging ameesh de cette tranche, qualifier
la résolution observée ; ne pas prétendre fournir le retraitement des
requêtes des produits externes. Cette intégration produit §16 reste séparée
et n'est pas incluse dans les estimations ci-dessous.

**Coût/risque/classe.** 5 h pour corrélation, suivi et notifications. Risques :
faux « réparé », doublons d'avis, confusion fusion/actif. Lecteur nouveau et
`plan_github.py` légers par défaut ; persistance, transitions concurrentes et
raccordement à la garde : classe finale **sensible**. Aucun déploiement nouveau.

### A11 — Reprise des exécutions

**Existant.** Le runner dispose de backoff, délais persistés de rate-limit,
baux et refus de reprise immédiate (#5856) ; `reprise.py`/son stockage
portent la reprise de session, pas le retraitement des demandes utilisateur.
Le tic de paquet ouvre déjà des lots d'empreinte. Les 19 retours QA → build,
les 29 runs staging et les huit récidives sont trois mesures différentes.

**Écart et proposition.** Journaliser intention et issue de reprise avec
origine (opérateur, agent, automate), cause stable, tentative, opération et
clé d'idempotence. Ne pas créer un retry générique par-dessus chaque backoff.
Déclarer quelles opérations déjà autorisées sont rejouables : lecture ou
intention non engagée, par exemple. L'effet engagé d'issue inconnue passe
par réconciliation, pas par relance ; un bail expiré ne prouve pas l'arrêt du
processus. Budget partagé durable par cause/épisode, distinct des boucles QA,
consommé atomiquement ; à épuisement, un seul lot de diagnostic ou note sur
le lot existant. Une panne externe ou un contrat négatif attendu ne déclenche
pas une modification automatique du composeur.

**Coût/risque/classe.** 6 h, journal puis intégration bornée. Risques :
double effet, compteur remis à zéro à la relève, tempête récursive de lots.
`repair_retry.py` léger par défaut ; runner/stockage sensibles ; classe
finale **sensible** (§1 concurrence/exécution).

### A12 — Balayage, surveillance de la boucle et synthèse

**Existant.** [housekeeping.py](../../../src/ameesh/housekeeping.py) (L73,
L219, #5858) applique quotas, preuves de propriétaire et conservation des
clones ; ce n'est pas un réparateur général. Les rôles élus
[role_run.py](../../../src/ameesh/role_run.py), les SLO, la veille #5506 et
[packet_watch.py](../../../src/ameesh/packet_watch.py) (#2601, #5941)
surveillent déjà plusieurs abandons, avec leurs verrous et destinataires.

**Écart et proposition.** Une passe bornée lit les signaux sans issue, liens
cassés, plans sans preuve et épisodes sans progression ; elle note ou route
vers le lot existant. Curseur durable, bail/époque et unicité d'effet,
reprise après perte d'hôte, synthèse dans le fil au changement seulement.
La boucle ne peut pas se réparer elle-même en récursion : une panne de sa
passe produit un constat borné repris par l'auditeur et l'interrupteur A5.
Aucun nouveau ménage destructif, aucune clôture ou suppression par ancienneté.

**Coût/risque/classe.** 3 h. Risques : oublier un signal hors limite de page,
répéter les avis à chaque tour ou avoir deux détenteurs actifs. Modules
nouveaux/`role_run.py` légers par défaut ; classe finale **sensible** pour le
bail, le curseur et les effets concurrents. Le rapport pur reste léger.

## Découpage proposé au pilote

Estimations auteur avec tests ciblés, hors attente et revues ; chaque unité
fait au plus une journée. Noms nouveaux indicatifs, aucun numéro de migration
réservé par cette étude. Réserver les migrations avant de les écrire.
Classes finales conservatrices selon §1, après vérification des chemins avec
`review-class` ; toute unité qui touche la porte reste sensible même si son
nouveau fichier Python reçoit `light`. Pas de CI de lot hors exception canon ;
contrats locaux puis CI complète du paquet. Les suites SQL passent par
`ameesh heavy`, et les témoins de concurrence exercent deux écrivains réels.

| Unité | Livrable actif et fichiers principaux proposés | Tests ciblés attendus | Dépendances | Coût / capacité | Classe |
| --- | --- | --- | --- | --- | --- |
| U1 | Rapport A1–A12 en lecture seule, `repair_report.py` ; export des compteurs et limites | Fenêtre UTC fixe, alertes ≠ défauts, événements ≠ runs, valeurs inconnues, cohorte et liens | Aucune | 2 h, DeepSeek | Léger |
| U2 | Enveloppe et adaptateurs purs A1, `repair_signals.py`, fixtures des sources existantes | ID stable, environnement obligatoire, champ inconnu, texte non fiable sans effet, absence de donnée brute | Aucune | 2 h, Codex ou DeepSeek | Léger |
| U3 | Qualification A2 et préfiltre A3, `repair_triage.py` | Témoins #5740/#5872/#6261 et garde #6272 conservée, non-concluant, évolution, refus voulu, inconnu et panier N | U2 | 3 h, DeepSeek | Léger |
| U4 | Corrélation pure A4, `repair_correlation.py` | Version ancienne, flotte mixte, attestation absente, source muette/périmée, environnement distinct | Aucune, formats staging existants | 2 h, DeepSeek | Léger |
| U5 | Journal A1 et curseur/issue de triage, `storage/postgres/repair_signals.py`, interface et migration additive | N-1 ignorant les colonnes nouvelles, double réception, crash avant/après commit, pagination, données non exposées par défaut | U2 | 3 h, Codex | Sensible (SQL/concurrence) |
| U6 | Raccordement observable A1/A2 aux entrées internes existantes, `role_run.py`, `github_watch.py`, lecteur de rapport | Relève de détenteur, entrée reçue deux fois, panne isolée, issue lot/panier/décision ; aucune ouverture additionnelle en observation | U1, U3, U5 | 2 h, DeepSeek | Sensible (curseur/bail) |
| U7 | Identité de cause et récidive A3, `fingerprints.py`, stockage existant | Deux écrivains, fermeture concurrente, alias ancien, même symptôme/deux causes, ancien SHA, couverture CI existante | U3, U5, U6 | 5 h, Codex | Sensible |
| U8 | Schéma/lecteur A5 et explication des refus, `repair_policy.py`, `mechanics.py` | Absent/inconnu = observation, N-1, aucun effet d'un signal sur l'autorité, interrupteur durable | U2 ; décision A5 avant toute écriture canon | 3 h, Codex | Sensible |
| U9 | Plan et témoin de reproduction A6, `repair_execution.py`, dossier de preuve du lot | Témoin négatif absent, périmètre incomplet, plan lié à la tête, aucune admission sans politique | U3, U7, U8 | 3 h, DeepSeek | Sensible (admission) |
| U10 | Preuve A8 reliée au plan, `test_attestation.py`, `prevalidation.py` | Avant/après réellement exécutés, arbre changé, portée différente, exclusion/non-concluant, digest ≠ signature | U9 | 3 h, Codex | Sensible |
| U11 | Diff et porte A6/A7, `repair_execution.py`, `review.py`, API actions existante | Rename/suppression/dépendance, tête changée après revue, reçu hors portée/expiré/réutilisé, concurrence contrôle/effet | U9, U10 | 3 h, Codex | Sensible |
| U12 | Intégration de réparation A6 aux branches/lots/runner existants, `runner.py` | Admission refusée, limite atteinte, perte de bail, aucune fusion directe, reprise sans doublon | U6, U11 ; décision A5 et politique active requises | 3 h, Codex | Sensible |
| U13 | Raccordement A9 à la file de décisions, `decisions.py`, adaptateur de triage | Une demande par plan, échéance/rappel, substitut absent, silence et réaction sans approbation | U5, U8 ; pas besoin d'activer U12 | 2 h, DeepSeek | Sensible |
| U14 | Journal d'intentions/issues de reprise A11, `repair_retry.py`, stockage via U5 | Origines distinctes, intention ≠ effet, reprise manuelle mesurable, compteur conservé à la relève | U5, U6 | 3 h, DeepSeek | Sensible |
| U15 | Reprise bornée A11 raccordée à un chemin déjà autorisé du runner | Backoff non doublé, deux détenteurs, budget épuisé, effet inconnu sans rejeu, un diagnostic sans récursion | U7, U8, U14 ; politique séparément autorisée | 3 h, Codex | Sensible |
| U16 | Suivi A10 de résolution en staging, `repair_monitor.py`, A4 et journal existant | Correctif actif par hôte, signal ancien/nouveau, source périmée, récidive liée, aucune clôture sur silence | U4, U7, U10 | 3 h, DeepSeek | Sensible |
| U17 | Avis A10 par transition, `plan_github.py`, fil et journal U5 | Relance après crash, destinataire indisponible, ticket/lot cohérents, avis unique sans contenu brut | U13, U16 | 2 h, DeepSeek | Sensible (unicité des effets) |
| U18 | Passe et synthèse A12, `repair_sweep.py`, `role_run.py` | Pagination exhaustive bornée, reprise de curseur, bail perdu, source défaillante, synthèse sans tempête ni boucle | U6, U14, U16 | 3 h, Codex ou DeepSeek | Sensible |

Total indicatif : **50 h auteur** ; revue, qualification staging et observation
ne sont pas incluses comme heures de développement. U1, U2 et U4 sont
démarrables indépendamment après relecture. U3 suit U2 ; U5 peut préparer
la persistance en parallèle. Les fichiers de stockage et `role_run.py` sont
des empreintes communes à réserver explicitement, pas dix lots concurrents.
Le pilote ajuste la capacité et peut regrouper des unités consécutives.

U1 est livré utilisé comme rapport, U2/U3/U4 par ce lecteur, U5/U6 par
l'observation réelle des entrées : ne pas accumuler de bibliothèques dormantes.
U8 puis U9–U12 et U15 ne sont pas lancés comme une vague éteinte en attendant
l'owner : le pilote obtient la décision A5 avant de planifier leur activation.
U13/U14/U16/U18 apportent suivi et mesure indépendamment d'une réparation
nouvellement autorisée. L'intégration à un second projet se fera ensuite par
son adaptateur, avec qualification et autorisations propres, sans chemin ou
politique client dans le moteur partagé.

## Mesure, activation et retour arrière

**Mesure initiale.** U1 publie le relevé reproductible et ses inconnues ; U5/U6
rendent mesurables les entrées reçues, refusées, triées et sans issue, par
source/environnement. U14 distingue reprises demandées, engagées et achevées
par origine, et celles évitées par idempotence. Les tableaux gardent les
inconnus et les sources périmées, plutôt que des zéros. Suivre ensuite : délai
signal → lot assigné (SLO §9), doublons exacts et rapprochements corrigés,
récidives après version active, corrections fondées sur un témoin négatif,
non-concluants par cause, cycles/temps consommés, arrêt par budget et temps
signal → résolution observée. Les 197 lots ne forment pas le dénominateur du
taux de couverture ; il faut le nombre de signaux éligibles effectivement reçus.

**Critères de passage.** Rejouer les témoins connus (quatre doublons reconnus,
huit récidives génériques et versions mixtes) sans attribuer automatiquement
une cause inconnue. Observation active sur les sources internes, puis bilan
sur sept jours ou cohorte explicitement bornée si le pilote tranche avant :
compteurs, taille, durée, entrées non traitées, erreurs de classification et
reprises opérateur séparées. Aucun objectif de baisse chiffrée n'est déduit
d'un dénominateur absent. Critères fonctionnels : aucune occurrence perdue
sur les scénarios de crash, aucun double effet sous concurrence, aucun échec
non concluant présenté vert, aucune admission sans politique valide, aucune
autorité obtenue par délai. Un défaut observé ouvre/alimente son lot de cause,
les manques de mesure restent déclarés.

**Activation par vagues réellement utilisées.** Lecteurs compatibles d'abord,
puis persistance additive avant redémarrage des écrivains (§11 bis), puis
adaptateurs en observation et rapport consulté par pilote/auditeur. Préserver
les écritures N-1, les anciens curseurs et les clés d'empreinte. Après décision
séparée A5, déployer les lecteurs du canon avant ses nouvelles données,
activer une portée ameesh staging bornée, et vérifier une chaîne réelle
jusqu'à résolution ; revue indépendante et paquet inchangés. Pas de passage
en prod réelle ni de réparation de données dans cette activation. Chaque
vague est mesurée et utilisée avant d'ouvrir la suivante (§5).

**Retour.** L'interrupteur durable de la politique arrête uniquement les
nouvelles admissions de réparation. Les lecteurs, l'entrée durable, le suivi
et les demandes de décision restent actifs ; aucune demande n'est perdue ou
marquée traitée par le repli. Une opération engagée suit son protocole de
sortie/réconciliation et garde sa clé ; jamais de kill/rejeu pour « repartir
propre ». Revenir aux chemins de triage existants, avec le lot et les preuves
conservés ; ne pas redescendre les données ou supprimer les colonnes.
Le retour du code N-1 doit rester lisible après l'écriture N, sinon la vague
n'est pas qualifiée. Le pilote et l'auditeur vérifient l'arrêt des admissions,
le suivi des en-cours et la reprise des signaux en attente. Cette procédure
est une proposition à réaliser/tester dans les unités concernées, pas un
interrupteur nouveau déjà disponible.
