# Profil serveur VM d'un mesh ameesh (lot L55)

Un mesh par organisation (décision 0033) : sa base, ses exécuteurs et ses
personas tournent sur **une VM de l'organisation**, pour que le travail ne
dépende plus du matériel des humains. Les appareils des humains (postes,
autres serveurs) rejoignent le mesh par un **réseau privé WireGuard**.
Postgres n'est jamais exposé sur Internet.

```
   poste d'un humain ──WireGuard (UDP 51820)──►  VM du mesh (Debian 12)
   10.77.0.N                                      10.77.0.1
   exécuteurs locaux (abonnements)                Postgres TLS (localhost, wg0)
   ameesh attach                                  ameesh-runner@<persona> (clés API)
                                                  sauvegarde quotidienne
```

## Fichiers

| Fichier | Rôle |
|---|---|
| `terraform/` | crée la VM, son IP et son groupe de sécurité (SSH restreint, WireGuard) chez Scaleway |
| `cloud-init.yaml` | premier démarrage : paquets, clone d'ameesh, lance l'installation |
| `installer-serveur.sh` | installation idempotente : WireGuard, Postgres TLS, utilisateur `ameesh`, ameesh, gabarits systemd, sauvegardes |
| `ameesh-runner@.service` | un exécuteur par persona, service système sous `ameesh` |
| `ameesh-sauvegarde.{service,timer}`, `sauvegarder-base.sh` | `pg_dump` quotidien, 14 jours en local, copie hors de la VM si configurée |
| `ajouter-appareil.sh` | déclare un appareil dans WireGuard et affiche sa configuration |

## Mise en place (actes de l'opérateur)

1. **Profil de l'organisation.** Un poste qui sert plusieurs organisations a
   **un profil Scaleway nommé par organisation, et aucun profil par défaut** :
   `scw` sans `-p` échoue au lieu d'agir chez la mauvaise organisation. Les
   agents passent toujours `-p <organisation>` et vérifient avant toute
   écriture : `scw -p <organisation> account project list`. Terraform exige
   le profil et l'identifiant de l'organisation, et refuse un projet qui n'en
   fait pas partie (0033 §1).
2. **Créer la VM** :

   ```bash
   cd deploy/serveur/terraform
   cat > mesh.auto.tfvars <<EOF
   scw_profile     = "<profil nommé de l'organisation>"
   organization_id = "<identifiant de l'organisation Scaleway>"
   project_id      = "<projet de l'organisation>"
   organisation    = "<id court>"
   instance_type = "PRO2-S"
   ssh_cidrs     = ["<IP d'administration>/32"]
   ameesh_ref    = "<étiquette ou commit>"
   EOF
   terraform init && terraform plan && terraform apply
   ```

   `*.tfvars` et l'état Terraform ne sont pas versionnés. Garder l'état hors
   du dépôt, dans un stockage d'objets de l'organisation par exemple.
3. **Attendre l'installation** : `ssh root@<ip> 'cloud-init status --wait && cat /root/ameesh-serveur.txt'`.
   Le journal est dans `/var/log/ameesh-installation.log`.
4. **Ajouter les appareils** : `ajouter-appareil.sh <nom>` sur la VM. Copier la
   configuration affichée dans `/etc/wireguard/ameesh.conf` sur l'appareil,
   puis `wg-quick up ameesh`. La base est joignable en
   `postgresql://ameesh@10.77.0.1:5432/ameesh?sslmode=require` ; le mot de
   passe est dans `/etc/ameesh/db.env` sur la VM et n'est jamais affiché.
5. **Canon** : déclarer l'hôte (`hotes/<organisation>-mesh-1.md`) dans le canon
   de l'organisation, renseigner `canons` dans
   `/home/ameesh/.config/ameesh/config.json`, puis
   `runuser -l ameesh -c "ameesh canon sync --fetch"`.
6. **Clés des harnais** : dans `/etc/ameesh/harnais.env` (`0640 root:ameesh`),
   uniquement des **clés API de l'organisation** (0033 §3). Les abonnements
   des humains restent sur leurs appareils.
7. **Lancer une persona** : `systemctl enable --now ameesh-runner@<persona>`.

## Personas isolées (étude v2 E1)

Par défaut (`ameesh-runner@<persona>`), toutes les personas tournent sous
l'utilisateur `ameesh` et peuvent lire leurs fichiers les unes des autres.
Pour isoler une persona :

```bash
/opt/ameesh/src/deploy/serveur/ajouter-persona.sh <persona> [--docker]
systemctl enable --now ameesh-persona@<persona>
```

* **Un utilisateur Unix `p-<persona>` par persona** : son dossier (0700)
  porte ses sessions, sa mémoire, ses dépôts de travail et l'état de son
  exécuteur, avec sa configuration ameesh, son `.pgpass` et son profil `dsh`.
* **Sandbox de l'unité** : les autres dossiers de `/home` sont invisibles
  (`ProtectHome=tmpfs`, son seul dossier remonté), le système est en lecture
  seule sauf son dossier, et `NoNewPrivileges` est actif. Les clés des
  harnais sont lues par systemd avant le passage à l'utilisateur de la
  persona.
* **Canons** : les placer dans un dossier lisible par le groupe
  `ameesh-personas` (par exemple `/srv/ameesh/canons`), pas dans `/home/ameesh`.
* **`--docker`** donne l'accès à Docker pour les tests, mais le groupe docker
  équivaut à root : l'isolation tombe pour cette persona. Docker sans
  privilèges par utilisateur reste à faire.
* **Limite** : toutes les personas partagent le rôle Postgres des exécuteurs
  (L56). Le cloisonnement des données par persona et le proxy MCP sont la
  suite du volet E.

Éprouvé dans un conteneur Debian 12 avec systemd. Deux personas `alpha` et
`beta` tournent chacune sous son utilisateur, ne voient que leur dossier,
et ne lisent pas les fichiers l'une de l'autre.

## Retour arrière

- **Une persona** : `agent-runner stop <persona>`, puis `systemctl disable --now ameesh-runner@<persona>`.
  Elle peut ensuite reprendre sur un poste avec la même procédure, à condition
  que sa session y soit présente (sauvegarde des sessions : lot L53).
- **La VM** : `pg_dump` final (`systemctl start ameesh-sauvegarde`), copie
  hors de la VM, puis `terraform destroy`.

## Limites connues de ce lot

- Un seul rôle Postgres `ameesh` pour tout le mesh : les rôles par hôte et par
  agent sont le lot L56.
- Les personas partagent l'utilisateur Unix `ameesh` : l'isolation par
  utilisateur ou par pod est le volet E de l'étude v2.
- Les harnais (Node, `dsh`, `codex`, `claude`) ne sont pas installés par ce
  lot : chaque organisation installe ceux de ses personas.
- Éprouvé dans un conteneur Debian 12 avec systemd (installation rejouable, TLS imposé sur wg0, sauvegarde, ajout d'appareil, `ameesh doctor`) ; pas encore sur une VM réelle.
