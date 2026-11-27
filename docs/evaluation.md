# How it is evaluated

The model and the prompt in this service were chosen in experiments. They are kept in the [evaluation lab](https://github.com/ArvinRastegar/i-adopt-llm-based-service). This page sums up what was measured and what came out.

## What is measured

There are two steps.

- **Taking a sentence apart.** The benchmark has 102 variables from the I-ADOPT corpus, each with a reference answer. The score is F1 over six fields, from 0 to 1.
- **Linking the parts.** The score is how often the first choice is the right entry of the vocabulary.

## What came out

**Similar examples, a matrix hint and three answers work best.** This is the setup the service runs ([the lab's best pipeline](https://github.com/ArvinRastegar/i-adopt-llm-based-service#best-pipeline-so-far)). Each row adds one step:

| Setup | Score |
|---|---|
| One fixed set of 25 examples | 0.590 |
| The 25 most similar examples for each sentence | 0.675 |
| + a first question that states the matrix | 0.684 |
| + three answers, keep the one that agrees most with the others | 0.694 |

The same model (Qwen3.8-27B on PSNC) and the same 102 variables throughout. With similar examples, each variable's group of 17 was left out of its own examples; with the fixed set, the variable itself. The similar examples are found with PSNC's embedding model.

**Good examples in the prompt help a lot.** The score doubles when the prompt shows 20 worked examples instead of none ([results](https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/main/iadopt-lab/few-shot-selection/shot-pool-30-results.md)).

| Examples in the prompt | 0 | 5 | 10 | 20 | 30 |
|---|---|---|---|---|---|
| Score | 0.22 | 0.37 | 0.38 | 0.45 | 0.46 |

Ten repetitions with 20, 25 and 30 examples gave the same level, so more than 20 examples bring nothing more.

**Which examples you pick matters.** A fixed set of 25 examples, chosen by testing 600 random sets, did better than random sets on variables that were not used for the choice ([report](https://github.com/ArvinRastegar/i-adopt-llm-based-service/blob/main/iadopt-lab/few-shot-selection/example_selection/output/example-selection-report.md)). Picking the 25 most similar examples for each sentence does better still (first table). The service keeps the fixed set only for when the similarity lookup fails.

**Different vocabularies need different methods.** A chat model and a small reranker were compared for linking. Each was better on some vocabularies ([linking experiments](https://github.com/ArvinRastegar/i-adopt-llm-based-service/tree/main/iadopt-linking)).

## How a release is checked

- Every push runs more than 1,100 automated tests, the frontend build and the container builds.
- Before a release the service runs on all 102 reference variables and is scored. Each variable is left out of its own examples, so no prompt shows the answer that is scored. It has to reach a fixed score, so this check is there to catch regressions.
- Tests make sure that the examples picked, the matrix question and the final prompt are exactly the ones tested in the lab.

## Limits

- The benchmark is one corpus of 102 English definitions, and the setup was tuned on it, so a new variable may score lower.
- The prompt in use today has a slightly different layout than the one in the table of example counts.
- To compare the three answers the service uses the exact score; the lab used the close score, which needs a local model. On the lab's stored answers both gave the same gain.
- The numbers come from experiments, not from users of the service.
