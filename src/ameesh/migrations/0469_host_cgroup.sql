-- #2814 : admission consciente du conteneur.
--
-- Le relevé d'hôte porte désormais la mesure du cgroup v2 de CE processus
-- (PSI `cpu`/`memory`, `cpu.max`, `cpu.stat`, `memory.current`/`memory.max`),
-- telle que `resources.CgroupMeter` la lit. Elle est publiée en JSON plutôt
-- qu'en colonnes typées : le jeu de mesures du cgroup peut s'étendre sans
-- nouvelle migration, et le relevé d'hôte reste lisible sans la mesure
-- (`NULL`, ou `{}` après décodage).
alter table host_resources
    add column if not exists cgroup jsonb;
