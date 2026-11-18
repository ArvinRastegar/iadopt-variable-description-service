# I-ADOPT Variable Description Service

This project provides a simple web interface for **decomposing variable definitions into the I-ADOPT ontology structure** and **visualizing the result**.

The system combines:

* a **Python backend** that runs the LLM pipeline
* a **React + TypeScript frontend** that visualizes, edits, exports and publishes the generated RDF/Turtle (TTL)

Visualization, editing, pre-nanopublication preparation and local SVG, PNG and Turtle
export are available without signing in. Decomposition, nanopublication publication
and retraction require the existing local account login when authentication is enabled.
The main page offers an in-page sign-in form that preserves the current work; after
signing in, press the intended action again. Anonymous tool use sends no activity events.
Administration and account APIs remain protected. Keep `IADOPT_AUTH_ENABLED=true` in
hosted deployments; disabling authentication is only for local development.

For a signed-in account the workspace keeps a record of the work: the decomposition or the
opened Turtle, each applied change, each "Looks right", and the version the person marks as
**best**, with the account and the IP address, browser, device and screen of each step.
Publishing needs that mark: only the version marked as best can be published. Anonymous
visitors are never recorded, and a deployment with authentication disabled records nothing
and cannot publish.

The user can:

1. describe a variable in plain text on the start page and click **Decompose**, watching the model output stream in
2. review the draft in the **Workspace**: the **I-ADOPT diagram**, the list of components with their Wikidata, QUDT or OM links, the **Turtle**, and the **raw model output** with the **JSON schema validation** result
3. correct components in the inspector (label, IRI, description, shape, constraints) and mark each one as checked
4. open or paste existing Turtle to visualize, edit and export it, without an account
5. mark the version they stand behind as **best**, with an optional note on what they changed and why
6. export SVG, PNG or Turtle, or publish the version marked as best as a **nanopublication** with their ORCID iD, and retract it later under **Publications**

---

# Project Architecture

The project consists of two main parts.

## Backend (FastAPI)

Location:

```
backend/
```

The backend is responsible for running the **LLM decomposition pipeline**.

It performs the following steps:

1. receives a **variable definition** from the frontend
2. builds an **LLM prompt**
3. calls the configured provider — **PSNC** by default, or OpenRouter
4. extracts and parses the **JSON output**
5. generates a **label and comment** in a second, smaller call
6. validates the output using a **JSON Schema**
7. links components to **Wikidata, QUDT and OM** entries (see *Entity linking* below)
8. converts the structured result to **RDF/Turtle (TTL)**
9. returns the results to the frontend

### The measured configuration

By default the decomposition step (2–4) runs a configuration measured on a
held-out set by the sibling `iadopt-lab` project: model `Qwen3.8-27B` on PSNC, the
`matrix-decomposition-v1` prompt rendered byte-exactly with 25 fixed few-shot
examples, reasoning disabled, `T=0.5`, `top_p=1.0`, `max_tokens=16000`. Each example
is shown as its definition followed by its answer as indented JSON, in the shape of
the output the model is asked for. Micro Close F1 **0.6117 ± 0.0190** over 15
repetitions on the 24 variables held out from example selection, in an exploratory
side test of 2026-11-18. In the same test the earlier layout, one compact JSON line
with every answer before its definition, scored 0.4924 (0.4890 when it was first
measured).

Read that figure carefully before relying on it. It comes from a side test, not
from the lab's official experiment, and that test chose the new layout on the same
24 variables, so for this prompt they are no longer strictly held out. It covers one
set of 25 examples, which were selected with the earlier layout, and the new layout
changes three things at once (order, field order and layout). The first measurement found that this example selection
beat a deliberately-bad and a domain-stratified selection decisively, but was *not*
significantly better than the best of three random 25-example draws; in that
measurement 3 of 24 held-out items were exactly right and 7 of 24 scored zero.
**Decompositions are drafts for review, not answers.**

That prompt returns six lexical fields and forbids regenerating the label,
comment, or definition, which is why step 5 exists as a separate call. Changing the
prompt, the examples, the model, or any sampling parameter means the service is no
longer running what was measured and the figure above no longer describes it. See
the separate private workflow repository, `iadopt-variable-description-service-docs/`
(`decisions.md` and `components/`), for the full record. It is an optional local
checkout and is not needed to build or run the service.

### Entity linking

Step 7 links each label of the decomposition (property, object of interest, matrix,
context object, statistical modifier, and the parts of systems) to a controlled-vocabulary
entry. For every label the backend searches **Wikidata** items, **QUDT** quantity kinds and
**OM** quantities (at most ten candidates from each), then asks the model that made the
decomposition, with reasoning off, which candidate is the same concept as the label, or none
of them. By default that is `Qwen3.8-27B` on PSNC, the configuration that was measured; a run
on another PSNC or OpenRouter model asks that model the same question. The answer
is a single character, so it is checked strictly: if it is not exactly one of the offered
labels, the identical request is sent again, up to three more times. A label the model
matches to nothing, or never answers validly, is left unlinked (so is a label longer than
200 characters), and if the model's provider cannot be reached or refuses the key, no label
is linked and the decomposition is returned without links. Links the decomposition model wrote itself are discarded; only links
chosen here are kept. The links appear as
`*URI` fields in `enriched_json` and as IRIs in the Turtle, where a property can now point
at a QUDT or OM quantity kind.

The question, its labels and its decoding parameters are those measured in the sibling
`iadopt-linking` project. That measurement used curated corpus labels and compared the model
with a different reranker from the one this service used before, and the difference could not
be told from zero. **Links are drafts for review, like the decomposition.** QUDT and OM are
searched by the words of the label, so a paraphrase can miss an entry the curated wording
would have found.

Linking is switched off with `ENABLE_WIKIDATA_LINKING=false` (the name is historical; it
switches all linking). It needs `PSNC_API_KEY`, and the backend must be able to reach
`www.wikidata.org`, `www.qudt.org` and `www.ebi.ac.uk`; a vocabulary that cannot be reached
is skipped and logged. Every outbound request carries a User-Agent that names this project and
its public URL, as Wikimedia asks; Wikidata rate-limited the earlier generic one after about ten
requests in five seconds. The earlier reranker and its `RERANK_THRESHOLD` and `PSNC_RERANK_MODEL`
settings are gone.

The backend exposes a simple API:

```
POST /decompose
```

Input:

```
{
  "definition": "My system is measuring air temperature at a height of 1.7m"
}
```

Output:

```
{
  raw_llm_output,
  parsed_json,
  validation_errors,
  ttl
}
```

---

## Frontend (React + TypeScript)

Location:

```
frontend/
```

The frontend provides the **interactive user interface**: a React + TypeScript app built with
Vite, styled with Tailwind CSS and [shadcn/ui](https://ui.shadcn.com) components (Radix
primitives), with CodeMirror as the Turtle editor. The Geist fonts are bundled, so the pages
load nothing from other hosts. Light and dark themes follow the system setting until the
visitor picks one.

The pages keep their previous URLs, so nginx needs no change:

* `index.html`: the start page, the workspace (`#/workspace`) and the publications (`#/publications`)
* `login.html`: the sign-in page nginx sends visitors to for protected pages
* `admin.html`: the admin dashboard (statistics, readiness, users, audit log)
* `remote.html`: the embeddable viewer, unchanged

The I-ADOPT model, Turtle parsing, the diagram layout engine (from the
[I-ADOPT Visualizer](https://doi.org/10.5281/zenodo.18097903) by Sirko Schindler) and the
nanopublication helpers stay in `src/lib` and `src/model`. The diagram reuses that layout engine
and only draws it in the new style; SVG and PNG exports embed the fonts and use the light colours.
Drafts, review checks and the list of publications are kept in the browser's local storage
(publications under the same key as before).

Working on the frontend, from `frontend/`:

```bash
pnpm dev                  # http://localhost:5173, proxies /api to localhost:8000
pnpm test:unit --run      # unit tests (run in CI)
pnpm build                # type check and production build (run in CI)
pnpm exec playwright install chromium   # once, for the browser tests
pnpm test:browser --run   # browser tests (not run in CI)
```

---

# Running the Project

The easiest way to run the project is using **Docker Compose**.

This will start:

* the **backend container**
* the **frontend container**

---

# 1. Clone the Repository

```bash
git clone (https://github.com/ArvinRastegar/i-adopt-service-website.git)
cd i-adopt-service-website
```

---

# 2. Create a `.env` File

Create a file called:

```
.env
```

in the **project root**.

Inside the file add:

```
OPENROUTER_API_KEY=sk-or-xxxxxxxxxxxxxxxxxxxx
PSNC_API_KEY=your-psnc-api-key
NANOPUB_PRIVATE_KEY=base64-body-of-private-key
NANOPUB_ORCID_ID=0009-0006-1978-4302
NANOPUB_PROFILE_NAME=Barbara Magagna
NANOPUB_AGENT_INTRO_URI=https://w3id.org/np/RAwy2xTZzt5Y3ix-7f1HDTewgZqa6eRm5YnmrSKCy0PTA
```

Replace the keys with your **OpenRouter API key** and, if you want to use PSNC models, your **PSNC API key**.

These keys are used by the backend to call the selected LLM provider.
The nanopub values are used by the backend to sign and publish nanopublications from the frontend's Turtle payload.
`NANOPUB_ORCID_ID` and `NANOPUB_PROFILE_NAME` identify the **signing profile** (the key owner) only. They are never used as the creator of a nanopublication or of generated Turtle: the person publishing or retracting types their own **Creator ORCID** below the diagram, and publishing or retracting without one is refused.
`NANOPUB_PRIVATE_KEY` and `NANOPUB_PUBLIC_KEY` can be provided either as one-line base64 key bodies or as PEM strings with `\n` escapes.

Optional nanopub settings:

```
NANOPUB_PUBLIC_KEY=...
NANOPUB_AGENT_URI=...
NANOPUB_PROFILE_INTRODUCTION_URI=...
NANOPUB_LICENSE_URI=https://creativecommons.org/publicdomain/zero/1.0/
NANOPUB_USE_TEST_SERVER=false
MODEL_NAMES=qwen/qwen3.5-flash-02-23,qwen/qwen3-32b,qwen/qwen3.5-397b-a17b
PSNC_API_BASE_URL=https://llm.hpc.psnc.pl
PSNC_MODEL_NAME=Qwen3.8-27B
PSNC_MODEL_NAMES=Qwen3.8-27B,Qwen3.5-397B-A17B,Qwen3-VL-235B-A22B-Instruct-FP8

TEMPERATURE=0.5
TOP_P=1.0
MAX_TOKENS=16000
USE_MEASURED_CONFIGURATION=1
```

`PSNC_MODEL_NAME`, `TEMPERATURE`, `TOP_P` and `MAX_TOKENS` above are the **measured
configuration** (see below). `PSNC_MODEL_NAMES` must keep `Qwen3.8-27B` in the
list: the backend rejects any model not named there, even when a request asks for
it explicitly.

Set `USE_MEASURED_CONFIGURATION=0` to fall back to the previous prompt templates
and five-shot examples, which remain in the repository.

---

# 3. Build and Run the Project

From the project root run:

```bash
docker compose up --build
```

Docker will:

1. build the backend image
2. build the frontend image
3. start both containers

---

# 4. Open the Application

Frontend:

```
http://localhost:5173
```

Backend API:

```
http://localhost:8000/docs
```

---

# How to Use the Application

1. **Sign in** (top right). Decomposing and publishing need an account; visualizing, editing
   and exporting Turtle do not.

2. Describe the variable on the start page and click **Decompose** (or press ⌘↵ / Ctrl+↵).

Example:

```
My system is measuring air temperature at a height of 1.7m
```

3. The **Workspace** opens and shows the model output while it streams. Then you see the
   I-ADOPT diagram, with the words of your definition highlighted where each component was found.

4. Select each component in the diagram or the review list and check its label and vocabulary
   link in the inspector; fix it there or in the **Turtle** tab, then mark it as checked.
   The **Model output** tab shows the raw response and the JSON schema validation.

5. When the decomposition is right, click **Mark as best** next to **Publish**. A note on what you
   changed and why is optional. Components without "Looks right" bring a warning, not a block.

6. Click **Export** for SVG, PNG or Turtle, or **Publish** to sign and publish a nanopublication:
   the dialog checks the draft, including that this version is the one marked as best, asks for
   your ORCID iD and asks you to confirm, because publishing is permanent and public. Retract it
   later under **Publications**.

---

# Stopping the Application

Press:

```
CTRL + C
```

or run:

```bash
docker compose down
```

---

# Release Quality Gate

Before merging `dev` into `prod`, run the quality gate. It checks that the code to be
released still decomposes the lab's 102 corpus variables at least as well as a threshold.
It is a command you run locally; CI does not run it, because CI has no model provider key.

It needs:

* the stack running the code to release, started with `docker compose up -d --build`
* a checkout of the sibling `iadopt-lab` project with its virtual environment, next to this
  repository or named by `IADOPT_LAB_PATH` (or `--lab`)

From the project root run:

```bash
../i-adopt-llm-based-service/iadopt-lab/.venv/bin/python backend/tools/quality_gate.py
```

First the gate asks the backend container what it runs, which calls no model. It stops
(exit code 2) when the container's `backend/app` and `backend/data` differ from your working
tree, so rebuild after every change. It also stops when the backend is not the measured
configuration prod runs by default: PSNC, `Qwen3.8-27B`, the measured prompt, temperature
0.5, top_p 1.0 and max_tokens 16000. Pass `--any-backend` to measure another configuration
anyway.

Then it sends all 102 corpus definitions through the backend's own pipeline inside the
container. Each definition runs the whole pipeline (decomposition, label and linking with its
vocabulary searches), so one run makes hundreds of calls to the provider; the first run took
76 seconds. It writes no records. Each answer is scored with the lab's extraction, validation
and Close F1 scorer, as the lab's example-layout side test scores its answers. An answer the
app failed to produce, or one the lab cannot use, counts as an empty prediction. (The lab's
official workflow differs: it retries an invalid answer up to three times.)

The gate **passes** when the micro Close F1 of one run on all 102 variables is at least
**0.60** (exit code 0); below that it fails (1). Definitions the provider never answered,
or whose answer was lost, also count as empty. With them the run can still pass, but below
0.60 the gate cannot tell the code from a provider outage, so it does not decide (2, as when
it could not run). `--threshold`, `--concurrency` (8 at once) and `--report-dir` change the
defaults. Each run that got answers writes a JSON report to `quality-gate-reports/`, which
git ignores. The report holds every answer, the commit, and whether the tree had uncommitted
changes.

Read the verdict with two caveats:

* 25 of the 102 variables are the prompt's own few-shot examples, shown to the model with
  their answers, so they score far higher than the rest. The gate prints both groups apart,
  for information. The first run (2026-11-18) scored 0.695 on all 102, **0.571** on the 77
  variables the prompt does not show, and 0.976 on the 25 examples. The 0.60 on all 102 is a
  floor against regressions, not a claim that new variables score 0.60.
* Runs vary. In the lab's side test, repeated runs on 24 variables had a standard deviation
  of 0.019; the spread on all 102 has not been measured. A result near 0.60 can pass or fail
  by chance, so run it again before acting on a narrow result.

---

# Project Structure

```
i-adopt-service-website
│
├── backend
│   ├── app
│   │   └── main.py
│   ├── data
│   ├── requirements.txt
│   └── Dockerfile
│
├── frontend
│   ├── src
│   │   ├── app          (main page: start, workspace, publications)
│   │   ├── admin        (admin dashboard)
│   │   ├── login        (sign-in page)
│   │   ├── components   (shadcn/ui building blocks)
│   │   ├── lib, model   (I-ADOPT model, parsing, layout, nanopub helpers)
│   │   └── styles       (design tokens, light and dark)
│   ├── css              (styles of remote.html)
│   ├── index.html, login.html, admin.html, remote.html
│   └── Dockerfile
│
├── docker-compose.yml
└── README.md
```

---

# Summary

This project connects an **LLM-based decomposition service** with the **I-ADOPT visualizer**.

The backend performs the semantic decomposition and generates RDF.

The frontend provides a simple interface to explore the results and visualize the variable structure.

To load frontend and backend separately:
in root directory: 
python3 -m uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
in frontend directory:
pnpm dev
Example of variable definition: 
Dynamic shear viscosity of polystyrene PS042 under the testing conditions of DIN 51810-1.
