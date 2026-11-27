# Connexion du siège sur un hôte pod (#7911)

Runbook du **bureau** pour connecter un abonnement personnel (Claude, Codex)
sur un hôte pod, sur décision owner [#233251](https://github.com/manaty/ameesh/issues/233251)
option a :

- **D2 oui, sous condition** : le bureau vérifie les conditions du fournisseur
  avec chaque personne **avant** toute connexion de siège, et la preuve est
  consignée sur le lot (référence citable, puis récit dans la note).
- **D4 oui** : l'admin du cluster peut monter le PVC ; il est dans la
  frontière de confiance.

La note de conception est
[`docs/design/7633-hote-avatars-pod.md`](../design/7633-hote-avatars-pod.md)
§7.2 (Q2 bis, abonnement) et §7.3 (connexion du siège). Le chart et ses
invariants sont dans [`deploy/k8s/pod-login/`](../../deploy/k8s/pod-login/README.md).

## Avant le premier geste

- [ ] la personne est un tenant lié de l'hôte pod
      (`ameesh host tenant list --host <hôte>`) ;
- [ ] la carte Host cite `runtime.isolation_ref` et le canon accepte
      `subscription` (Q2 bis : `allow_subscription`, `require_isolation`,
      `account_isolation: per-tenant-volume`) ;
- [ ] les conditions du fournisseur ont été vérifiées **avec la personne**
      (D2) ; la preuve est notée sur le lot, et sa référence citable est
      prête pour `--terms` ;
- [ ] l'image du harnais est épinglée par digest.

## Séquence

1. **Ouvrir la fenêtre** (geste humain ou avatar, jamais un agent) :

   ```sh
   ameesh host tenant login human:<id> --host <hôte-pod> \
       --terms '<référence D2>' --ttl 1800
   ```

2. **Attendre l'acquittement du runner** : `ameesh host tenant list --host
   <hôte-pod>` doit montrer `state=login`. Le runner a arrêté ses tours, rendu
   ses baux et ne réclame plus ; la garde SQL refuse tout autre runner pendant
   la fenêtre. L'acquittement est publié par le battement qui suit la
   libération (au plus l'intervalle de ressources, 60 s par défaut) : tant que
   la ligne ne montre pas `state=login`, ne pas créer le pod.

3. **Lire le nœud** du pod partagé (`spec.nodeName`) : le PVC
   `ReadWriteOnce` doit être monté par les deux pods sur le même nœud.

4. **Créer le pod de connexion** avec le chart
   [`deploy/k8s/pod-login/render.py`](../../deploy/k8s/pod-login/render.py) :
   un conteneur, le PVC du seul tenant, UID/GID du chart, sans jeton de
   compte de service, `readOnlyRootFilesystem`, TTL ≤ 30 min, Role
   `pods/exec` borné par `resourceNames` à ce pod. Aucun `pods/exec` sur le
   pod partagé.

5. **La personne se connecte elle-même** (`kubectl exec -it <pod-login> --
   sh`, puis `claude`/`login` ou `codex login`). Le bureau ne saisit et ne lit
   aucun identifiant ; le harnais écrit `0600` dans un dossier `0700` du PVC.

6. **Fermer** : supprimer le pod, le Role et le RoleBinding, puis
   `ameesh host tenant logout human:<id> --host <hôte-pod>`. Vérifier
   `state=running` et les profils republiés.

## Repli

- **PVC non montable à côté du pod partagé** (stockage, nœud imposé, politique) :
  pod dédié, pod de travail arrêté, montages séquentiels — jamais de
  `pods/exec` dans le pod partagé (design §7.3, D3).
- **Perte du PVC ou déplacement** : re-login sur la cible, aucun fichier
  d'identifiants copié (design §9, D7).
- **Conditions du fournisseur non vérifiées pour cette personne** : pas de
  connexion ; le siège reste sur le poste, la carte peut rester en `api-key`.

## Preuve et limites

- Le contrôle **6 bis** du témoin `kind` (`deploy/k8s/pod-witness/`) vérifie
  le RBAC (aucun `pods/exec` sur le pod partagé, Role du pod de connexion
  borné, `SubjectAccessReview` autorisé sur ce seul pod et refusé sur le pod
  partagé et celui d'un pair) ; sans cluster il rend `77` (non-concluant,
  jamais vert).
- L'effet réel sur le cluster TPK, le stockage et la connexion des sièges sont
  qualifiés par **Q-TPK**, hors série (`docs/ops/qualification-hote-pod.md`).
- L'admin du cluster peut monter le PVC : limite D4 assumée par l'owner.
