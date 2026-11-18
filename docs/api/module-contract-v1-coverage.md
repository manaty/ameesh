# External module contract v1: coverage

Each element of the [contract](module-contract-v1.md) maps to the test of the
ameesh CI that fails when that element breaks
([#7665 / #5857](https://github.com/manaty/ameesh/issues/5857)). The frozen
baseline is [module-contract-v1.surface.json](module-contract-v1.surface.json);
the witness is the [contract-witness](../../examples/modules/contract-witness)
distribution, outside `src/`, which imports neither ameesh nor `tests`.
`tests.test_7665_module_contract.CoverageTableTest` checks that every element
has exactly one row and that every cited test exists.

The surface freezes revision 1.2; its baseline commit stays the revision 1.0
comparison point, and the elements that revisions 1.1 and 1.2 add are now
listed. The comparison is additive-compatible, as the contract's compatibility
rule states:

- HTTP: every schema is flattened per field (type, `const`, `enum`, bounds,
  `pattern`, `format`, `required`, `additionalProperties`), for parameters,
  request bodies and every declared response. An input may only widen: no field
  removed, no value refused that was accepted, nothing newly required. An output
  may only narrow: no field removed or no longer required, no other type,
  constant or bound. An added output field or enumeration value is compatible,
  since a consumer reads an unknown one as unknown.
- CLI: the options of each command's `--help` equal the frozen ones. A removed
  option is a break. An added one is compatible, but it has to be listed in the
  surface when it ships.
- Module interface: frozen record fields are required, and added ones are
  compatible. A frozen call signature keeps every parameter, its position, its
  kind and its default, and an added parameter has a default.

| Element | Tests |
| --- | --- |
| `compatibility` | `tests.test_7665_module_contract.CompatibilityTest` |
| `cli:ameesh --version` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options` |
| `cli:ameesh modules` | `tests.test_7665_module_contract.CliContractTest.test_modules_json_describes_the_installed_witness`, `tests.test_7665_module_contract.CliContractTest.test_a_reserved_or_mismatched_entry_point_is_refused` |
| `cli:ameesh module` | `tests.test_7665_module_contract.CliContractTest.test_module_list_is_reserved_installation_evidence`, `tests.test_7665_module_contract.CliContractTest.test_module_help_runs_without_a_database_and_dispatch_needs_one`, `tests.test_7665_module_contract.StoreAndViewsTest.test_activation_dispatch_and_named_store_operations` |
| `cli:ameesh canon check` | `tests.test_7665_module_contract.CliContractTest.test_canon_selection_validates_the_witness_options` |
| `cli:ameesh canon sync` | `tests.test_7665_module_contract.StoreAndViewsTest.test_activation_dispatch_and_named_store_operations` |
| `cli:ameesh canon snapshot` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options`, `tests.test_7669_canon_snapshot.SnapshotContract` |
| `cli:ameesh turn current` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options`, `tests.test_l2894_turn_context` |
| `cli:ameesh turn observe` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options`, `tests.test_l2894_turn_observe` |
| `cli:ameesh agent observe` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options`, `tests.test_7670_agent_observation.AgentObservationTest.test_cli_and_http_use_the_builder` |
| `cli:ameesh list` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options`, `tests.test_7675_deferral.PublicViewsTest.test_list_json_carries_the_same_block` |
| `cli:ameesh show` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options`, `tests.test_7675_deferral.PublicViewsTest.test_show_json_carries_the_block` |
| `cli:ameesh migrate` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options`, `tests.test_migrations_additives` |
| `cli:ameesh action propose` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options` |
| `cli:ameesh action approve` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options` |
| `cli:ameesh action execute` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options` |
| `cli:ameesh action reconcile` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options` |
| `cli:agent-runner` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options`, `tests.test_3925_admission_dynamique` |
| `cli:agent-mail send` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options` |
| `cli:agent-mail show` | `tests.test_7665_module_contract.CliContractTest.test_the_help_documents_exactly_the_frozen_options` |
| `http:discovery` | `tests.test_7665_module_contract.HttpContractTest.test_discovery_then_openapi_without_a_token`, `tests.test_l84_serve.DefencesTest.test_discovery_and_openapi_without_token` |
| `http:operations` | `tests.test_7665_module_contract.CompatibilityTest.test_the_served_openapi_keeps_every_baseline_operation`, `tests.test_7665_module_contract.HttpContractTest.test_discovery_then_openapi_without_a_token` |
| `http:authentication` | `tests.test_7665_module_contract.HttpContractTest.test_every_other_documented_path_requires_the_token`, `tests.test_l84_serve.DefencesTest.test_token_required_and_compared` |
| `http:schemas` | `tests.test_7665_module_contract.CompatibilityTest.test_the_comparator_tells_additions_from_breaks`, `tests.test_7665_module_contract.HttpContractTest.test_an_authenticated_read_answers_its_documented_schema`, `tests.test_l84_serve.OpenApiTest.test_every_route_documented_with_its_schema`, `tests.test_l84_serve.ServePgTest.test_every_route_answers_in_line_with_its_schema` |
| `http:agent-observation` | `tests.test_7665_module_contract.CompatibilityTest.test_the_served_openapi_keeps_every_baseline_operation`, `tests.test_7665_module_contract.HttpContractTest.test_every_other_documented_path_requires_the_token`, `tests.test_7670_agent_observation.AgentObservationTest.test_cli_and_http_use_the_builder` |
| `http:idempotency` | `tests.test_l87_serve_actions.GuardsTest.test_one_idempotency_key_required`, `tests.test_l87c_alert_ack.AcknowledgementTest.test_idempotent_and_durable_with_actor_and_principal` |
| `http:events` | `tests.test_l85_events.StreamPgTest.test_without_last_event_id_only_new_events`, `tests.test_l85_events.StreamPgTest.test_reset_when_events_were_lost`, `tests.test_l85_events.StreamPgTest.test_delayed_is_observable_while_a_transaction_holds_events` |
| `module:name_pattern` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_names_follow_the_published_pattern` |
| `module:reserved_names` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_names_follow_the_published_pattern`, `tests.test_7665_module_contract.CliContractTest.test_a_reserved_or_mismatched_entry_point_is_refused` |
| `module:descriptor_keys` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_descriptor_keys_and_options` |
| `module:capabilities` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_capabilities_are_the_published_names_and_are_checked` |
| `module:callbacks` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_callbacks_keep_their_calling_convention`, `tests.test_7665_module_contract.StoreAndViewsTest.test_activation_dispatch_and_named_store_operations` |
| `module:discover` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_discover_signature_and_registry_record`, `tests.test_7665_module_contract.ModuleInterfaceTest.test_the_interface_comparators_tell_additions_from_breaks` |
| `module:registry` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_discover_signature_and_registry_record` |
| `module:record_fields` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_discover_signature_and_registry_record`, `tests.test_7665_module_contract.CliContractTest.test_modules_json_describes_the_installed_witness`, `tests.test_7665_module_contract.ModuleInterfaceTest.test_the_interface_comparators_tell_additions_from_breaks` |
| `module:storage_attributes` | `tests.test_7665_module_contract.ModuleStorageContractTest.test_the_frozen_storage_attributes_are_the_module_declaration`, `tests.test_7649_schema_modules.DeclarationTest.test_declaration_reads_schema_migrations_and_requires` |
| `module:selection_scopes` | `tests.test_7665_module_contract.CliContractTest.test_canon_selection_validates_the_witness_options`, `tests.test_7665_module_contract.CliContractTest.test_the_bare_profile_keeps_installed_entry_points_only`, `tests.test_l98_modules_activation.ActivationStorageTest.test_sync_stores_the_resolved_rows_and_active_reads_them` |
| `module:store_operations` | `tests.test_7665_module_contract.ModuleInterfaceTest.test_store_named_operations_keep_their_signatures`, `tests.test_7665_module_contract.StoreAndViewsTest.test_activation_dispatch_and_named_store_operations`, `tests.test_7665_module_contract.ModuleInterfaceTest.test_the_interface_comparators_tell_additions_from_breaks` |
| `runner:agents-file` | `tests.test_3925_admission_dynamique` |
| `runner:turn-context` | `tests.test_l2894_turn_context` |
| `runner:turn-observation` | `tests.test_l2894_turn_observe`, `tests.test_l84_serve` |
| `canon:session-policy` | `tests.test_7665_module_contract.CanonPolicyContractTest.test_the_frozen_session_keys_are_the_declared_policy`, `tests.test_7674_politique_declarative.DeclaredPolicyCardTest.test_politique_lue_et_serialisee` |
| `canon:resource-policy` | `tests.test_7665_module_contract.CanonPolicyContractTest.test_the_frozen_resource_keys_are_native_thresholds`, `tests.test_7675_qualification.PressureTest.test_cpu_used_is_the_ima_cpu_gate_and_needs_the_new_key`, `tests.test_7675_qualification.PressureTest.test_throttled_periods_and_wall_time_are_distinct_and_both_kept`, `tests.test_7675_qualification.PressureTest.test_the_new_keys_accept_fractions_like_the_other_ceilings` |
| `deferral` | `tests.test_7665_module_contract.DeferralContractTest.test_the_frozen_block_shape_and_closed_enums`, `tests.test_7675_deferral.MappingTest.test_the_block_validates_against_the_public_schema`, `tests.test_7675_deferral.PublicViewsTest.test_show_json_carries_the_block`, `tests.test_7675_deferral.PublicViewsTest.test_list_json_carries_the_same_block` |
| `python:mail` | `tests.test_7665_module_contract.PythonMailContractTest.test_the_public_mail_signatures_are_frozen`, `tests.test_7665_module_contract.PythonMailContractTest.test_the_value_objects_keep_their_frozen_fields`, `tests.test_7665_module_contract.PythonMailContractTest.test_the_published_bounds_enums_and_errors_are_frozen`, `tests.test_7665_module_contract.PythonMailContractTest.test_the_served_contract_version_is_frozen`, `tests.test_7671_mail_contract.ContractSurfaceTest.test_version_and_exported_surface`, `tests.test_7671_mail_contract.BatchTest.test_a_batch_is_bounded_before_any_deposit`, `tests.test_7671_mail_contract.HistoryTest.test_cursor_is_bounded_and_stable_under_new_mail`, `tests.test_7706_mail_contract.ReservedActorTest.test_core_sender_identities_are_refused_before_deposit` |
| `view:agent_mesh_overview` | `tests.test_7665_module_contract.StoreAndViewsTest.test_named_views_keep_their_admitted_columns`, `tests.test_cluster.ContratDesColonnesTest` |
| `view:mesh_keys` | `tests.test_7665_module_contract.StoreAndViewsTest.test_named_views_keep_their_admitted_columns`, `tests.test_cluster.ContratDesColonnesTest` |
| `view:mesh_approvals_status` | `tests.test_7665_module_contract.StoreAndViewsTest.test_named_views_keep_their_admitted_columns`, `tests.test_cluster.ContratDesColonnesTest` |
| `fixture` | Not covered yet: the published fixture kit (consumer-owned disposable database, no `tests.support`) is lot #7701 ([#5902](https://github.com/manaty/ameesh/issues/5902)). |
| `stable-pin` | `tests.test_7666_stable_channel.StableChannelTest.test_witness_moves_from_one_stable_to_the_next_by_its_pin_alone`, `tests.test_7666_stable_channel.StableChannelTest.test_verify_refuses_an_edited_pin_or_another_installation` ([#7666 / #5858](https://github.com/manaty/ameesh/issues/5858)) |

The 1.1 elements (E1a snapshot, E1b observation) and the 1.2 elements (module
schema/migrations, E2a mail, E3a session policy, E3b bounded deferral) are
released and have their rows above (mapping,
[IMA public contract](../design/ima-public-contract-mapping.md)).
