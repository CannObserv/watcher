#!/usr/bin/env bash
# Audit uv.lock against OSV and report what it finds. Never fail (#397).
#
# Run by .github/workflows/audit.yml and by hand: `bash scripts/audit_lock.sh`.
# Ported from CannObserv/broker#86 and CannObserv/archiver#420. Two audits, each
# reported under its own title: runtime (--no-dev) and dev (--only-dev). Apart,
# so a standing dev advisory (virtualenv, pygments) never masks a new runtime
# one. The two closures share packages, so a finding in a shared one shows
# under both titles; the runtime title is the signal.
#
# Report-only on purpose: a gate that failed on a finding would turn main red
# on someone else's disclosure date. So every exit is 0:
#   uv exit 1 (findings)        -> warning "uv.lock <group> advisories"
#   any other non-zero exit     -> warning "uv.lock <group> audit did not run", never clean
#   checkout not entered        -> warning "uv audit did not run", uv never called
# Each audit's text goes to the job summary. In Actions the stdout copy sits
# between stop-commands and a random resume token: it is OSV's text, and a line
# of it starting `::` would otherwise be read as a workflow command.
#
# --frozen reads uv.lock as committed, without the wheelhouse (measured on
# #397). A relock would need it (AGENTS.md, Environment & Tooling), and the
# job has no credential to fetch it. `uv audit` is a preview command, hence the
# flag. uv before 0.11.19 (astral-sh/uv#19645) has the command but not the name
# `audit-command`, and rejects it with exit 2 and "Unknown feature flag", so
# that one rejection retries without the flag. From 0.11.20 (astral-sh/uv#19669)
# an unknown name only warns, so a later uv that drops the name still audits.
# tests/scripts/test_audit_lock.py pins all of it.
set -u

# The lock is the one beside this script. A checkout it cannot enter is not
# audited: the caller's directory may hold some other lock (broker#112).
if ! cd "$(dirname "$0")/.."; then
	echo "::warning title=uv audit did not run::cannot enter the checkout at $(dirname "$0")/..; the lock was not audited."
	exit 0
fi

# <label>|<group flag>
AUDITS=(
	"uv.lock runtime|--no-dev"
	"uv.lock dev|--only-dev"
)

audit() { # <group flag>; sets out and rc
	out=$(uv audit --frozen "$1" --preview-features audit-command 2>&1)
	rc=$?
	if [ "$rc" -eq 2 ] && [[ $out == *"Unknown feature flag"* ]]; then
		out=$(uv audit --frozen "$1" 2>&1)
		rc=$?
	fi
}

for entry in "${AUDITS[@]}"; do
	IFS='|' read -r label group <<<"$entry"
	audit "$group"

	if [ -n "${GITHUB_ACTIONS:-}" ]; then
		token=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
		echo "::stop-commands::$token"
		printf '%s\n' "$out"
		echo "::$token::"
		printf '### %s: uv audit --frozen %s (exit %s)\n\n```\n%s\n```\n\n' \
			"$label" "$group" "$rc" "$out" >>"${GITHUB_STEP_SUMMARY:-/dev/null}"
	else
		printf '== %s (exit %s)\n%s\n' "$label" "$rc" "$out"
	fi

	case $rc in
	0) ;;
	1) echo "::warning title=$label advisories::uv audit found advisories in $label; the job summary lists them. Sync the wheelhouse, then uv lock --upgrade-package <name> (docs/DEPENDENCIES.md)." ;;
	*) echo "::warning title=$label audit did not run::uv audit exited $rc; $label was not audited. The job summary has its output." ;;
	esac
done
exit 0
