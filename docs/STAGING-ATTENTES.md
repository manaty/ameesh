# Correctifs fusionnés en attente de staging

La règle du canon « Processus de lot » §11 bis du 24 octobre 2026
(`manaty/home#158`, `8238c1c99`) prévoit un staging hors coupure quand plusieurs
lots attendent un correctif déjà fusionné. La coupure suivante conserve au
moins le contenu déjà actif sur les hôtes.

`ameesh release staging-needed --project ameesh` produit un relevé JSON : lots
en attente, correctifs et jalons de fusion, hôtes en retard, attestations
datées, candidate verte et lots que cette candidate peut débloquer. Les
dépendants peuvent appartenir à Nexlink : c'est le projet du correctif qui
détermine le staging d'ameesh.

La lecture utilise les dépendances explicites et les notes `hold:` encore
actives, avec leur référence `#lot` ou `migration N`. Une description de lot,
un ancien hold levé ou un jalon de fusion annulé ne créent pas d'attente. Les
commits actifs viennent de la dernière attestation de chaque hôte
(`mise-a-jour/actif-<commit>.json`), dont la date est rendue dans le relevé.
Sans cette preuve, l'attente apparaît dans `unknown` et ne compte pas dans le
seuil. La commande ne prétend pas réaliser une nouvelle sonde distante ; la
séquence de staging garde ses contrôles réels par hôte.

Le lecteur mécanique accepte ce bloc optionnel de `processus/mecanique.yaml` :

```yaml
staging:
  waiting_lots_min: 3
  before_cutoff_seconds: 1200
  before_packet_seconds: 600
```

Les trois valeurs sont obligatoires lorsque le bloc existe. Le publier après
installation du lecteur sur les hôtes ; les lecteurs antérieurs refusent une
clé inconnue. Sans bloc, le relevé automatique est `off`, sans seuil supposé.
La protection anti-recul de la coupure est indépendante de cette activation.

Le timer existant `ameesh release tick` envoie une alerte au pilote et au
composeur configurés dans `packet_watch.projects.ameesh.notify`, ainsi qu'au
bureau. Sans cette liste, il utilise les orchestrateurs déclarés. L'alerte
est dédupliquée par destinataire, candidate, attentes et preuves actives.

Le pilote ou le bureau exécute ensuite
`ameesh release staging-needed --project ameesh --apply`. À un terminal humain,
ajouter `--actor human:<identifiant>`. Le geste relit la candidate et les
attentes sous le bail partagé avec le train et la release. Il refuse un bail
occupé ou perdu, une fenêtre trop proche, une candidate qui ne débloque pas
assez de lots ou qui ferait reculer un hôte. Il appelle la séquence ordinaire
`staging.sh` avec les lots correctifs inclus : aucun tag ni changement de
version n'est créé par cette commande.

À la coupure, une candidate antérieure ou divergente de l'actif donne un
rapport durable `kept-active`, sans tag ni déploiement. Cette garde s'applique
aussi à la réconciliation d'un tag déjà publié et est relue avant le staging.
