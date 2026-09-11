"""Operational jobs that run beside the service, not inside it (#296).

Each module here is an entry point for a systemd unit or an operator's shell:
``backup`` ships the database to a bucket nightly, ``restore`` brings a shipped
dump back, and ``checkin`` reports each backup run to notifier's dead-man
monitor. None of them imports the application; they touch the database only
through ``pg_dump`` / ``pg_restore`` / ``psql``. Runbook: docs/RECOVERY.md.
"""
