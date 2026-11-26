# How it is evaluated

The model and the prompt in this service were chosen in experiments. They are kept in the [evaluation lab](https://github.com/ArvinRastegar/i-adopt-llm-based-service). This page sums up what was measured and what came out.

## What is measured

There are two steps.

- **Taking a sentence apart.** The benchmark has 102 variables from the I-ADOPT corpus, each with a reference answer. The score is F1 over six fields, from 0 to 1.
- **Linking the parts.** The score is how often the first choice is the right entry of the vocabulary.

## What came out

**Good examples in the prompt help the most.** The score doubles when the prompt shows 20 worked examples instead of none ([results](https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/main/iadopt-lab/few-shot-selection/shot-pool-30-results.md)).

| Examples in the prompt | 0 | 5 | 10 | 20 | 30 |
|---|---|---|---|---|---|
| Score | 0.22 | 0.37 | 0.38 | 0.45 | 0.46 |

Ten repetitions with 20, 25 and 30 examples gave the same level, so more than 20 examples bring nothing more.

**Which examples you pick matters.** The 25 examples in the prompt were chosen by testing 600 random sets. On variables that were not used for the choice they did better than random sets ([report](https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/main/iadopt-lab/few-shot-selection/example_selection/output/example-selection-report.md)).

**Different vocabularies need different methods.** A chat model and a small reranker were compared for linking. Each was better on some vocabularies ([linking experiments](https://github.com/ArvinRastegar/i-adopt-llm-based-service/tree/main/iadopt-linking)).

## How a release is checked

- Every push runs more than 800 automated tests, the frontend build and the container builds.
- Before a release the service runs on all 102 reference variables and is scored. It has to reach a fixed score. The examples of the prompt are part of these variables, so this check is there to catch regressions.
- A test makes sure that the prompt is exactly the one that was tested in the lab.

## Limits

- The benchmark is one corpus of 102 English definitions, and parts of it were used to tune the prompt.
- The prompt in use today has a slightly different layout than the one in the table above.
- The numbers come from experiments, not from users of the service.
