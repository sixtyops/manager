# CPE session-proof investigation

**Key points**

Cause (b) prevents the trusted proof from reaching CPE authentication.
The poller works without the scheduler when the proof is absent.

**Detail**

The starting head of [#654](https://github.com/sixtyops/manager/pull/654)
was `a0b318ad2d7119a3ec8cd5cbf073b2393da67994`.
The comparison main was `bb47a3a06bc76b74fb56f0d13a92ba0d3bda9e53`.
The trusted proof came from that main commit without changes.

The proof wraps `TachyonClient.login`, `session_valid`, and
`get_connected_cpes`. It also replaces `NetworkPoller._check_cpe_auth` with
`check_cpe_auth(self, cpe_ip, username, password)`.
The PR calls that replacement with `force_fresh_login`, including when false.
The call raises this error before the original auth method runs:

```text
TypeError: _instrument.<locals>.check_cpe_auth() got an unexpected keyword argument 'force_fresh_login'
```

The AP's CPE probe gather uses `return_exceptions=True` and ignores the returned
exceptions. AP reads and link uptime continue. CPE auth never runs in the proof.
This explains every zero counter without a daily-marker or in-flight failure.

## Offline comparison

**Key points**

The same fake devices and trusted counting harness reproduce the difference.
No scheduler or hardware runs.

**Detail**

`tests/test_cpe_poller_proof.py` uses the real poller and Tachyon driver with fake
network methods and an in-memory database. It runs two AP poll cycles. Its
`TRUSTED_SESSION_PROOF` option loads the unchanged proof from main when testing
an archived commit.

| Commit | Trusted hooks | Actual CPE logins | Counted CPE logins | Counted reuse |
| --- | --- | --- | --- | --- |
| main `bb47a3a` | absent | 1 | n/a | n/a |
| main `bb47a3a` | present | 1 | 1 | 1 |
| PR `a0b318a` | absent | 1 | n/a | n/a |
| PR `a0b318a` | present | 0 | 0 | 0 |
| fixed PR | present | 1 | 1 | 1 |

The regression also runs the poller alone across daily and maintenance-window
boundaries. A daily login does not satisfy a later window. A window login
satisfies the rest of that day's daily checks. Each next day and next window
requires a fresh login.

## Fix and remaining bench gate

**Key points**

Keep the trusted three-argument hook. Keep the new session lifetime rules.
Update proof requirements in a separate PR before bench sign-off.

**Detail**

The period check stores its fresh-login requirement under the existing per-CPE
lock. It calls `_check_cpe_auth` with three arguments. The auth method reads the
requirement. A `finally` block clears it, including on cancellation or failure.
Token validation, unreachable handling, daily markers, and window admission
keep their existing behavior.

The earlier proof edits and their tests are restored to main in this PR. No
proof behavior changes remain in the PR diff. The trusted proof still ages a
cache entry and demands renewal after 600 seconds. That requirement conflicts
with [#634](https://github.com/sixtyops/manager/issues/634), which removes the
age cap. A separate proof PR must replace that trigger with an explicit cache
removal or token invalidation and verify renewal and reuse. Do not restore the
product age cap to satisfy the old proof. Exact-head bench proof remains with
the lead; this worker ran offline tests only.

## Validation commands

**Key points**

Exclude the integration directory before collection. Run both host time zones.

**Detail**

```bash
TZ=UTC PYTEST_ADDOPTS='--ignore=tests/integration' /private/tmp/pr558-venv/bin/python -m pytest -v -m 'not integration'
TZ=America/Chicago PYTEST_ADDOPTS='--ignore=tests/integration' /private/tmp/pr558-venv/bin/python -m pytest -v -m 'not integration'
```

Changelog validation reports `Invalid changelog fragment name: 337b.changed.md`.
That fragment already exists on main. This PR's `634.fixed.md` name is valid.

Both full runs passed: 3,257 passed and 27 skipped in each time zone.
UTC took 126.01 seconds. America/Chicago took 125.65 seconds.
No integration tests were collected or run.
