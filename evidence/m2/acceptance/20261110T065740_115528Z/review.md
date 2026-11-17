# M2: Limited acceptance on new cases after the freeze

Translated from Traditional Chinese on 2026-11-16. The original is in commit ac348c0.

Local date: 2026-11-09 (America/Los_Angeles). The run folder uses UTC. The M2 code, prompts, tools, checkers, `.env`, and budgets were frozen first, and then new cases were selected from three different repositories. These targets did not appear in the project's cases or change history before the freeze. That does not tell us whether the model saw these public issues during pretraining.

The [acceptance protocol](protocol.json), [case criteria](cases.json), and [independent GitHub selection record](selection.json) were all saved and fixed before the model ran. The [raw results](report.json) keep the real API output, and the [assistant source check](assistant-review.json) records its judgments separately. User review remains `pending`.

## Decision for this round

**This round's limited functional acceptance passed, and M2 can move to wrap-up. The code stays frozen.** All three cases have an accepted final answer, and there are no blocking issues under the predefined criteria. This conclusion applies to the scope of this round. It does not guarantee production accuracy or that every branch was tested.

| New case | Result | Main checks |
| --- | --- | --- |
| [HTTPX #3602](https://github.com/encode/httpx/issues/3602) | `completed`; 6 model requests, 5 tool calls | Correctly separated the fact that the pre-release exists from the open question of whether the API removal was intentional. Did not falsely claim that `Transport` is a drop-in replacement. Recommended `release`, and explained the scope of its two successful empty searches. |
| [pytest #14560](https://github.com/pytest-dev/pytest/issues/14560) | `partial`; 5 model requests, 4 tool calls | Kept the author's and the maintainer's different accounts apart, asked for a minimal reproducer, and carried forward the explicit ID suggestion. Recommended `topic: parametrize` and did not repeat the existing needs-information label. |
| [Ruff #29177](https://github.com/astral-sh/ruff/issues/29177) | `completed`; 6 model requests, 5 tool calls | Recognized a lint rule request. After reading the body of #25014, proposed a tentative `possible_duplicate` and recommended `rule`. Did not falsely claim to have read the docs or verified a release version. |

The run used `gpt-5.6` / `medium`, once per case. Each case kept the limits of 8 model requests, 15 tool calls, 90 seconds, 12,000 tokens of local input estimate, 2,400 output tokens, 20 candidates, and 5 candidate bodies. In total: **17 model requests, 14 tool calls, 81,782 reported tokens, and 72.92 seconds**. All 17 sent inputs fit the budget. The largest estimate was 9,634, and the largest total including the actual reserve was 12,000. There were no timeouts, API errors, exhausted output, or missing final answers.

## Why the pytest partial result is accepted

The protocol, fixed in advance, already allowed a bounded partial result that has a final answer and honestly discloses its coverage. pytest read the first 10 of 11 comments, omitted the retrieved S5 and S11, and read none of the five candidates. When it was about to read candidates, the minimum reserve of 2,400 could not be met, so the program finished with the evidence it had.

It did not claim that it read every comment, that there are no related issues, or that it completed a duplicate comparison. The two omitted excerpts are still kept in Sources. The original automatic check `minimum_similar_issue_checks_completed=false` is kept in full and was not changed to a pass. This is a coverage limit, and under the original protocol it does not by itself block delivery for this round.

## Remaining limits

- Most of HTTPX's `missing_information` items are unknowns of the investigation, such as API intent, the migration path, and later releases. They would be clearer in the limitations section. It did not recommend `need-info` because of them.
- The Ruff draft infers with "appears to be covered" that the existing rule may cover the case, but its final recommendation to use that rule directly is too strong. The actual behavior and version should be checked first. Its limitations state that the docs, code, and release contents were not verified. The PEP 810 name in the summary can be found in S5, but the summary itself cites only S1 and S2, so the fine-grained citations could be more precise.
- This round did not look up templates or a README, and no answer was rejected and sent to repair. Repair is still supported only by the offline regression from before the freeze, so we cannot say this round verified repair live.
- The full model replay bytes were not saved in the original report, so this review does not claim to have independently checked all replay content. Three purposively selected historical cases are also not enough to estimate accuracy in general use.

Under the predefined criteria, these are recorded as non-blocking observations or unexercised items. This round did not change code, prompts, or scoring criteria because of the results, and did not pay for reruns to pick a better answer.

## Freeze and reproducibility

All 166 existing files and `.env` were checked and found unchanged. The actual bodies of the three targets also matched their content at selection time. The SHA-256 hashes are:

- Cases: `5d3946523219d059b87ce421f63445dffc7ed092a9fbf3e11270da74460163e2`
- Selection: `ae70754609ffc7c0496e918d344b18a44046908681e62159bab372df19f99126`
- Protocol: `bd84dc70073dee9ae3ee004a720b9f4545c9f9c4207097be9b35d0148048a0fd`
- Raw results: `ab6051135015cb1d456ae3b2c419c787a30947206975a69c83c8b47b9dff25ca`

The run reused the existing `scripts.acceptance.main()` and chose the new case and selection files only at the execution boundary. The original three cases and the runner code were not overwritten. The acceptance criteria and selection data did not enter AgentRun. The model received only what the normal flow provides: the target URL, the settings, and real tool results.

This is the command used for the run. It spends real model quota. The round is complete, so there is no need to run it again to get the same result.

```bash
uv run --locked python -c 'import asyncio; from pathlib import Path; import scripts.acceptance as acceptance; acceptance.CASES_PATH=Path("evaluation/m2_frozen_acceptance_cases.json").resolve(); acceptance.SELECTION_PATH=Path("evaluation/m2_frozen_acceptance_selection.json").resolve(); asyncio.run(acceptance.main())'
```

The command's two input files were removed from `evaluation/` on 2026-11-15. Byte-identical copies are `cases.json` and `selection.json` in this folder, and the originals are in commit `619ed99`.

The pre-freeze record of 262 offline tests and zero Pyright errors still applies. This round did not change code and does not claim to have rerun those checks.

## Exercise

Compare pytest's `partial`, the `false` from the original automatic coverage check, and this round's protocol rule for bounded partial delivery. Explain why "run status", "lookup coverage", and "whether the delivery meets this round's criteria" need to be recorded separately.

## Next step

Keep this version as the baseline for the next milestone, and do not fine-tune for these cases. These three are now seen acceptance cases. If the code is later adjusted based on them, they can no longer be called fresh validation data.

## Raw drafts

The text below comes directly from the model output accepted in this round, with no human edits.

<details>
<summary>httpx_prerelease_transport</summary>

Thanks for documenting the upgrade path and exact exception. This showed that moving from 0.28.1 to 1.0.dev1 removed the top-level `httpx.BaseTransport` export. On July 14, 2025, lovelydinosaur confirmed that the 1.0 pre-release was intentionally available on PyPI and said more information would follow, but the discussion captured here did not clarify whether this particular API removal was intentional or whether `Transport` was a compatible replacement. Anyone revisiting this should consult the current 1.0 API and migration documentation rather than assuming the suggested `Transport` name is a drop-in replacement; the intended replacement and migration path should be documented explicitly.

</details>

<details>
<summary>pytest_custom_object_ids</summary>

Thanks for the additional explanation. The failure occurred while pytest was deriving an automatic ID for the parameter value: it queried `__name__`, and the instance’s custom attribute lookup raised `KeyError`. As discussed above, missing attributes should not escape from `__getattr__` as `KeyError`; the instance can instead raise `AttributeError` for an unknown attribute.

For an object that intentionally keeps that behavior, the supported workaround is to provide the test ID explicitly rather than modify pytest internals:

```python
pytest.param(MyBroken(), id="broken")
```

The report did not include a minimal reproducer or the exact pytest versions before and after the reported change, so it was not possible from this discussion to establish a pytest regression. If this is revisited, please include the smallest custom class and parametrized test that demonstrate it, along with the last working and first failing pytest versions.

</details>

<details>
<summary>ruff_lazy_import_rule</summary>

Thanks for the concise example. This case appears to be covered by the existing `lazy-import-immediately-resolved` rule linked above. The earlier proposal in #25014 explicitly included lazy-imported names used at unconditional module scope, which matches the assignment shown here. The recommendation is therefore to use that existing rule rather than add a separate diagnostic.

</details>
