"""Operational jobs that run beside the service, not inside it (#296).

Each module here is an entry point for a systemd unit or an operator's shell:
``backup`` ships the database to a bucket nightly, ``restore`` brings a shipped
dump back, ``checkin`` reports each backup run to notifier's dead-man monitor,
and ``prune_job_history`` is the one-off that prunes the job-history backlog
before the hourly retention task takes over (#296 D7).

The backup modules touch the database only through ``pg_dump`` /
``pg_restore`` / ``psql``; the prune goes through procrastinate's own API.
Runbook: docs/RECOVERY.md.
"""
