# External module contract v1: coverage

Each element of the [contract](module-contract-v1.md) maps to the test of the
ameesh CI that fails when that element breaks
([#7665 / #5857](https://github.com/manaty/ameesh/issues/5857)). The frozen
baseline is [module-contract-v1.surface.json](module-contract-v1.surface.json);
the witness is the [contract-witness](../../examples/modules/contract-witness)
distribution, outside `src/`, which imports neither ameesh nor `tests`.
`tests.test_7665_module_contract.CoverageTableTest` checks that every element
has exactly one row and that every cited test exists.

| Element | Tests |
| --- | --- |
| `compatibility` | `tests.test_7665_module_contract.CompatibilityTest` |
| `cli:ameesh --version` | `tests.test_7665_module_contract.CliContractTest.test_every_documented_option_is_in_the_help` |
| `cli:ameesh modules` | `tests.test_7665_module_contract.CliContractTest.test_modules_json_describes_the_installed_witness`, `tests.test_7665_module_contract.CliContractTest.test_a_reserved_or_mismatched_entry_point_is_refused` |
| `cli:ameesh module` | `tests.test_7665_module_contract.CliContractTest.test_module_list_is_reserved_installation_evidence`, `tests.test_7665_module_contract.CliContractTest.test_module_help_runs_without_a_database_and_dispatch_needs_one`, `tests.test_7665_module_contract.StoreAndViewsTest.test_activation_dispatch_and_named_store_operations` |
| `cli:ameesh canon check` | `tests.test_7665_module_contract.CliContractTest.test_canon_selection_validates_the_witness_options` |
| `cli:ameesh canon sync` | `tests.test_7665_module_contract.StoreAndViewsTest.test_activation_dispatch_and_named_store_operations` |
| `cli:ameesh turn current` | `tests.test_7665_module_contract.CliContractTest.test_every_documented_option_is_in_the_help`, `tests.test_l2894_turn_context` |
| `cli:ameesh turn observe` | `tests.test_7665_module_contract.CliContractTest.test_every_documented_option_is_in_the_help`, `tests.test_l2894_turn_observe` |
| `cli:ameesh migrate` | `tests.test_7665_module_contract.CliContractTest.test_every_documented_option_is_in_the_help`, `tests.test_migrations_additives` |
| `cli:ameesh action propose` | `tests.test_7665_module_contract.CliContractTest.test_every_documented_option_is_in_the_help` |
| `cli:ameesh action approve` | `tests.test_7665_module_contract.CliContractTest.test_every_documented_option_is_in_the_help` |
| `cli:ameesh action execute` | `tests.test_7665_module_contract.CliContractTest.test_every_documented_option_is_in_the_help` |
| `cli:ameesh action reconcile` | `tests.test_7665_module_contract.CliContractTest.test_every_documented_option_is_in_the_help` |
| `cli:agent-runner` | `tests.test_7665_module_contract.CliContractTest.test_every_documented_option_is_in_the_help`, `tests.test_3925_admission_dynamique` |
| `cli:agent-mail send` | `tests.test_7665_module_contract.CliContractTest.test_every_documented_option_is_in_the_help` |
| `cli:agent-mail show` | `tests.test_7665_module_contract.CliContractTest.test_every_documented_option_is_in_the_help` |
| `http:discovery` | `tests.test_7665_module_contract.HttpContractTest.test_discovery_then_openapi_without_a_token` |
| `http:operations` | `tests.test_7665_module_contract.CompatibilityTest.test_the_served_openapi_keeps_every_baseline_operation`, `tests.test_7665_module_contract.HttpContractTest.test_discovery_then_openapi_without_a_token` |
| `http:authentication` | `tests.test_7665_module_contract.HttpContractTest.test_every_other_documented_path_requires_the_token`, `tests.test_l84_serve.DefencesTest.test_token_required_and_compared` |
| `http:schemas` | `tests.test_7665_module_contract.HttpContractTest.test_an_authenticated_read_answers_its_documented_schema`, `tests.test_l84_serve.OpenApiTest.test_every_route_documented_with_its_schema` |
| `http:idempotency` | `tests.test_l87_serve_actions.GuardsTest.test_one_idempotency_key_required`, `tests.test_l87c_alert_ack.AcknowledgementTest.test_idempotent_and_durable_with_actor_and_principal` |
| `http:events` | `tests.test_l85_events.StreamPgTest.test_without_last_event_id_only_new_events`, `tests.test_l85_events.StreamPgTest.test_reset_when_events_were_lost`, `tests.test_l85_events.StreamPgTest.test_delayed_is_observable_while_a_transaction_holds_events` |
| `module:name_pattern` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_names_follow_the_published_pattern` |
| `module:reserved_names` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_names_follow_the_published_pattern`, `tests.test_7665_module_contract.CliContractTest.test_a_reserved_or_mismatched_entry_point_is_refused` |
| `module:descriptor_keys` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_descriptor_keys_and_options` |
| `module:capabilities` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_capabilities_are_the_published_names_and_are_checked` |
| `module:callbacks` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_callbacks_keep_their_calling_convention`, `tests.test_7665_module_contract.StoreAndViewsTest.test_activation_dispatch_and_named_store_operations` |
| `module:discover` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_discover_signature_and_registry_record` |
| `module:registry` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_discover_signature_and_registry_record` |
| `module:record_fields` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_discover_signature_and_registry_record`, `tests.test_7665_module_contract.CliContractTest.test_modules_json_describes_the_installed_witness` |
| `module:selection_scopes` | `tests.test_7665_module_contract.CliContractTest.test_canon_selection_validates_the_witness_options`, `tests.test_7665_module_contract.CliContractTest.test_the_bare_profile_keeps_installed_entry_points_only`, `tests.test_l98_modules_activation.ActivationStorageTest.test_sync_stores_the_resolved_rows_and_active_reads_them` |
| `module:store_operations` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_store_named_operations_keep_their_signatures`, `tests.test_7665_module_contract.StoreAndViewsTest.test_activation_dispatch_and_named_store_operations` |
| `runner:agents-file` | `tests.test_3925_admission_dynamique` |
| `runner:turn-context` | `tests.test_l2894_turn_context` |
| `runner:turn-observation` | `tests.test_l2894_turn_observe`, `tests.test_l84_serve` |
| `view:agent_mesh_overview` | `tests.test_7665_module_contract.StoreAndViewsTest.test_named_views_keep_their_admitted_columns`, `tests.test_cluster.ContratDesColonnesTest` |
| `view:mesh_keys` | `tests.test_7665_module_contract.StoreAndViewsTest.test_named_views_keep_their_admitted_columns`, `tests.test_cluster.ContratDesColonnesTest` |
| `view:mesh_approvals_status` | `tests.test_7665_module_contract.StoreAndViewsTest.test_named_views_keep_their_admitted_columns`, `tests.test_cluster.ContratDesColonnesTest` |
| `fixture` | Not covered yet: the published fixture kit (consumer-owned disposable database, no `tests.support`) is a follow-up lot, requested from the pilot under #5827. |
| `stable-pin` | Not covered yet: the stable channel and the single consumer pin are [#7666 / #5858](https://github.com/manaty/ameesh/issues/5858). |

E1–E3 runner extension points get their rows when they are released
(mapping, [IMA public contract](../design/ima-public-contract-mapping.md)).
