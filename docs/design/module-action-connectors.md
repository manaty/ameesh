# Action connectors supplied by a module

A built-in or installed Python module can provide `ACTION_CONNECTORS`, a mapping
from connector name to a constructor. The installed module exposes an
`ameesh.modules` entry point as usual. A connector-only module may return an
empty `types` list in its descriptor; it does not invent a work item type just
to publish an action.

Since contract revision 1.8 (#8342), an installed module imports `Action`,
`Outcome` and `NotSentError` from `ameesh.public.connectors`; `ameesh.connectors`
is the core's implementation and is not a public import path.

```python
NAME = "example"

def descriptor(options=None):
    return {"types": [], "schemas": {}, "rules": []}

ACTION_CONNECTORS = {"example-publish": PublishConnector}
```

The constructor returns an object satisfying `ameesh.connectors.Connector`:
`name`, `dedupe` (`guaranteed` or `none`), and callable `classify`, `execute` and
`reconcile`. Its name must match the registry key. `execute` receives the action
and its `action_id` as the idempotency key. Return `Outcome.unknown` whenever an
effect might have happened; `reconcile` must inspect the external system and
return a confirmed or certainly failed outcome, or `None` when it cannot tell.
A `guaranteed` connector must actually deduplicate that same action key. The
optional timeout has the same meaning as for built-in connectors.

A transport failure raised **before any byte of the request is written** — name
resolution, refused or timed-out connection, TLS handshake — is a certain
failure: raise `ameesh.connectors.NotSentError` (never raise it after sending).
The gate settles the attempt `failed` with a `not sent` detail, releases a
standing reservation, and `ameesh action retry <id> --standing` replays the same
`action_id`, hence the same deduplication key. A read timeout after the request
was sent, a lost response, or any other exception stays `unknown`, because the
effect may have happened. A request the external system received and rejected is
a certain `Outcome.failed`, not `NotSentError`.

Names are global. A module cannot replace `shell-noop`, `git-merge`, or another
module's connector. Discovery reports the refused module and the collision;
requesting an unknown connector never imports a path supplied by the canon.
Delivery connectors (`work deliver`) retain their existing separate interface.

## Activate and classify in the canon

A declaration is installed code, not permission to use it. Select the module
in the trusted canon and classify each offered operation:

```yaml
extensions:
  ameesh:
    projects:
      example-project:
        modules:
          example:
            action_classes:
              example-publish:
                publish: irreversible
                inspect: reversible
```

The normal federation/team/project precedence applies to this entire module
selection. The core removes the reserved `action_classes` option before calling
`descriptor(options)`, validates connector names and classes, and compiles it
into a reserved `ameesh.action-classes` rule in `project_modules`. Its empty
`types` list gives it no lifecycle requirement. Descriptors cannot supply that
reserved rule themselves. `canon sync` applies the policy using the existing
module store; no database migration is needed.

Only the applied policy authorizes the connector in that project. An operation
missing from `action_classes` is refused at proposal, even if `--class` or the
programmatic `canon_class` argument supplies a value. The canonical class wins
over `classify`; no call to the connector's classifier is needed. The gate
rechecks activation and class before approval and execution. A class changed
since proposal requires a new action. Deactivation prevents execution of
previously approved actions. Reconciliation is a read-only observation of an
outcome that already happened: it only checks that the connector name is not
now served by another provider, so a policy withdrawal, a class change or an
uninstalled module never leaves an `unknown` action unsettled.

Compatibility for the existing development connector: `git-merge` keeps its
historical always-active behavior and default classification when no explicit
`dev` project policy is applied. Selecting `dev` with `action_classes` opts its
actions into the same mandatory operation policy. `shell-noop` remains a core
connector. This preserves the existing gate's public API and tests while new
module connectors always require an applied policy.

## Test outside the product checkout

The offline example is under `tests/fixtures/module_connector_bench`. It has a
Python module and distribution metadata exposing a real entry point. The
integration test copies this distribution into its own temporary directory,
outside the checkout, and puts it on the Python search path. It does not install
anything into the host's shared environment and does not contact a service.

```sh
ameesh heavy -- bash scripts/test.sh tests.test_7628_module_connectors
```

The test runs with `AMEESH_MODULES=bare`: no built-in modules, but installed
entry points are still discovered. It compiles and applies a project policy,
proposes through the CLI, approves with a software test authenticator, launches
a journal write, and confirms the uncertain result through `reconcile`. The
example's `classify` deliberately raises: success proves the class came from
the canon. It also tests missing classes, inactive projects, policy withdrawal,
name collisions and malformed connector interfaces.

A real connector distribution uses its own package metadata and environment,
with the same entry point and interface. Test with a fake external client or a
local journal before connecting an account. Keep credentials outside the module
descriptor and action arguments; the action log is inspectable evidence.
