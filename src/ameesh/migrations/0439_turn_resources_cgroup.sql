-- SPDX-License-Identifier: AGPL-3.0-only
-- L498 (#498) : le cgroup délégué d'un tour, à côté du groupe de processus.
--
-- `turn_resources.pgid` ne peut pas prouver la propriété d'un descendant qui
-- a fait `setsid` ou un double `fork` après le dernier relevé ; un cgroup v2
-- délégué, lui, suit `fork`/`exec`, ne se réattribue pas comme un pid et se
-- vide d'un bloc par `cgroup.kill`. Le chemin est **relatif à la monture
-- cgroup v2** (la forme de `/proc/self/cgroup`, par exemple
-- `/user.slice/…/ameesh-runner-agent@<agent>.service/turn-<id>`) : stable
-- d'un redémarrage à l'autre, indépendant du point de montage, et
-- directement exploitable pour signaler ou nettoyer un orphelin.
-- NULL = tour hors cgroup délégué (repli PID/PGID : hôte non Linux,
-- délégation absente, exécuteur médié).
alter table turn_resources add column if not exists cgroup text;

comment on column turn_resources.cgroup is
    'L498 : cgroup v2 délégué du tour, chemin relatif à la monture cgroup v2 ; NULL hors délégation';
