# Diagnostics des contrôles VM

Les argv de `enrollment.example.json` et de l’étape 6 du README restent
`/usr/bin/python3 -I /opt/ameesh-vm-adapter/checks/health.py` suivis de
`doctor` ou `version-mode`. Python peut être un lien root : chaque maillon
et chaque parent traversé doivent appartenir à root ; les répertoires et
la cible finale ne doivent être inscriptibles ni par le groupe ni par les
autres. Les bits de mode d’un lien sont ignorés, son propriétaire est vérifié.
Cela couvre aussi les liens intermédiaires de `/etc/alternatives`.

Le producteur publie ses échecs sous forme `étape:ClasseException` dans le
champ `reason` du reçu rouge et sur stderr, préfixé de `vm-health`.
Les étapes sont `setup`, `boot`, `verifier`, `inventory`, `runners-before`,
`venv`, `metadata`, `doctor`, `runners-after` et `publish`. La classe vient
d’une liste fixe ; une classe inconnue est ramenée à `Exception`. Aucun
texte d’exception, chemin privé, environnement ou DSN n’est ajouté au diagnostic.
Le reçu vert conserve son schéma sans champ supplémentaire.

Un échec de lecture ou validation de la carte (`setup`) ne permet pas de
faire confiance à un chemin de reçu : seul stderr reçoit alors la raison.
Un échec d’écriture (`publish`) se signale aussi sur stderr ; le précédent
reçu expire selon les règles habituelles. L’unité utilisateur conserve
stdout à `null` et envoie stderr au journal. Pour consulter ces raisons,
sous le compte ameesh :

```sh
journalctl --user -u ameesh-vm-health.service -n 20 --no-pager
```

Le lecteur SSH garde ses sorties supprimées et refuse les reçus rouges.
Le producteur exécuté directement reste observable par son code de sortie
et son diagnostic stderr.
