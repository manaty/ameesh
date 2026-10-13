# #1035 t2 / PR #817 — R1 prod, auditeur Codex

2026-10-13. Tête `ef6a717dd99ea8cbdebaf1cc279b1d95e2e35151`,
base empilée t1 `8a25bec5`. **NO-GO : 2 B/P2 sensible / 0 N.** Lecture
du delta t2 (six fichiers), du moteur t1 et du chemin réel Notifier. Aucune
suite rejouée, aucune modification produit ni activation.

## B1 — Les cibles et l'urgence P1 ne vont pas jusqu'à la livraison

exploitation._slo_breaches produit critical/urgent=true et targets owner
pour le P1. Pourtant notify.Notifier collecte les alertes humaines dans
`wanted` seulement quand le type figure dans notify.types (:1018).
slo_breach n'est pas ajouté à DEFAULT_TYPES (:83-115), et aucune autre
branche n'agit sur la cible owner. alert_briefs ignore ce rôle et retourne
seulement les noms des agents, tandis que les textes affirment « owner
notified » / « the owner is notified ». Une configuration de notification
par défaut envoie donc les briefs agents sans notifier l'owner.

L'urgence est aussi perdue : _brief_agents (:1329-1331) pose payload.urgent
seulement pour les types de URGENT_TYPES. slo_breach n'en fait pas partie,
et alert.urgent n'est pas lu. Le P1 part comme événement ordinaire malgré
le champ urgent=true de l'alerte. Ajouter le type globalement aux urgents
rendrait aussi les SLO non P1 urgents : propager le champ par épisode est
le contrat souhaité.

Attendu : brancher réellement la cible owner pour les P1, avec le routage
humain prévu et une configuration explicite ou un comportement par défaut
qui tient le contrat. Les SLO ordinaires peuvent rester filtrables, mais ne
pas affirmer une notification non réalisée. Propager alert.urgent aux mails.
Témoin nécessaire : passage complet Notifier sur un dépassement P1 et un
non P1, observation de la livraison humaine et du payload des événements,
ainsi que l'escalade/clé de déduplication. Les tests actuels vérifient les
champs de l'alerte et le texte des briefs, pas leur livraison.

## B2 — Le nouveau module de test base manque au pilote psql

tests/test_789_slo_alerts.py utilise PgTestCase, modifie priority et les
événements/jalons SQL, puis lit le nouveau champ Operations.priority. Il
n'est pas dans tests/parts/pilote-psql.txt sur cette tête. CONTRIBUTING:19-20
exige qu'un module touchant la couche base figure dans cette liste ; les
entrées sont triées et commentées (:55-57). Les neuf cas ciblés psql de
l'auteur ne remplacent pas leur inclusion dans la CI du gel.

Attendu : entrée `tests.test_789_slo_alerts` triée, commentée, puis nouvelle
tête avec CI exacte. Pas de suite à rejouer par le relecteur.

## Points conformes et portée

Le delta t2 utilise lot_steps de t1 sans réinventer les étapes non mesurées,
exclut les attentes planifiées et la file d'amélioration, et ajoute priority
à la projection Operations. reason contient étape/niveau, ce qui change la
clé de déduplication quand l'escalade monte. Niveau 1 vers assigné, niveau 2
vers pilotes de l'équipe (tous si inconnue), niveau 3 vers bureau/auditeurs.
Pas de message attaché au lot dans _brief_agents, donc le brief ne réinitialise
pas lui-même la mesure d'activité. Diff --check propre.

Tests auteur 9+27 psql/psycopg et lint rapportés, non rejoués. Base #789/t1,
condition #603 et second avis Claude restent distincts ; aucun avis sur les
blobs t1 ou un droit d'activer t2 n'est donné ici. Nouvelle tête après
correction : delta à relire, deux avis prod indépendants, CI complète exacte
sans part annulée et feu pilote selon la demande mesh-design #24922.
