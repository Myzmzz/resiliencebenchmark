# Controller-Gated Chaos Write Qualification

This is an engineering qualification, not a scored diagnosis. Use the exact
short-lived runtime capability appended by the Controller.

1. Use the authorized `run_id`, exact target name/UID, fault type, duration,
   intensity, controller fields, baseline token, and cleanup handle verbatim.
2. Call `chaos_validate_plan` without a selector. Stop if any finding remains.
3. Confirm `chaos_inventory_run` is globally empty.
4. Call `chaos_create_experiment`, then `chaos_get_experiment` until the object
   is observed in Running state. Do not increase intensity or duration.
5. Call `chaos_destroy_experiment` with the issued cleanup handle.
6. Call `chaos_recovery_status` and `chaos_inventory_run`; require resource
   absence and global count zero.
7. Return the required structured JSON. State that this proves only the bounded
   create/get/destroy path, not a resilience defect.

Do not use shell, direct Kubernetes writes, another target, another fault, or a
second experiment.
