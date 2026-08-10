# Evaluation report

Vendor-Assessment Agent · policy v1.0 · evaluation date 2026-08-01 · deterministic
(`rule`) planner · 14 supplied requests, 9 extra requests, 127 tests.

## Success rate

| Measure | Result |
| ------- | ------ |
| Supplied requests matching the expected decision | **14 / 14 (100%)** |
| Expected decisions derived by hand from `vendor_policy.md` | `golden/expected_decisions.json` |
| Match criteria | action, stop reason, applied rules, missing fields, injection sources, duplicate flag, retry floor |
| Test suite | **127 passed** |
| Extra edge-case requests | 9, all as expected |

Decisions issued: 5 `APPROVE`, 6 `ESCALATE`, 2 `REQUEST_INFORMATION`, 1 `REJECT`.
64 loop steps and 62 tool calls in total — 4.6 steps per request, well inside the
budget of 14. Observed tool outcomes: 55 success, 4 timeout, 3 empty result.
5 retries were attempted, 1 further retry was refused by the budget, and 1
injection attempt was quarantined.

The scored run is reproducible with `python scripts/run_batch.py`; the table it
writes is [batch_report.md](batch_report.md).

## What the unreliable conditions produced

| Condition | Case | Behaviour |
| --------- | ---- | --------- |
| Recoverable timeout | VR-006 | primary risk route timed out, the approved backup answered, `APPROVE` with 1 retry |
| Unrecoverable timeout | VR-007 | both risk routes timed out; no third approved route exists, so retrying would repeat an attempt — escalated for want of evidence rather than proceeding without a risk rating |
| Empty results | VR-011 | default query, then corrected query, then the alternate route; a fourth attempt was refused by the retry budget and logged |
| Outdated evidence | VR-011 | the only risk record is 304 days old against a 180-day limit, so it is not current — `ESCALATE`, citing the stale record |
| Conflicting sources | VR-008 | risk database says low, the current approved assessment says high and fails; both are priority-2 and current, so the fact is contested and the request escalates |
| Prompt injection | VR-009 | tier-3 instruction quarantined, all tools still called, approval rests on `RISK-006` and `SEC-005`, attempt recorded in the decision |
| Duplicate submission | VR-010 ×2 | second run stopped at the memory guard before any tool call; one entry in the decision log |
| Missing information | VR-004, VR-005 | stopped in 2 steps, naming the absent field |
| Strictest action | VR-003, EX-008 | rejection outranks escalation when both apply |

## Observed failures during development

Four defects the tests and batch caught, all fixed:

1. **Duplicate submissions were miscounted.** The approval tool combined an
   in-process counter with a persisted flag, so the second submission reported
   call number 3. Replaced with a single persisted `submissions` counter.
2. **Two runs of one request could share a `run_id`.** The identifier was a
   second-resolution timestamp, so the duplicate VR-010 run overwrote the log and
   database row of the first. A random suffix now distinguishes them.
3. **SQLite broke under the web server.** The connection was created on one thread
   and used from the request thread pool. One connection now serves all threads
   under a lock, which also makes the duplicate check and its insert atomic.
4. **Stored and returned decisions disagreed on `steps_used`.** The count was
   taken before the submission step and patched afterwards, so the database held
   one value and the caller another. The submission step is now counted up front.

No supplied case currently produces a wrong decision. The residual risk is not in
the loop but in interpretation, which is what the next section is about.

## Limitations

- **One documented interpretation of rule precedence.** The policy lists cost
  before vendor status, so read literally, an over-limit cost on a prohibited
  vendor would escalate. I evaluate all applicable rules and take the strictest
  action, because a prohibited vendor cannot be made acceptable by escalation.
  The choice is in `policy.select_action`, tested in `test_policy_engine.py`, and
  configurable by changing one severity table — but it is a choice, and a reviewer
  may want the literal reading instead.
- **"Vendor-risk record" is read narrowly.** Only the internal risk database
  satisfies that requirement; a security assessment carrying a risk rating does
  not substitute for it. This is why VR-007 escalates despite having a current
  passing assessment. If the intent was broader, VR-007 would become `APPROVE`.
- **Conflict detection covers the facts the rules read** — vendor status and risk
  rating. Two sources disagreeing on a field no rule consults would go unnoticed.
- **Injection detection is a reporting aid, not the defence.** The real protection
  is that retrieved text is never treated as instructions and tier-3 material is
  excluded from fact resolution. A novel phrasing would evade the patterns in
  `sanitize.py` and still be unable to change an outcome, but it would not be
  flagged.
- **Backends are mock and latency is simulated.** Timeouts come from
  `tool_scenarios.json`, not from a network. The runtime does enforce a real
  wall-clock budget, but no test exercises genuine network failure modes such as
  a half-open connection or a partial response.
- **The LLM planner is untested against a live model here.** Its degradation path
  is tested; its planning quality is not, and the reported results all come from
  the deterministic planner.
- **No concurrency test.** The store is lock-guarded and the decision key is
  unique, so a race should resolve to one recorded action, but that is argued
  rather than demonstrated.

## Recommended improvements

1. **Ask the policy owner to rank the rules explicitly.** The precedence question
   above is a policy decision, not an engineering one, and it should be written
   into the document rather than inferred by me.
2. **Make `evaluation_date` an input per request.** It is fixed at 2026-08-01 to
   match the supplied data; real use needs the date of assessment, and every
   currency calculation already flows from that one setting.
3. **Add a conflict rule for every material fact.** Generalise the check from two
   named facts to any field two priority-2 sources both assert.
4. **Escalate with a work item, not just a verdict.** An escalation should say what
   a human needs to obtain — "a current risk record for FailWare" — since the
   agent already knows exactly which evidence was missing.
5. **Track per-vendor failure rates.** Repeated timeouts on one vendor are an
   operational signal that currently only appears in individual logs.
6. **Property-based tests for the rule table.** Generate requests across the field
   space and assert invariants: never approve without a current tier-2 record,
   never approve restricted data, never record twice.
7. **Run the LLM planner against the golden set** to measure how often a model in
   the planner slot reaches the same decisions as the deterministic planner, which
   would quantify what the model adds and what it costs.
