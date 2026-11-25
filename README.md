# TranslationCheck — Translation Quality Assessment by GenLayer Consensus

A standalone GenLayer Intelligent Contract that assesses translation
quality: anyone registers a translation job (source text, source
language, target language, translation text, and an optional reference
as a URL and/or free text), and anyone can trigger assessment. The
leader fetches the reference page (when a URL is given) and an LLM
assesses the translation, returning strict JSON with a verdict
(`accurate` / `needs_revision` / `inaccurate`), scores 0–100 for
accuracy, fluency and completeness, plus a short rationale. The total
score is the deterministic sum of the three scores.

**Deployed on StudioNet (fee-charging Studio v0.123 / Consensus v0.6):**
`0xf2726451908235b370c1A659b005822bDA5396cf` — see
[explorer](https://explorer-studio.genlayer.com/address/0xf2726451908235b370c1A659b005822bDA5396cf)

Deploy transaction:
`0x76acfb056bad3d2ffde34b7ab9eea1566a77d0ef0e2a3c3f432985c2fa459c80`
(ACCEPTED; 3/5 validators agreed, 2 idle).

## How it works

1. **Anyone registers a translation job** (`register_job`): source
   text, source language, target language, translation text, and an
   optional reference (URL and/or free text). No reference is required.
2. **Anyone triggers assessment** (`assess`). The leader fetches the
   reference page (if a URL was given) and an LLM assesses the
   translation against the source text and the reference.
3. **Validators independently re-fetch the same reference and re-run
   the same assessment.** The custom comparative validator accepts the
   assessment only when the verdict matches exactly AND the total score
   agrees within +/-10 points — no single leader can set a verdict
   alone. Rationale wording and individual scores may differ.
4. **The contract owner can close a job** (`close_job`), from OPEN or
   ASSESSED state. **The submitter can withdraw their own OPEN job**
   (`withdraw_job`).

No money moves in this contract at all: no GEN transfers, no payments, no
payable methods. It is a pure assessment primitive — the same contract
serves any translation because the languages, the texts, and the
reference are per-job inputs, not hard-coded prompts.

## State machine

```
OPEN ──assess (consensus verdict + scores)──▶ ASSESSED
  │                                              │
  └──────close_job / withdraw_job────────────────┘──▶ CLOSED
```

(withdraw_job is submitter-only and OPEN-only; close_job is
owner-only and works from OPEN or ASSESSED.)

## Why this is a reusable primitive, not a thin LLM wrapper

- The job, the assessment, and the state transition are all
  on-chain and gated by independent validator re-verification of the
  same reference — the LLM never decides alone.
- Equivalence is comparative on the decision fields (exact verdict
  match + total score within +/-10); free-text rationales and
  individual scores may differ.
- The verdict is one of three fixed labels (`accurate`,
  `needs_revision`, `inaccurate`); the contract validates the LLM
  output against that set, validates each score range (0–100),
  computes the total deterministically (sum of the three scores), and
  enforces verdict/score consistency (accurate requires every score
  >= 80; inaccurate requires total <= 120; needs_revision must sit
  between) — inconsistent LLM output is an LLM error, not a stored
  assessment.
- Error classification follows the GenLayer convention
  (`[EXPECTED]` / `[EXTERNAL]` / `[TRANSIENT]` / `[LLM_ERROR]`) so
  validators compare failure paths correctly.
- The contract is generic: one deployment serves any translation job.

## Contract methods

| Method | Type | Description |
|---|---|---|
| `register_job(source_text, source_lang, target_lang, translation_text, reference_url, reference_text)` | write | Register a job (anyone), returns `job_N` |
| `assess(job_id)` | write | Consensus assessment, returns verdict + 3 scores + total_score + rationale |
| `close_job(job_id)` | write | Owner closes a job |
| `withdraw_job(job_id)` | write | Submitter withdraws their own OPEN job |
| `get_job(job_id)` | view | Full job record |
| `list_jobs()` | view | All jobs |
| `get_job_count()` | view | Number of jobs |

## Tests

```bash
# Direct mode (fast, in-memory; leader only, mocked web/LLM)
.venv/bin/python -m pytest tests/direct/ -v

# Integration (GLSim, full leader + validator consensus)
.venv/bin/glsim --port 4000 --validators 5
.venv/bin/gltest tests/integration/ -v -s --network localnet
```

- Direct: 7 tests — registration + validation, assess accurate,
  all three verdicts, LLM validation, close (owner-only), close after
  assessment + withdraw.
- Integration: 4 tests — full register → assess needs_revision flow,
  assess accurate flow, close owner-only + assess of a closed job
  fails, assess of an unregistered job fails.

## Toolchain note — StudioNet is Consensus v0.6 / Studio v0.123

StudioNet is on the v0.6 line (fee-charging). Fee-less deploys are
rejected with `FeeValueMustBeNonZero`, and the old runner hash
(`py-genlayer:1jb45aa8...`) is gone from GenVM v0.6.0. This project
uses:

- Runner: `py-genlayer:kzybg95x1gn21dw72m7hyg0maaap04n4nx55j9yt6fkdqxtz6bz0`
- genlayer-py 0.19.0 / genlayer-test 0.30.0 (in `.venv/`, see
  `requirements.txt`), CLI 0.40.0.
- v0.4 SDK API changes applied to the contract: `import genlayer as gl`
  plus `from genlayer.storage import DynArray, TreeMap, allow`;
  `@allow` replaces `@allow_storage`; `gl.contract.Contract` replaces
  `gl.Contract`; `gl.vm.run_nondet` replaces `run_nondet_unsafe`;
  `gl.vm.UserError` carries `.data`, not `.message`.
- `genvm-lint` 0.11.0 (latest on PyPI) cannot validate the new runner
  (its SDK loader looks for a `.tar` runner that no longer exists):
  AST lint passes 3/3, SDK validation errors with E101. The direct +
  integration tests running on the real GenVM v0.6.0 runner are the
  effective validation until a newer linter ships. Note: on this VM the
  user-site `genvm-lint` lost its module path after a VM restart; run
  it as `PYTHONPATH=$HOME/.local/lib/python3.12/site-packages python3
  ~/.local/bin/genvm-lint check contracts/translationcheck.py`.
  The contract file is pure ASCII, verified with `file` before testing.
- Test-harness workarounds for this toolchain pair (documented in the
  test files): direct + GLSim LLM mocks must double-encode the JSON
  payload (the mock layer auto-parses JSON once, the SDK parses again);
  the integration conftest patches `make_calldata_object` to the legacy
  `{"method": ...}` shape because the GLSim bundled with genlayer-test
  0.30.0 rejects the `{"": ...}` shape with "No method in calldata".

## Deploy (StudioNet, Consensus v0.6 — fee-charging, 0 GEN still works)

Studio v0.123 charges protocol fees, but a 0-GEN account can still
deploy: Studio credits the fee shortfall before debiting it, and unused
budget is refunded at finalization. Every deploy must carry a fee
distribution. The deploy below used the distribution from StudioNet's
own `sim_getFeeConfig` defaults, with `executionBudgetPerRound` raised
to 10x the floor: the floor budget (78,852,000,000,000) fails the deploy
(leader execution runs out of budget — the deploy consumes
78,999,000,000,000), while 788,520,000,000,000 succeeds.

```bash
genlayer network set studionet
printf '%s\n' "$(cat ~/workspace/genlayer/.builder-account-password)" | genlayer deploy \
  --contract contracts/translationcheck.py \
  --fees '{"distribution":{"leaderTimeunitsAllocation":"100","validatorTimeunitsAllocation":"200","appealRounds":"0","executionBudgetPerRound":"788520000000000","executionConsumed":"0","totalMessageFees":"0","rotations":["0"],"maxPriceGenPerTimeUnit":"2","storageFeeMaxGasPrice":"300000000","receiptFeeMaxGasPrice":"300000000"}}'
```

> Note: pipe the password with a trailing newline (`printf '%s\n'`)
> — piping the file with `cat` alone (no newline) makes the CLI reject
> the password as invalid. The deploy was done with the CLI 0.40.0
> command shown here.

`fee-profile.json` was generated by `gltest --fee-profile` against local
GLSim, but local GLSim has fees disabled so it measures all zeros; its
`deploy` entry was then set to the values that succeeded on StudioNet
(see above). Method entries remain the local (zero) measurements.

## License

MIT — see [LICENSE](LICENSE).
