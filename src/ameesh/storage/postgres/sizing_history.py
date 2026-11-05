# SPDX-License-Identifier: AGPL-3.0-only
"""#5921: exact daily distributions, retained independently of raw readings.

Host distributions use readings; turn distributions use cgroup counters and
are assigned to the UTC day the turn started. Missing counters stay NULL.
Refreshes share the existing per-host gate lock with Hosts.record, so two
runners cannot publish an older aggregate over a newer one.
"""
from __future__ import annotations


def refresh_host(db, host, day):
    db.execute("""
        INSERT INTO host_resource_daily(host, day, host_metrics)
        SELECT %s, %s::date, jsonb_build_object(
            'samples', count(*),
            'memory_samples', count(mem_total_bytes - mem_available_bytes),
            'memory_p95_bytes', percentile_cont(0.95) WITHIN GROUP
                (ORDER BY mem_total_bytes - mem_available_bytes),
            'memory_max_bytes', max(mem_total_bytes - mem_available_bytes),
            'app_cpu_samples', count(app_cpu),
            'app_cpu_p95', percentile_cont(0.95) WITHIN GROUP (ORDER BY app_cpu),
            'app_cpu_max', max(app_cpu),
            'concurrency_p95', percentile_cont(0.95) WITHIN GROUP (ORDER BY turns_in_progress),
            'concurrency_max', max(turns_in_progress),
            'psi_cpu_some_p95', percentile_cont(0.95) WITHIN GROUP (ORDER BY psi_cpu_some),
            'psi_cpu_some_max', max(psi_cpu_some),
            'psi_mem_some_p95', percentile_cont(0.95) WITHIN GROUP (ORDER BY psi_mem_some),
            'psi_mem_some_max', max(psi_mem_some),
            'psi_mem_full_p95', percentile_cont(0.95) WITHIN GROUP (ORDER BY psi_mem_full),
            'psi_mem_full_max', max(psi_mem_full),
            'psi_io_some_p95', percentile_cont(0.95) WITHIN GROUP (ORDER BY psi_io_some),
            'psi_io_some_max', max(psi_io_some))
          FROM host_resources
         WHERE host = %s AND sampled_at >= %s::date::timestamp AT TIME ZONE 'UTC'
           AND sampled_at < (%s::date + 1)::timestamp AT TIME ZONE 'UTC'
        ON CONFLICT (host, day) DO UPDATE SET host_metrics = excluded.host_metrics
    """, (host, day, host, day, day))


def refresh_turns(db, host, day):
    db.execute("""
        INSERT INTO host_resource_daily(host, day, turn_metrics)
        SELECT %s, %s::date, jsonb_build_object(
            'turns', count(*), 'memory_samples', count(memory_peak_bytes),
            'memory_p95_bytes', percentile_cont(0.95) WITHIN GROUP (ORDER BY memory_peak_bytes),
            'memory_max_bytes', max(memory_peak_bytes),
            'cpu_samples', count(cpu_usage_usec), 'cpu_usage_usec', sum(cpu_usage_usec))
          FROM turn_resources
         WHERE host = %s AND started_at >= %s::date::timestamp AT TIME ZONE 'UTC'
           AND started_at < (%s::date + 1)::timestamp AT TIME ZONE 'UTC'
        ON CONFLICT (host, day) DO UPDATE SET turn_metrics = excluded.turn_metrics
    """, (host, day, host, day, day))


def prune(db):
    # Today and the preceding 89 UTC days. No deletion of turn_resources.
    db.execute("DELETE FROM host_resource_daily WHERE day < "
               "(now() AT TIME ZONE 'UTC')::date - 89", ())
