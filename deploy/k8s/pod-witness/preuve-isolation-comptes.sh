#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# #7694/#7695/#7698/#7699/#7911 (design 7633 §5.3, §6, §7.3, §9.3) — témoin
# kind d'un hôte pod : identité (UID du chart = UID du processus = entrée
# passwd), montage (le PVC de B n'est pas dans le conteneur de A), permissions
# (le dossier de comptes 0700 de B refuse l'UID de A, même monté en lecture
# seule), superviseur (uid 10001, aucun volume de tenant, /tmp propre), base
# (réclamation par tenant, attach, isolation UID, bail superviseur et préflight
# de déplacement pod sur un PostgreSQL migré — 0601 comprise). En plus des cinq
# contrôles de #7695, le canon (#7694, B1 de #44265) est rejoué : clone sous
# 10001, lecture sous l'UID du tenant avec la paire `safe.directory` du plan.
# #7698 ajoute le déplacement `pod partagé -> pod dédié` : le MÊME PVC est
# ré-attaché à un pod recréé (UID Kubernetes neuf, UID de personne inchangé,
# témoin intact), puis le retour au pod partagé. Un UID différent ne lit pas le
# dossier de comptes (témoin 3) : c'est le repli « compte re-login » quand le
# PVC ne peut pas être ré-attaché, jamais une copie. #7699 ajoute le contrôle
# du chart de référence : objets P6 acceptés par l'API, quota qui mord, PDB
# 1 pod / 0 interruption, image par digest, preStop et labels. #7911 ajoute le
# contrôle 6 bis : le pod de connexion dédié, son Role `pods/exec` borné à ce
# seul pod par `resourceNames`, un `SubjectAccessReview` autorisé dessus et
# refusé sur le pod partagé et sur celui d'un pair (jamais `pods/exec` sur le
# pod partagé, design §7.3).
#
# Ce script est un geste de BUREAU/OPÉRATEUR, pas de l'agent intéressé : il
# crée un cluster kind jetable, applique les gabarits du dossier, exécute les
# contrôles depuis les conteneurs, imprime une trace (version Kubernetes,
# image, UID du pod, UID/GID, modes) puis supprime le cluster. Aucun secret
# n'est lu ni affiché.
#
#   ./preuve-isolation-comptes.sh                 # kind + contrôles + nettoyage
#   ./preuve-isolation-comptes.sh --render-only   # rendu des manifestes, sans cluster
#   ./preuve-isolation-comptes.sh --keep          # garde le cluster pour inspection
#
# Contrôle 5 (base) : AMEESH_TEST_DSN=postgresql://… (schéma migré). Sans DSN,
# il est sauté et le script sort 77 (non-concluant). Sortie 77 aussi si
# kind/kubectl/docker manque : jamais « vert ». Variables/options : --uid-a,
# --uid-b, --fs-group, --image, --git-image, --cluster, --namespace.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$HERE/../../.." && pwd)
CLUSTER=${CLUSTER:-ameesh-pod-witness}
NS=${NS:-ameesh-pod-witness}
#: Namespace jetable du contrôle du chart de référence (#7699).
NS_REF=${NS_REF:-${NS}-reference}
UID_A=${UID_A:-2519}
UID_B=${UID_B:-2520}
FS_GROUP=${FS_GROUP:-10000}
SUPERVISOR_UID=${SUPERVISOR_UID:-10001}
IMAGE=${IMAGE:-busybox:1.36}
#: image avec Git pour le témoin du canon (le dépôt est cloné sous
#: SUPERVISOR_UID puis lu sous UID_A).
GIT_IMAGE=${GIT_IMAGE:-alpine/git:2.45.2}
KEEP=0
RENDER_ONLY=0

usage() {
    sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --render-only) RENDER_ONLY=1 ;;
        --keep) KEEP=1 ;;
        --cluster) CLUSTER=${2:?}; shift ;;
        --namespace) NS=${2:?}; shift ;;
        --uid-a) UID_A=${2:?}; shift ;;
        --uid-b) UID_B=${2:?}; shift ;;
        --fs-group) FS_GROUP=${2:?}; shift ;;
        --image) IMAGE=${2:?}; shift ;;
        --git-image) GIT_IMAGE=${2:?}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "option inconnue: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

for valeur in UID_A UID_B FS_GROUP SUPERVISOR_UID; do
    eval "v=\${$valeur}"
    case "$v" in
        ''|*[!0-9]*) echo "$valeur doit être un entier: $v" >&2; exit 2 ;;
    esac
done
[ "$UID_A" != "$UID_B" ] || { echo "UID_A et UID_B doivent différer" >&2; exit 2; }
[ "$FS_GROUP" -gt 0 ] || { echo "FS_GROUP doit être > 0" >&2; exit 2; }
for uid in "$UID_A" "$UID_B"; do
    if [ "$uid" -eq 0 ] || [ "$uid" -eq "$FS_GROUP" ] || [ "$uid" -eq "$SUPERVISOR_UID" ]; then
        echo "UID de tenant refusé : $uid (0, fsGroup et superviseur $SUPERVISOR_UID sont réservés)" >&2
        exit 2
    fi
done

render() {
    sed -e "s#__UID_A__#${UID_A}#g" \
        -e "s#__UID_B__#${UID_B}#g" \
        -e "s#__FS_GROUP__#${FS_GROUP}#g" \
        -e "s#__SUPERVISOR_UID__#${SUPERVISOR_UID}#g" \
        -e "s#__IMAGE__#${IMAGE}#g" \
        -e "s#__GIT_IMAGE__#${GIT_IMAGE}#g" \
        "$HERE/$1"
}

if [ "$RENDER_ONLY" -eq 1 ]; then
    echo "# #7694/#7695/#7698/#7911 témoin kind — UID_A=$UID_A UID_B=$UID_B fsGroup=$FS_GROUP image=$IMAGE git_image=$GIT_IMAGE"
    echo "# kubectl create namespace $NS && kubectl -n $NS apply -f -"
    echo "---"
    render temoin-pvc.yaml
    echo "---"
    render temoin-deux.yaml
    echo "---"
    render temoin-dedie.yaml
    echo "---"
    render temoin-lecture.yaml
    echo "---"
    render temoin-canon.yaml
    echo "---"
    if python3 -c 'import yaml' 2>/dev/null; then
        python3 "$REPO_ROOT/deploy/k8s/pod-login/render.py" \
            --namespace "$NS" --host pod-alice --tenant human:alice \
            --uid "$UID_A" --pvc pvc-temoin-a --pod ameesh-login-alice \
            --node node-temoin \
            --image "ameesh/temoin@sha256:$(printf '0%.0s' {1..64})" \
            --subject User:temoin-alice
    else
        echo "# login: PyYAML absent, rendu non produit"
    fi
    exit 0
fi

for outil in kind kubectl docker; do
    command -v "$outil" >/dev/null 2>&1 \
        || { echo "SKIP: $outil absent — témoin kind non exécuté (non-concluant)" >&2; exit 77; }
done

k() { kubectl -n "$NS" "$@"; }
#: pod courant des contrôles d'identité (#7698 : partagé puis dédié).
POD=temoin-deux
kexec() { local conteneur=$1; shift; k exec "$POD" -c "$conteneur" -- "$@"; }
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

pod_uid() {
    k get pod "$1" -o jsonpath='{.metadata.uid}' 2>/dev/null || true
}

#: volume réellement lié au PVC : preuve que le volume est ré-attaché, pas recréé.
pvc_volume() {
    k get pvc "$1" -o jsonpath='{.spec.volumeName}' 2>/dev/null || true
}

diagnostic() {
    echo "--- diagnostic (namespace $NS) ---" >&2
    k get pods -o wide >&2 || true
    k get pvc >&2 || true
    k get events --sort-by=.lastTimestamp >&2 | tail -20 || true
    for conteneur in a b superviseur; do
        k logs temoin-deux -c "$conteneur" >&2 || true
    done
    k logs temoin-dedie >&2 || true
    k logs temoin-lecture >&2 || true
    k logs temoin-canon >&2 || true
}

cleanup() {
    local code=$?
    trap - EXIT
    if [ "$code" -ne 0 ]; then
        diagnostic || true
    fi
    if [ "$KEEP" -eq 0 ]; then
        kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    else
        echo "cluster conservé : kubectl --context kind-$CLUSTER -n $NS get pods" >&2
    fi
    exit "$code"
}
trap cleanup EXIT

apply_manifest() {
    render "$1" | k apply -f - >/dev/null
}

wait_ready() {
    k wait --for=condition=Ready "pod/${1:-temoin-deux}" --timeout=180s >/dev/null
}

# kind lie ses PVC par local-path, un volume hostPath : le kubelet n'applique
# pas fsGroup aux hostPath, alors qu'un pilote CSI le fait. Le plan repose sur
# UN fsGroup neutre (10000, jamais un par tenant) : on applique donc à la
# racine du PVC le pas que ferait le kubelet (chgrp + setgid 2770), avec
# trace. Sans lui, la racine resterait root:root 0777 en kind et le contrôle 1
# jugerait le pilote, pas le chart.
apply_fsgroup() {
    local conteneur=$1 pvc=$2 home gid pv chemin noeud
    home="/home/ameesh-$conteneur"
    gid=$(kexec "$conteneur" stat -c '%g' "$home")
    if [ "$gid" = "$FS_GROUP" ]; then
        echo "trace: fsgroup=$conteneur deja applique par le pilote"
        return 0
    fi
    pv=$(k get pvc "$pvc" -o jsonpath='{.spec.volumeName}')
    [ -n "$pv" ] || fail "fsgroup $conteneur: PVC $pvc non lie"
    chemin=$(kubectl get pv "$pv" -o jsonpath='{.spec.hostPath.path}')
    [ -n "$chemin" ] \
        || fail "fsgroup $conteneur: PV $pv sans hostPath (pilote inattendu)"
    noeud=$(k get pod temoin-deux -o jsonpath='{.spec.nodeName}')
    [ -n "$noeud" ] || fail "fsgroup $conteneur: noeud du pod illisible"
    docker exec "$noeud" chgrp "$FS_GROUP" "$chemin" \
        || fail "fsgroup $conteneur: chgrp impossible sur $chemin"
    docker exec "$noeud" chmod 2770 "$chemin" \
        || fail "fsgroup $conteneur: chmod impossible sur $chemin"
    gid=$(kexec "$conteneur" stat -c '%g' "$home")
    [ "$gid" = "$FS_GROUP" ] \
        || fail "fsgroup $conteneur: groupe $gid apres application, attendu $FS_GROUP"
    echo "trace: fsgroup=$conteneur applique a la racine du PVC (kind local-path, comme un pilote CSI)"
}

# --- trace : ce que la preuve attachée au lot doit citer ---------------------
trace() {
    echo "trace: date=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "trace: kubernetes=$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}' 2>/dev/null || echo '?')"
    echo "trace: image=$IMAGE"
    echo "trace: git_image=$GIT_IMAGE"
    echo "trace: image_id=$(docker image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || echo '?')"
    echo "trace: uid_a=$UID_A uid_b=$UID_B fs_group=$FS_GROUP"
    echo "trace: pod=$POD pod_uid=$(pod_uid "$POD")"
    echo "trace: image_ids=$(k get pod "$POD" -o jsonpath='{.status.containerStatuses[*].imageID}' 2>/dev/null || echo '?')"
}

# --- contrôle 1 : identité et modes -----------------------------------------
check_identity() {
    local conteneur=$1 attendu=$2 home="/home/ameesh-$1"
    local uid passwd home_stat acct_stat marker_stat
    uid=$(kexec "$conteneur" id -u | tr -d '\r')
    [ "$uid" = "$attendu" ] \
        || fail "identite $conteneur: id -u=$uid, attendu $attendu"
    passwd=$(kexec "$conteneur" sh -ec "grep '^ameesh-$conteneur:' /etc/passwd | cut -d: -f3,4")
    [ "$passwd" = "$attendu:$FS_GROUP" ] \
        || fail "passwd $conteneur: uid:gid=$passwd, attendu $attendu:$FS_GROUP"
    { read -r home_stat; read -r acct_stat; read -r marker_stat; } < <(
        kexec "$conteneur" stat -c '%u:%g %a %n' \
            "$home" "$home/.config/ameesh/accounts" \
            "$home/.config/ameesh/accounts/.temoin-$conteneur")
    local home_group home_mode acct_owner acct_group acct_mode
    local marker_owner marker_group marker_mode
    home_group=$(echo "$home_stat" | awk '{print $1}' | cut -d: -f2)
    home_mode=$(echo "$home_stat" | awk '{print $2}')
    acct_owner=$(echo "$acct_stat" | awk '{print $1}' | cut -d: -f1)
    acct_group=$(echo "$acct_stat" | awk '{print $1}' | cut -d: -f2)
    acct_mode=$(echo "$acct_stat" | awk '{print $2}')
    marker_owner=$(echo "$marker_stat" | awk '{print $1}' | cut -d: -f1)
    marker_group=$(echo "$marker_stat" | awk '{print $1}' | cut -d: -f2)
    marker_mode=$(echo "$marker_stat" | awk '{print $2}')
    # La racine du PVC n'est pas la frontière : elle peut être root:fsGroup
    # 0770/0775. Ce qui compte est le fsGroup appliqué et les modes du compte.
    [ "$home_group" = "$FS_GROUP" ] \
        || fail "home $conteneur: groupe $home_group, attendu fsGroup $FS_GROUP"
    # fsGroup peut poser le bit setgid (2770) : on juge les permissions
    # effectives (3 derniers chiffres), pas le bit d'héritage.
    home_perms=${home_mode: -3}
    case "$home_perms" in
        770|775) ;;
        *) fail "home $conteneur: mode $home_mode, attendu 770/775 (racine du PVC)" ;;
    esac
    [ "$acct_owner" = "$attendu" ] \
        || fail "comptes $conteneur: proprietaire $acct_owner, attendu $attendu"
    [ "$acct_group" = "$FS_GROUP" ] \
        || fail "comptes $conteneur: groupe $acct_group, attendu $FS_GROUP"
    acct_perms=${acct_mode: -3}
    [ "$acct_perms" = "700" ] \
        || fail "comptes $conteneur: mode $acct_mode, attendu 700 (setgid toléré)"
    [ "$marker_owner" = "$attendu" ] \
        || fail "temoin $conteneur: proprietaire $marker_owner, attendu $attendu"
    [ "$marker_group" = "$FS_GROUP" ] \
        || fail "temoin $conteneur: groupe $marker_group, attendu $FS_GROUP"
    marker_perms=${marker_mode: -3}
    [ "$marker_perms" = "600" ] \
        || fail "temoin $conteneur: mode $marker_mode, attendu 600"
    pass "identite $conteneur: id -u=$uid passwd=$passwd home=[$home_stat] comptes=[$acct_stat] temoin=[$marker_stat]"
}

# --- contrôle 2 : isolation des montages ------------------------------------
check_mount() {
    if kexec a sh -ec 'test -e /home/ameesh-b' 2>/dev/null; then
        fail "montage: /home/ameesh-b existe dans le conteneur de A"
    fi
    if kexec a sh -ec 'grep -Fq " /home/ameesh-b " /proc/self/mountinfo' 2>/dev/null; then
        fail "montage: le PVC de B apparait dans /proc/self/mountinfo de A"
    fi
    kexec a sh -ec 'grep -Fq " /home/ameesh-a " /proc/self/mountinfo' 2>/dev/null \
        || fail "montage: controle positif — /home/ameesh-a absent de mountinfo"
    kexec a sh -ec 'test "$(cat "$HOME/.config/ameesh/accounts/.temoin-a")" = temoin-a' \
        || fail "montage: A ne lit pas son propre temoin (controle positif)"
    if kexec a sh -ec 'test -e /var/run/secrets/kubernetes.io/serviceaccount/token' 2>/dev/null; then
        fail "montage: jeton de compte de service monte dans A"
    fi
    pass "montage: A ne voit ni /home/ameesh-b, ni le PVC de B, ni un jeton ; il lit son temoin"
}

# --- contrôle 4 : le superviseur n'a aucun volume de tenant -----------------
# #7695 : dans le chart pod-shared, le superviseur est un conteneur à part,
# sous SON UID, avec son home et son /tmp à lui ; il ne monte ni le home ni le
# PVC d'un tenant et ne reçoit pas de jeton de compte de service. Le contrôle
# positif (écrire et relire son /tmp) prouve que l'absence de volume tenant
# n'est pas un conteneur mort.
check_supervisor() {
    local uid passwd stat
    uid=$(kexec superviseur id -u | tr -d '\r')
    [ "$uid" = "$SUPERVISOR_UID" ] \
        || fail "superviseur: id -u=$uid, attendu $SUPERVISOR_UID"
    passwd=$(kexec superviseur sh -ec "grep '^ameesh:' /etc/passwd | cut -d: -f3,4")
    [ "$passwd" = "$SUPERVISOR_UID:$FS_GROUP" ] \
        || fail "superviseur: passwd uid:gid=$passwd, attendu $SUPERVISOR_UID:$FS_GROUP"
    kexec superviseur sh -ec 'test -w /tmp && echo ok > /tmp/.temoin-superviseur \
        && test "$(cat /tmp/.temoin-superviseur)" = ok' \
        || fail "superviseur: son /tmp n'est pas inscriptible (contrôle positif)"
    stat=$(kexec superviseur stat -c '%u:%g %a' /tmp/.temoin-superviseur)
    [ "$stat" = "$SUPERVISOR_UID:$FS_GROUP 600" ] \
        || fail "superviseur: temoin /tmp = [$stat], attendu $SUPERVISOR_UID:$FS_GROUP 600"
    for chemin in /home/ameesh-a /home/ameesh-b; do
        if kexec superviseur sh -ec "test -e '$chemin'" 2>/dev/null; then
            fail "superviseur: $chemin est visible dans son conteneur"
        fi
    done
    if kexec superviseur sh -ec 'grep -Fq " /home/ameesh-a " /proc/self/mountinfo \
        || grep -Fq " /home/ameesh-b " /proc/self/mountinfo' 2>/dev/null; then
        fail "superviseur: un home de tenant figure dans /proc/self/mountinfo"
    fi
    if kexec superviseur sh -ec 'test -e /var/run/secrets/kubernetes.io/serviceaccount/token' 2>/dev/null; then
        fail "superviseur: jeton de compte de service monte"
    fi
    pass "superviseur: uid=$uid, /tmp prive inscriptible, aucun home/PVC de tenant, aucun jeton"
}


# --- contrôle 3 : permissions (pod séparé, sans fsGroup) --------------------
check_lecture() {
    apply_manifest temoin-lecture.yaml
    if ! k wait --for=jsonpath='{.status.phase}'=Succeeded pod/temoin-lecture --timeout=120s >/dev/null; then
        k logs temoin-lecture >&2 || true
        fail "lecture: le pod temoin-lecture n'a pas réussi"
    fi
    local logs code
    logs=$(k logs temoin-lecture)
    printf '%s\n' "$logs"
    grep -q '^PASS lecture-a:' <<<"$logs" \
        || fail "lecture: contrôle positif A manquant"
    grep -q '^PASS lecture-b:' <<<"$logs" \
        || fail "lecture: B n'est pas refusé à l'UID de A"
    grep -q '^PASS listage-b' <<<"$logs" \
        || fail "lecture: le dossier de comptes de B est listable par A"
    if grep -q '^FAIL' <<<"$logs"; then
        fail "lecture: au moins un contrôle du pod temoin-lecture a échoué"
    fi
    code=$(k get pod temoin-lecture -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}')
    [ "$code" = "0" ] || fail "lecture: code de sortie $code"
    pass "lecture: A lit son compte, B (0700, monté sans fsGroup) le refuse"
}

# --- contrôle canon (#7694) : clone et lecture sous des UID différents ------
# Le dépôt est créé par l'init sous SUPERVISOR_UID ; la lecture par l'UID du
# tenant passe seulement grâce à la paire `safe.directory` du plan, bornée à
# ce dépôt. Sans elle, Git refuse « dubious ownership » (contrôle négatif).
check_canon() {
    apply_manifest temoin-canon.yaml
    if ! k wait --for=jsonpath='{.status.phase}'=Succeeded pod/temoin-canon --timeout=180s >/dev/null; then
        k logs temoin-canon >&2 || true
        fail "canon: le pod temoin-canon n'a pas réussi"
    fi
    local logs code
    logs=$(k logs temoin-canon)
    printf '%s\n' "$logs"
    grep -q "^PASS canon-lecture: uid=$UID_A clone_uid=$SUPERVISOR_UID$" <<<"$logs" \
        || fail "canon: lecture du dépôt sous l'UID du tenant (clone sous $SUPERVISOR_UID) absente"
    grep -q '^PASS canon-refus: sans safe.directory' <<<"$logs" \
        || fail "canon: le refus sans safe.directory n'est pas prouvé"
    if grep -q '^FAIL' <<<"$logs"; then
        fail "canon: au moins un contrôle du pod temoin-canon a échoué"
    fi
    code=$(k get pod temoin-canon -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}')
    [ "$code" = "0" ] || fail "canon: code de sortie $code"
    pass "canon: dépôt clone sous $SUPERVISOR_UID lu sous $UID_A, confiance bornée au dépôt"
}

# --- contrôle 6 (#7699) : chart de référence, objets acceptés et bornés -----
# Ce contrôle ne rejoue pas les conteneurs : il prouve que le chart de
# référence APPLIQUÉ est accepté par l'API et que ses gardes mordent (quota,
# PDB), que l'image est épinglée, que le préStop et les labels sont présents.
# L'application de la NetworkPolicy dépend du CNI : kind ne la prouve pas, la
# qualification réelle est Q-TPK (runbook docs/ops/qualification-hote-pod.md).
reference_pod() {
    # Un pod conforme à PSA restricted, dans la quota du chart (1 pod).
    cat <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $1
  labels:
    app.kubernetes.io/name: ameesh
    app.kubernetes.io/instance: ameesh-pod-alice
spec:
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    runAsUser: 2519
    runAsGroup: 10000
    fsGroup: 10000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: temoin
      image: $IMAGE
      command: ["sleep", "300"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
        readOnlyRootFilesystem: true
      resources:
        requests:
          cpu: 10m
          memory: 16Mi
        limits:
          cpu: 50m
          memory: 64Mi
EOF
}

check_reference() {
    local render="python3 $REPO_ROOT/deploy/k8s/pod-shared/render.py"
    local sts types out expected allowed manifest
    python3 -c 'import yaml' 2>/dev/null \
        || { echo "SKIP reference: PyYAML absent — contrôle non exécuté (non-concluant)" >&2; return 77; }
    $render --check >/dev/null \
        || fail "reference: le rendu du chart est périmé (./render.py --write)"
    sts=$($render --stdout --kind StatefulSet) \
        || fail "reference: rendu du StatefulSet impossible"
    grep -Eq '@sha256:[0-9a-f]{64}' <<<"$sts" \
        || fail "reference: image non épinglée par digest"
    grep -q 'preStop' <<<"$sts" \
        || fail "reference: preStop de drain absent"
    grep -q 'terminationGracePeriodSeconds: 120' <<<"$sts" \
        || fail "reference: grâce absente ou inférieure à 120 s"
    grep -q 'app.kubernetes.io/version' <<<"$sts" \
        || fail "reference: label de version absent"
    manifest=$($render --stdout --kind NetworkPolicy --kind ResourceQuota \
        --kind PodDisruptionBudget) \
        || fail "reference: rendu des objets P6 impossible"
    grep -q 'kind: NetworkPolicy' <<<"$manifest" \
        || fail "reference: NetworkPolicy absente"
    grep -q 'kind: ResourceQuota' <<<"$manifest" \
        || fail "reference: ResourceQuota absente"
    grep -q 'kind: PodDisruptionBudget' <<<"$manifest" \
        || fail "reference: PodDisruptionBudget absente"
    kubectl kustomize "$REPO_ROOT/deploy/k8s/pod-shared" >/dev/null \
        || fail "reference: kubectl kustomize en échec"

    kubectl create namespace "$NS_REF" --dry-run=client -o yaml \
        | kubectl apply -f - >/dev/null
    kubectl label namespace "$NS_REF" \
        pod-security.kubernetes.io/enforce=restricted \
        pod-security.kubernetes.io/enforce-version=latest --overwrite >/dev/null
    printf '%s\n' "$manifest" \
        | kubectl -n "$NS_REF" apply -f - >/dev/null \
        || fail "reference: les objets P6 sont refusés par l'API"
    types=$(kubectl -n "$NS_REF" get networkpolicy \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' \
        | grep -- '-default-deny$' | head -1)
    [ -n "$types" ] || fail "reference: aucune NetworkPolicy default-deny"
    types=$(kubectl -n "$NS_REF" get networkpolicy "$types" \
        -o jsonpath='{.spec.policyTypes[*]}')
    [ "$types" = "Ingress Egress" ] \
        || fail "reference: default-deny ne couvre pas Ingress et Egress ($types)"
    [ "$(kubectl -n "$NS_REF" get networkpolicy -o name | wc -l)" -ge 5 ] \
        || fail "reference: règles de sortie manquantes"

    reference_pod temoin-reference | kubectl -n "$NS_REF" apply -f - >/dev/null
    kubectl -n "$NS_REF" wait --for=condition=Ready pod/temoin-reference \
        --timeout=120s >/dev/null || fail "reference: pod témoin non prêt"
    expected=0
    for _ in $(seq 1 30); do
        expected=$(kubectl -n "$NS_REF" get pdb \
            -o jsonpath='{.items[0].status.expectedPods}' 2>/dev/null || true)
        [ "$expected" = "1" ] && break
        sleep 1
    done
    [ "$expected" = "1" ] \
        || fail "reference: PDB expectedPods=$expected, attendu 1"
    allowed=$(kubectl -n "$NS_REF" get pdb \
        -o jsonpath='{.items[0].status.disruptionsAllowed}')
    [ "$allowed" = "0" ] \
        || fail "reference: PDB disruptionsAllowed=$allowed, attendu 0"
    out=$(mktemp "${TMPDIR:-/tmp}/ameesh-reference-XXXXXX")
    if reference_pod temoin-reference-2 | kubectl -n "$NS_REF" apply -f - >"$out" 2>&1; then
        rm -f "$out"
        fail "reference: un second pod n'est pas refusé par la quota"
    fi
    grep -q 'exceeded quota' "$out" \
        || { cat "$out" >&2; rm -f "$out"; fail "reference: refus de quota inattendu"; }
    rm -f "$out"
    pass "reference: 5 NetworkPolicy (default-deny + DNS + sorties), quota mord (2e pod refusé), PDB 1 pod / 0 interruption, image@digest, preStop, label de version"
    echo "trace: reference: NetworkPolicy acceptée ; son application par le CNI kind n'est pas prouvée, Q-TPK la qualifie"
}

# --- contrôle 5 : base réelle — réclamation et isolation d'un hôte pod ------
# #7695 : les contrôles 1–4 prouvent les conteneurs ; celui-ci prouve la base
# qui les gouverne, avec un vrai PostgreSQL migré (0599 comprise) :
# `ameesh_tenant_claim_ok` refuse le runner d'un autre tenant, une session
# `attach` ne s'ouvre qu'à l'utilisateur système du tenant, l'isolation par UID
# refuse un pair du même tenant, et un seul superviseur prend le bail.
check_base() {
    if [ -z "${AMEESH_TEST_DSN:-}" ]; then
        echo "SKIP base: AMEESH_TEST_DSN absent — contrôle base non exécuté (non-concluant)" >&2
        return 77
    fi
    command -v python3 >/dev/null 2>&1 \
        || fail "base: python3 absent alors qu'AMEESH_TEST_DSN est posé"
    echo "== base : $AMEESH_TEST_DSN =="
    ( cd "$REPO_ROOT" && PYTHONPATH=src python3 -m unittest -v \
        tests.test_7695_pod_partage.WitnessBaseTest \
        tests.test_7698_deplacement_pod.WitnessMoveBaseTest ) \
        || fail "base: le contrôle base a échoué (voir la sortie unittest)"
    pass "base: réclamation par tenant, attach par utilisateur système, isolation UID, bail superviseur, présence pod et préflight de déplacement (#7698)"
}

# --- contrôle 6 bis (#7911) : connexion du siège, exec borné -----------------
# Le pod partagé ne reçoit JAMAIS `pods/exec` : `kubectl exec` choisit le
# conteneur dans la requête, hors de l'objet RBAC. Ce contrôle rend le pod de
# connexion dédié (#7911, design 7633 §7.3), l'applique, puis interroge l'API
# d'autorisation : le sujet du RoleBinding peut exec dans SON pod de connexion,
# jamais dans le pod partagé ni dans celui d'un pair. Aucun conteneur n'y
# tourne : le pod reste Pending et l'image n'est pas tirée, seule
# l'autorisation est prouvée.
check_login() {
    local pod=ameesh-login-alice peer=ameesh-login-bob partage=ameesh-pod-alice
    local user=temoin-alice node image manifest allowed denied
    python3 -c 'import yaml' 2>/dev/null \
        || { echo "SKIP login: PyYAML absent — contrôle non exécuté (non-concluant)" >&2; return 77; }
    command -v python3 >/dev/null 2>&1 \
        || { echo "SKIP login: python3 absent — contrôle non exécuté (non-concluant)" >&2; return 77; }
    node=$(kubectl -n "$NS" get pod temoin-deux \
        -o jsonpath='{.spec.nodeName}' 2>/dev/null || true)
    [ -n "$node" ] || fail "login: nœud du pod partagé illisible"
    # Image épinglée par digest (le rendu l'exige), volontairement absente :
    # ce contrôle porte sur le RBAC, pas sur l'exécution d'un harnais.
    image="ameesh/temoin@sha256:$(printf '0%.0s' {1..64})"
    manifest=$(python3 "$REPO_ROOT/deploy/k8s/pod-login/render.py" \
        --namespace "$NS" --host pod-alice --tenant human:alice \
        --uid "$UID_A" --pvc pvc-temoin-a --pod "$pod" --node "$node" \
        --image "$image" --subject "User:$user") \
        || fail "login: le rendu du pod de connexion a échoué"
    # Un nom de pod partagé est refusé à la source : pas de pods/exec dessus.
    if python3 "$REPO_ROOT/deploy/k8s/pod-login/render.py" \
        --namespace "$NS" --host pod-alice --tenant human:alice \
        --uid "$UID_A" --pvc pvc-temoin-a --pod "$partage" --node "$node" \
        --image "$image" >/dev/null 2>&1; then
        fail "login: le rendu accepte le pod partagé comme cible exec"
    fi
    grep -q 'resourceNames:' <<<"$manifest" \
        || fail "login: Role sans resourceNames (exec non borné)"
    if grep -Eq 'name: (admin|edit|cluster-admin)' <<<"$manifest"; then
        fail "login: le rendu lie un rôle large"
    fi
    printf '%s\n' "$manifest" | kubectl -n "$NS" apply -f - >/dev/null \
        || fail "login: les objets du pod de connexion sont refusés par l'API"
    echo "trace: login: pod=$pod (Pending, image non tirée) role=$pod sujet=User:$user"
    _sar_login() {
        kubectl create -f - -o jsonpath='{.status.allowed}' <<EOF
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
spec:
  user: $1
  resourceAttributes:
    namespace: $NS
    verb: create
    group: ""
    resource: pods
    subresource: exec
    name: $2
EOF
    }
    allowed=$(_sar_login "$user" "$pod") \
        || fail "login: SubjectAccessReview impossible sur le pod de connexion"
    [ "$allowed" = "true" ] \
        || fail "login: exec refusé sur son propre pod de connexion ($allowed)"
    for denied in "$partage" "$peer"; do
        allowed=$(_sar_login "$user" "$denied") \
            || fail "login: SubjectAccessReview impossible sur $denied"
        [ "$allowed" = "false" ] \
            || fail "login: exec autorisé sur $denied (attendu: refusé)"
    done
    [ "$(kubectl -n "$NS" get role "$pod" \
        -o jsonpath='{.rules[0].resourceNames[0]}')" = "$pod" ] \
        || fail "login: le Role ne nomme pas uniquement le pod de connexion"
    [ "$(kubectl -n "$NS" get rolebinding "$pod" \
        -o jsonpath='{.subjects[0].kind}:{.subjects[0].name}')" = "User:$user" ] \
        || fail "login: le RoleBinding ne lie pas l'utilisateur attendu"
    if kubectl -n "$NS" get rolebinding "$pod" -o yaml \
        | grep -q 'kind: ServiceAccount'; then
        fail "login: le RoleBinding lie un compte de service"
    fi
    # Le pod d'abord, puis ses droits : jamais l'inverse (design §7.3, étape 6).
    kubectl -n "$NS" delete pod "$pod" --wait=true --timeout=120s >/dev/null
    kubectl -n "$NS" delete role "$pod" rolebinding "$pod" serviceaccount ameesh-login \
        --ignore-not-found --wait=true >/dev/null
    pass "login: exec autorisé sur le seul pod de connexion ($pod) pour User:$user, refusé sur le pod partagé ($partage) et sur celui d'un pair ($peer), Role borné, aucun Role large ni compte de service"
}


docker pull --quiet "$IMAGE" >/dev/null
docker pull --quiet "$GIT_IMAGE" >/dev/null
kind create cluster --name "$CLUSTER" --wait 120s >/dev/null
# kind v0.27 importe `docker save` avec `ctr images import --all-platforms` ;
# avec le magasin d'images containerd de Docker récent, l'archive d'une image
# multi-arch ne porte qu'une plateforme et l'import échoue sur un digest
# absent. `docker save --platform linux/amd64` + `kind load image-archive`
# borne l'archive à la plateforme du nœud ; le repli `docker save` classique
# couvre les Docker qui ne connaissent pas `--platform`.
load_image() {
    local image=$1 archive
    archive=$(mktemp "${TMPDIR:-/tmp}/ameesh-kind-image-XXXXXX.tar")
    if ! docker save --platform linux/amd64 -o "$archive" "$image" 2>/dev/null; then
        docker save -o "$archive" "$image"
    fi
    kind load image-archive "$archive" --name "$CLUSTER" >/dev/null
    rm -f "$archive"
}
load_image "$IMAGE"
load_image "$GIT_IMAGE"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# PSA restricted : si le cluster l'applique, le gabarit doit y être admis.
kubectl label namespace "$NS" \
    pod-security.kubernetes.io/enforce=restricted \
    pod-security.kubernetes.io/enforce-version=latest >/dev/null

# B2 de #44265 : kind installe une StorageClass `WaitForFirstConsumer` ; un PVC
# ne passe `Bound` qu'une fois un pod consommateur planifié. On crée donc le
# consommateur AVANT d'attendre le binding — l'ordre inverse expirait à 120 s.
apply_manifest temoin-pvc.yaml
apply_manifest temoin-deux.yaml
k wait --for=jsonpath='{.status.phase}'=Bound \
    pvc/pvc-temoin-a pvc/pvc-temoin-b --timeout=120s >/dev/null
pass "PVC: pvc-temoin-a et pvc-temoin-b liés (consommateur temoin-deux créé d'abord)"
wait_ready
pass "pod: temoin-deux prêt (A et B)"
apply_fsgroup a pvc-temoin-a
apply_fsgroup b pvc-temoin-b
trace

check_identity a "$UID_A"
check_identity b "$UID_B"
check_mount
check_supervisor

# --- #7698 : pod partagé -> pod dédié, même PVC, même UID de personne ------
# Le pod partagé est supprimé (PVC ReadWriteOnce) : le pod dédié de A monte le
# MÊME PVC `pvc-temoin-a`, sous le MÊME UID 2519. Seul le UID Kubernetes du
# pod change : le volume n'est pas copié, le témoin de A doit survivre et le
# volume de B ne doit pas apparaître.
premier_uid=$(pod_uid temoin-deux)
premier_volume=$(pvc_volume pvc-temoin-a)
kubectl -n "$NS" delete pod temoin-deux --wait=true --timeout=120s >/dev/null
apply_manifest temoin-dedie.yaml
wait_ready temoin-dedie
dedie_uid=$(pod_uid temoin-dedie)
dedie_volume=$(pvc_volume pvc-temoin-a)
[ -n "$premier_uid" ] && [ -n "$dedie_uid" ] || fail "deplacement: UID de pod illisible"
[ "$premier_uid" != "$dedie_uid" ] \
    || fail "deplacement: le pod dedie porte le même UID ($premier_uid)"
[ -n "$premier_volume" ] && [ "$premier_volume" = "$dedie_volume" ] \
    || fail "deplacement: le PVC a changé de volume ($premier_volume -> $dedie_volume)"
POD=temoin-dedie
trace
check_identity a "$UID_A"
kexec a sh -ec 'test "$(cat "$HOME/.config/ameesh/accounts/.temoin-a")" = temoin-a' \
    || fail "deplacement: le temoin de A n'a pas survecu au re-attachement du PVC"
if kexec a sh -ec 'test -e /home/ameesh-b' 2>/dev/null; then
    fail "deplacement: le volume de B est visible dans le pod dedie"
fi
POD=temoin-deux
pass "deplacement pod partage -> dedie : volume $premier_volume re-attache (pod $premier_uid -> $dedie_uid), UID $UID_A et temoin intacts, volume de B absent"

# --- retour au pod partagé : témoin 1 rejoué -------------------------------
kubectl -n "$NS" delete pod temoin-dedie --wait=true --timeout=120s >/dev/null
apply_manifest temoin-deux.yaml
wait_ready
second_uid=$(pod_uid temoin-deux)
second_volume=$(pvc_volume pvc-temoin-a)
[ -n "$second_uid" ] || fail "rejeu: UID de pod illisible"
[ "$dedie_uid" != "$second_uid" ] \
    || fail "rejeu: le pod recréé porte le même UID ($dedie_uid)"
[ "$dedie_volume" = "$second_volume" ] \
    || fail "rejeu: le PVC a changé de volume ($dedie_volume -> $second_volume)"
trace
check_identity a "$UID_A"
check_identity b "$UID_B"
kexec a sh -ec 'test "$(cat "$HOME/.config/ameesh/accounts/.temoin-a")" = temoin-a' \
    || fail "rejeu: le temoin de A n'a pas survécu au déplacement du PVC"
kexec b sh -ec 'test "$(cat "$HOME/.config/ameesh/accounts/.temoin-b")" = temoin-b' \
    || fail "rejeu: le temoin de B n'a pas survécu au déplacement du PVC"
pass "rejeu: pod $dedie_uid -> $second_uid, volume $second_volume, identité et modes conservés, temoins persistants"

# Le pod à deux conteneurs est supprimé avant le témoin 3 pour ne pas
# partager un PVC ReadWriteOnce entre deux pods.
kubectl -n "$NS" delete pod temoin-deux --wait=true --timeout=120s >/dev/null
check_lecture

# --- témoin 1 rejoué après le passage du PVC dans un pod SANS fsGroup ------
# Le pod 3 a monté les deux PVC en lecture seule, sans fsGroup : c'est le
# déplacement qui, dans le constat N de #7659, pouvait élargir les modes au
# groupe. On recrée le pod à deux conteneurs et on rejoue l'identité : les
# dossiers 0700/0600 doivent être intacts.
kubectl -n "$NS" delete pod temoin-lecture --wait=true --timeout=120s >/dev/null
apply_manifest temoin-deux.yaml
wait_ready
trace
check_identity a "$UID_A"
check_identity b "$UID_B"
kexec a sh -ec 'test "$(cat "$HOME/.config/ameesh/accounts/.temoin-a")" = temoin-a' \
    || fail "rejeu final: temoin de A illisible apres le pod sans fsGroup"
pass "rejeu final: identite et modes intacts apres un montage sans fsGroup"

check_canon

# Le contrôle de référence rend 77 si PyYAML manque : non-concluant.
ref_code=0
check_reference || ref_code=$?
[ "$ref_code" -eq 0 ] || [ "$ref_code" -eq 77 ] || exit "$ref_code"

# Le contrôle base rend 77 quand le DSN manque : non-concluant, jamais vert.
base_code=0
check_base || base_code=$?

# Le contrôle login (#7911) rend 77 si PyYAML/python3 manque : non-concluant.
login_code=0
check_login || login_code=$?
[ "$login_code" -eq 0 ] || [ "$login_code" -eq 77 ] || exit "$login_code"

if [ "$ref_code" -eq 77 ] || [ "$base_code" -eq 77 ] || [ "$login_code" -eq 77 ]; then
    echo "TEMOINS CONTENEURS PASSENT ; référence/base/login NON EXÉCUTÉE(S) (non-concluant)" >&2
    exit 77
fi
[ "$base_code" -eq 0 ] || exit "$base_code"
[ "$login_code" -eq 0 ] || exit "$login_code"

echo "TOUS LES TEMOINS PASSENT (#7695 : identité, montage, permissions, superviseur, base, canon #7694 ; #7698 : déplacement pod partagé -> dédié, PVC ré-attaché, présence pod ; #7699 : chart de référence, quota, PDB, digest, preStop ; #7911 : pod de connexion, exec borné à son seul pod, pod partagé et pair refusés)"
