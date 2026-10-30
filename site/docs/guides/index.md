# Guides

Task-oriented pages. Each one uses only commands that exist in the CLI; see the
[CLI reference](../reference/cli.md) for every option.

- [Install on a workstation](install.md): install ameesh as a tool with `uv`
  from the Git repository or a clone, on Linux, macOS and Windows through
  WSL2, and the platform state of v1.10.0.
- [Write a canon](write-a-canon.md): declare humans, hosts, agents and
  placements, check and sync them; add a second canon on the same host.
- [Write a Process card](write-a-process.md): the working rules injected into
  briefs, and the executable `workflow:` block (stages, grouped transitions,
  guards, work types, projection, admission pin, check command).
- [Write a Pipeline](write-a-pipeline.md): a pipeline declared as canon data
  (`pipelines/*.md`), its declarative module, adoption and native commands,
  with the acme and after-sales support examples.
- [Run the demo](run-the-demo.md): the v1 scenario on the bench, step by step.
- [Switch from a v0 setup](switch-from-v0.md): move a running team of agents to
  ameesh, one reversible step at a time; upgrade an existing installation.
- [Cluster profile](cluster-profile.md): one persistent agent per Kubernetes
  pod.
- [Progress view](progress-view.md): lots, agents, milestones and budget, as
  text, JSON or a standalone HTML page.
- [Model catalogue](model-catalogue.md): `ameesh models`.
- [Review classes](review-classes.md): `ameesh review-class`.
- [Operate agents](operate-agents.md): session policy, agent mode,
  `list --json`, cost per turn, gauges and balance, alerts and pushed alerts,
  guarded assignment and delegation, session bindings, host resources, restart
  on a brief, adopt and resume, interruption.
- [Host ameesh-approve for a team](host-ameesh-approve.md): strict profile,
  local TLS, `approve-check`, database role, moving the service.
