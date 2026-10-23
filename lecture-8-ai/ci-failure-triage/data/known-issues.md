# Known CI issues — Northwind Cloud platform team

A living knowledge base of recurring CI failures. Each entry: symptom pattern to look
for in the log, root cause, and the standard fix/action. Kept next to the pipeline
config so the triage assistant (and humans) can check it before opening a new incident.

---

## KI-001 — npm registry timeout during install

**Symptom pattern:** `ETIMEDOUT`, `network timeout at: https://registry.npmjs.org`,
step name contains `install` or `npm ci`.

**Root cause:** Transient network flakiness between the CI runners and the npm
registry mirror. Not caused by our code or dependency changes.

**Standard action:** Not a real failure — retry the job. If it fails 3 times in a row
for the same job, escalate to #platform-infra (runner network issue).

**Severity if it recurs:** low (self-healing), escalate to medium after 3 consecutive
retries on the same pipeline.

---

## KI-002 — Postgres test container fails to become ready in time

**Symptom pattern:** `connection refused`, `pg_isready`, `timed out waiting for
database`, step name contains `test` or `integration`.

**Root cause:** The Postgres service container in CI sometimes takes longer than the
30s healthcheck window to finish initdb on a cold runner.

**Standard action:** Not a code issue. Retry the job. If it keeps happening on the
same runner, flag the runner for the infra team to check disk I/O.

**Severity if it recurs:** low.

---

## KI-003 — Out of memory during `docker build`

**Symptom pattern:** `Killed`, `OOMKilled`, `exit code 137`, step name contains
`docker build` or `image build`.

**Root cause:** Multi-stage builds with a large dependency install stage (e.g.
`node_modules` or Python wheels) can exceed the 4 GB memory limit on standard
runners when several build stages run in parallel.

**Standard action:** Real infra limitation, not code regression — but worth a ticket
if it starts happening on a repo where it hasn't before, since it blocks every
subsequent build on that branch. Suggest splitting the build stage or requesting a
large runner.

**Severity if it recurs:** medium.

---

## KI-004 — Snapshot test mismatch after a dependency bump

**Symptom pattern:** `snapshot mismatch`, `toMatchSnapshot`, `expected ... received`,
step name contains `test` or `unit`.

**Root cause:** A minor/patch dependency bump (usually a UI library or formatter)
silently changes rendered output, and checked-in snapshots go stale.

**Standard action:** Usually safe to regenerate snapshots after a human confirms the
new output is correct — not an automatic fix, needs a developer's eyes.

**Severity if it recurs:** low, but always needs human confirmation before
auto-resolving.

---

*(Only test/assertion failures with a clear root cause in application code, or any
failure pattern NOT listed above, should be treated as new and escalated — see the
triage skill for the exact decision rule.)*
