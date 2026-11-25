# Evaluation

- **What runs:** `Qwen3.8-27B` on PSNC with a 25-example prompt. No public result file exists for exactly this prompt.
- **Nearest public number:** Close F1 0.4890 (SD 0.0252 over 15 repetitions) on 24 variables, with the same 25 examples in an earlier layout, 12 September 2026.
- **Links:** 206 of 284 right as the lab scored them, 199 if the model's own "none of these" counts, against 194 for a small reranker. The
  difference cannot be told from zero.
- **Release gate:** by hand, since 18 November 2026. No result of it is published.
- **Limits:** [what the numbers do not show](#what-the-numbers-do-not-show).

The measurements are in the separate [lab repository](https://github.com/ArvinRastegar/i-adopt-llm-based-service) and use the
I-ADOPT corpus v2.0.1: 102 variable definitions, each with a reference. Sources are in the last column of each table. Figures whose result
file is not public are [left out](#left-out).

## How it is scored

The service has three model steps. Only two were ever measured: the second call that writes a label and a comment was not.

- **Decomposition**: from a definition to six fields (property, object of interest, matrix, context object, statistical modifier,
  constraints). The score is *micro Close F1*. A field counts as right when its text is at least 0.8 similar to the reference by
  embedding, with partial credit for systems and constraints. A wrong field counts as something wrongly said, not as something missed, which raises F1
  compared with counting both ([rules][top-configurations]).
- **Linking**: from a label to an entry in Wikidata, QUDT or OM. The score is the share of *slots* (one label of one variable) whose first choice is
  exactly the reference entry. A slot is *reachable* when the reference is in one of the three sources.

The official runs ask an invalid answer again, the example studies count it as empty, so rows of different studies are **not comparable**.

## Numbers: decomposition

| What | Result | Measured on | Runs | Run date | File, commit |
|---|---|---|---|---|---|
| Best of the 298 configurations that completed, of 396 planned: `z-ai/glm-5.2`, 5 examples | Close F1 0.4206 | 97 variables | 1 each | committed 24 Sep 2026 | [`configuration-results.csv`][csv], `c537bb1`; [campaign log][campaign] |
| `Qwen3.8-27B` on PSNC, 5 examples | Close F1 0.3912, 6th of the 298 | the same 97 | 1 | committed 24 Sep 2026 | the same |
| `Qwen3.8-27B` with 0, 5, 20 and 30 examples | Close F1 0.224, 0.373, 0.451, 0.456 | 72 variables | 1 each | 12 Sep 2026 | [`shot-pool-30-results.md`][shots], `7405fb0` |
| The same with 20, 25 and 30 examples, repeated | means 0.456, 0.451, 0.468; SD 0.016, 0.018, 0.017; no difference is significant | the same 72 | 10 each | 12 Sep 2026 | the same |
| The 25 examples this service shows, earlier layout | Close F1 0.4890 (SD 0.0252), the best of the seven sets compared | 24 variables | 15 | 12 Sep 2026 | [`example-selection-report.md`][selection], `7405fb0` |
| The same against three random sets of 25, pooled, and against the best of them | +0.0367 (p = 0.0002) and +0.0133 (p = 0.156, not significant) | the same 24 | 15 | 12 Sep 2026 | the same |
| The same, one repetition, variable by variable | 3 of 24 exactly right, 7 of 24 zero | the same 24 | 1 | 12 Sep 2026 | [`best-configuration-session.md`][best], `7405fb0` |

The 97 are all 102 without the five that served as examples, the 72 all without a pool of 30. The 24 are one part of a fixed split, 40 to
choose examples from, 38 to search on, 24 to confirm on. SD and p describe repeated runs on the same variables, not how the result would change with
other variables. The first study did not touch the 24 before its end; a side test of 18 November and a later study
([`guided/`][guided], `17abce5`) scored them again.

## Numbers: linking

| What | Right first choice | Measured on | Run date | File, commit |
|---|---|---|---|---|
| The lab's baseline, `Qwen3-Reranker-0.6B` | 194 of 294 slots (0.660); of the 284 reachable ones 0.683 | all slots whose reference is a controlled-vocabulary entry | 14 Oct 2026 | [report][link-baseline], `4af382e` |
| `Qwen3.8-27B` on the same candidates, forced choice | 206 of 284 (0.725): +0.042, 95 % interval -0.073 to +0.172 | the same 284 | 15 Oct 2026 | [report][link-psnc], `a077ebe` |
| The same, other candidate orders | 202 and 201 of 284 | the same 284 | 15 Oct 2026 | the same |
| The same, honouring the model's "none of these" (17 times, 7 of them counted right above) | 199 of 284 | the same 284 | 15 Oct 2026 | [`outcomes.csv`][link-psnc-csv] |
| By vocabulary (exploratory) | Wikidata 138 to 169 of 218 (+0.142, interval +0.008 to +0.300); QUDT 51 to 32 of 61 (-0.311, interval -0.522 to -0.138); OM 5 to 5 of 5 | the same 284 | 15 Oct 2026 | the same report |

The candidates were retrieved from the live services on 14 October and used again on 15 October. The interval resamples whole terms, because one term
is asked in many slots. Before 18 October 2026 (`3fe4dc1`) the service linked to Wikidata only, with another reranker (`bge-reranker-v2-m3`), which the lab did not
measure; since then it asks the chat model, honours "none of these", and orders the candidates by a seed of its own. The table describes the
question, not exactly what runs.

This recomputes the first two figures of the chat model from a public file:

```bash
curl -sL https://raw.githubusercontent.com/ArvinRastegar/i-adopt-llm-based-service/a077ebe0877afa3b1d5530321a0a02651f15664e/iadopt-linking/results/psnc-qwen3.8-27b-20261015/outcomes.csv | python3 -c "
import csv, sys
rows = [r for r in csv.DictReader(sys.stdin) if r['reachable'] == '1']
print(len(rows), sum(r['qwen3_correct'] == '1' for r in rows), sum(r['llm_correct'] == '1' for r in rows))"
```

It prints `284 194 206`.

## The release gate

[`quality_gate.py`](../backend/tools/quality_gate.py) (added 18 November 2026, `4cb9b19`; its own logic is tested in
[`test_quality_gate.py`](../backend/tests/test_quality_gate.py)) is meant to be run by hand before `dev` is merged into `prod`. Nothing enforces it. CI
has no key for a model provider and does not run it; CI runs the backend tests, the frontend's unit tests, type check and build, and the container
builds ([`ci.yml`](../.github/workflows/ci.yml)).

1. It asks the backend container what it runs, and stops when that code and data differ from the working tree, committed or not, or when it is not the
   measured configuration (`--any-backend` skips the second check).
2. It starts a second Python process in the container and sends all 102 definitions through the service's own pipeline, not through the streamed route
   the browser uses, and scores the answers with the lab's scorer. An answer that is missing or that the lab cannot use counts as empty.
3. It passes when one run reaches a micro Close F1 of 0.60 on all 102. Below that it fails, or says that it cannot decide when the provider left
   definitions unanswered.

The prompt shows 25 of the 102 with their answers, so the score is a floor against regressions and not the accuracy on a new variable; the gate prints the two
groups apart. It is one run; the spread on all 102 was not measured. It scores the decomposition, not the links. No gate report is published,
so no gate result is quoted here.

## Three failures

**1. The best model lost half its runs.** In the last campaign `z-ai/glm-5.2` reached the best score, but 50 of its tasks ended at the 8,000-token output
ceiling with reasoning on, and only 18 of its 36 configurations completed. The headline 0.4206 is the best of what was left
([campaign log][campaign], campaign `79b5ec5f`, `7405fb0`, 14 Sep 2026). In the same campaign the provider's key reached its spend limit at 37.5 %, every model got an
HTTP 403, and the retry loop lost 17 tasks before it was stopped. The log names the cause and the resumption.

**2. One run that showed a peak.** A single run over 0 to 30 examples seemed to peak at 25 (0.4672) and fall at 30 (0.4603), in a score that leaves out answers
that failed validation. Ten repetitions gave 0.4607 and 0.4768, the other way round. A single measurement on these 72 variables resolves about 0.052, more than both
gaps ([`shot-pool-30-results.md`][shots], `7405fb0`, 12 Sep 2026). The example-selection study repeated every comparison 15 times. The linking
headline and the release gate are single passes.

**3. Chat model against reranker.** The reranker picked the wrong entry for `water` in all 12 slots (ten times a QUDT quantity such as "Mass Concentration of Water", twice
a river-like Wikidata item). The chat model got all 12 right and gained 41 slots in total, but lost 29: 21 in QUDT, where it often picks the Wikidata or OM entry of the
same concept, and 8 in Wikidata, 6 of them `person`. The net 12 slots are inside the interval above. The reference itself uses two Wikidata items for `person`, 14 and 12
times. Whether an equivalent entry should count as right is open in the lab ([`outcomes.csv`][link-psnc-csv], `a077ebe`, 15 Oct 2026). The service asks the
chat model since 18 October 2026; these numbers do not show that it is the better choice.

A trap seen in time, not a failure: the first plan for choosing 25 examples scored each choice on the other 77 variables, so a choice picked its own test. On the per-variable scores of one
run, moving the 25 hardest variables out of the test lifts Close F1 from 0.4672 to 0.6348 without any change to the model. The study uses one fixed split instead, and its
chosen set still scored 0.5218 on the variables that chose it against 0.4890 on the 24 that did not ([DS-1][split-design], [report][selection], `7405fb0`).

## What the numbers do not show

- **The exact prompt that runs has no public result file.** The prompt shows the 25 examples in a layout chosen by a side test on 18 November 2026, whose records are not in git.
  Its figures stand in [`PROVENANCE.md`](../backend/tests/fixtures/PROVENANCE.md); nobody outside can check them.
- **Most unseen variables were looked at.** Of the 77 variables the prompt does not show, 62 helped to choose the examples or the layout (38 of the search, 24 of the
  confirmation); 15 scored neither. Recomputed from the lab's split code and corpus (`4c56487`) on 25 November 2026. The model, prompt and temperature came from
  results on 97 of the 102.
- **One corpus.** 102 English definitions, four domains. The reference has its own inconsistencies, as `person` shows.
- **Linking used the corpus's own labels**, not labels a model wrote. The corpus's QUDT labels are close to QUDT's own names, so that result will not carry over.
- **Records are partly missing.** The raw answers of the example studies and of the main campaigns are not in git (the main ones are in a local database). The three
  linking runs recorded an uncommitted tree; the hash of their evidence files matches the published commit for the baseline.
- **No user study.** The example study recorded 3.35 s mean latency, 3,736 prompt tokens and 1.6 % invalid answers per call on PSNC; the deployed service was not measured.

## Left out

The score of the side test that chose the layout, and the score of the first gate run. Both stand in code comments or notes of this repository, and neither has a public result file.

[csv]: https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/c537bb112d63c4e59268cdc5d4172fe540212714/configuration-results.csv
[campaign]: https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/7405fb047e541a7c0e79670cf5f8196fe2ab5e9e/iadopt-lab/docs/campaign-log.md
[top-configurations]: https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/9943def76edf9779febdbd3a13711d337dbedacf/iadopt-lab/docs/results-top-configurations.md
[shots]: https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/7405fb047e541a7c0e79670cf5f8196fe2ab5e9e/iadopt-lab/few-shot-selection/shot-pool-30-results.md
[selection]: https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/7405fb047e541a7c0e79670cf5f8196fe2ab5e9e/iadopt-lab/few-shot-selection/example_selection/output/example-selection-report.md
[best]: https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/7405fb047e541a7c0e79670cf5f8196fe2ab5e9e/iadopt-lab/few-shot-selection/example_selection/best-configuration-session.md
[split-design]: https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/7405fb047e541a7c0e79670cf5f8196fe2ab5e9e/iadopt-lab/few-shot-selection/example_selection/README.md
[guided]: https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/17abce5b70f5df32efc63d88ec742d2d63a83ff7/iadopt-lab/few-shot-selection/guided/groups.py
[link-baseline]: https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/4af382e2128c0559cc5497bcc8a2d660bb0ab483/iadopt-linking/results/baseline-20261014/report.md
[link-psnc]: https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/a077ebe0877afa3b1d5530321a0a02651f15664e/iadopt-linking/results/psnc-qwen3.8-27b-20261015/report.md
[link-psnc-csv]: https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/a077ebe0877afa3b1d5530321a0a02651f15664e/iadopt-linking/results/psnc-qwen3.8-27b-20261015/outcomes.csv
