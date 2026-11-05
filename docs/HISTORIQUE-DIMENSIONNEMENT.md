# Historique de dimensionnement

Les ressources d'un tour (`turn_resources`) portent deux compteurs cgroup v2 :
`memory_peak_bytes` (`memory.peak`, octets) et `cpu_usage_usec` (`cpu.stat`,
`usage_usec`, microsecondes CPU cumulées, tous descendants du cgroup compris).
Ils sont lus avant la suppression du cgroup, y compris après le suivi du
travail de fond. Les compteurs absents restent NULL : ni estimation depuis
`memory.current`, ni zéro supposé. Les ressources sans cgroup dédié, notamment
les exécuteurs médiés, restent sans ces mesures.

`host_resource_daily` conserve les journées UTC courante et précédentes,
sur 90 jours. Sa clé est `(host, day)` ; les deux documents JSON distinguent
les populations :

- `host_metrics` : chaque relevé de `host_resources` compte une fois, sans
  pondération par durée ni déduplication entre exécuteurs. Mémoire utilisée
  (`mem_total_bytes - mem_available_bytes`) p95/max, `app_cpu` p95/max en
  cœurs, concurrence p95/max, PSI CPU/mémoire/I/O p95/max en pourcentage.
- `turn_metrics` : tours rattachés à leur journée UTC de début. Pic mémoire
  p95/max et somme des microsecondes CPU, avec nombres de tours et de
  compteurs disponibles. Un tour sans mesure ne compte pas dans le percentile.
  Un tour chevauchant minuit est actualisé sur son jour de début à sa fermeture.

Les percentiles sont exacts (`percentile_cont(0.95)`). Le relevé d'hôte et
son agrégat sont publiés dans la même transaction. Les compteurs de tour et
leur agrégat le sont aussi, sous le même verrou par hôte. Rejouer la collecte
ne double pas les compteurs. Les agrégats restent après la purge à sept jours
des relevés bruts ; les écritures de relevés et de compteurs purgent les
journées hors rétention. Un mesh sans écriture ne déclenche pas de purge.
Les journées antérieures à l'activation ne sont pas reconstruites ; le
premier relevé inclut les mesures brutes déjà présentes du jour courant.

Lecture, par exemple :

```sql
SELECT host, day, host_metrics, turn_metrics
FROM host_resource_daily
ORDER BY host, day;
```

La migration additive 0546 doit précéder la relance des exécuteurs. Les
écrivains N-1 peuvent ignorer les nouvelles colonnes, qui restent NULL.
Les mesures sont internes au mesh et n'ajoutent aucun droit superviseur.
