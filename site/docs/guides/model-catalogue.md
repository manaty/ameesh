# Model catalogue

`ameesh models` keeps a catalogue of the models each provider offers, with
their prices and the options each harness accepts, so that routing (`ameesh set
<agent> model=… effort=…`) and cost accounting work from known data.

```bash
ameesh models list [--harness H] [--json]   # the catalogue
ameesh models show <model> [--json]         # context window, prices, source, harness, efforts
ameesh models discover [--source S] [--dry-run] [--notify AGENT] [--sender NAME]
```

## Discovery never spends a token

`discover` reads **list endpoints only**, never a completion:

| Source | What it reads | Can it retire a model? |
|---|---|---|
| `anthropic` | the provider's model list (`/v1/models`), key from `ANTHROPIC_API_KEY` | yes |
| `openai` | the provider's model list, key from `OPENAI_API_KEY` | yes |
| `deepseek` | the provider's model list, key from `DEEPSEEK_API_KEY` | yes |
| `prices` | the local price table (`AMEESH_PRICES`, else the built-in defaults) | no: it gives prices |
| `harness` | how each harness accepts a model and an effort (its flags) | no: harnesses do not publish their model lists without a paid call |

Without a key, a provider source reports itself incomplete and **retires
nothing**. Keys come from the environment (or the host's keychain) and are
logged nowhere. Requests require HTTPS, refuse redirects and check the origin,
so a key never follows a redirect to another origin.

Without `--source`, every source is read. `--dry-run` prints what each source
sees without writing anything.

## Retirement rules

A model disappearing from a list is not enough to retire it. In order:

1. an **incomplete or failed** list retires nothing;
2. a **complete but empty** list retires nothing, and the event says so;
3. a **mass withdrawal** (more than half of the source's active models) is
   refused: nothing is retired, an event "mass withdrawal refused" is produced,
   and **a human decides**;
4. a **local** source (prices, harness options) never retires anything.

## Change events

Each change (models seen, retired, refused withdrawal) is written to the
team's readable thread. With `--notify <agent>`, it is also delivered to that
agent's mailbox as an **event** (so an orchestrator is woken, subject to event
coalescing). `--sender` sets the author (default `AMEESH_AGENT`, else
`catalogue`).

`discover` exits with 1 when a source could not be read completely (outside
`--dry-run`), so it can run from a timer and alert.

!!! note "Planned"
    Evaluation of models on reference tasks, and recommendations, are planned
    work.
