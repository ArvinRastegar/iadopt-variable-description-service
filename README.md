# I-ADOPT Variable Description Service

This project provides a simple web interface for **decomposing variable definitions into the I-ADOPT ontology structure** and **visualizing the result**.

The system combines:

* a **Python backend** that runs the LLM pipeline
* a **JavaScript frontend** that visualizes the generated RDF/Turtle (TTL)

The user can:

1. enter a variable definition in plain text
2. click **Decompose**
3. inspect the **raw LLM output**
4. inspect **JSON schema validation errors**
5. view the **generated TTL**
6. click **Visualize**
7. see the **I-ADOPT diagram**

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
7. enriches components with **Wikidata URIs**
8. converts the structured result to **RDF/Turtle (TTL)**
9. returns the results to the frontend

### The measured configuration

By default the decomposition step (2–4) runs a configuration measured on a
held-out set by the sibling `iadopt-lab` project: model `Qwen3.8-27B` on PSNC, the
`matrix-decomposition-v1` prompt rendered byte-exactly with 25 fixed few-shot
examples, reasoning disabled, `T=0.5`, `top_p=1.0`, `max_tokens=16000`. Held-out
micro Close F1 **0.4890 ± 0.0252** over 24 variables and 15 repetitions.

Read that figure carefully before relying on it. It beats a deliberately-bad and a
domain-stratified example selection decisively, but it is *not* significantly
better than the best of three random 25-example draws. In absolute terms 3 of 24
held-out items were exactly right and 7 of 24 scored zero. **Decompositions are
drafts for review, not answers.**

That prompt returns six lexical fields and forbids regenerating the label,
comment, or definition, which is why step 5 exists as a separate call. Changing the
prompt, the examples, the model, or any sampling parameter means the service is no
longer running what was measured and the figure above no longer describes it. See
`docs/decisions.md` and `docs/components/` for the full record.

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

## Frontend (Vite + JavaScript)

Location:

```
frontend/
```

The frontend provides the **interactive user interface**.

It allows the user to:

1. enter a variable definition
2. send it to the backend
3. display:

   * raw LLM output
   * validation errors
   * generated TTL
4. visualize the TTL using the **I-ADOPT visualizer**
5. export the current TTL or publish it as a nanopublication

The visualization is rendered as an **SVG diagram**.

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

1. Enter a **variable definition** in the first textbox.

Example:

```
My system is measuring air temperature at a height of 1.7m
```

2. Click **Decompose**

You will see:

* raw LLM output
* schema validation results
* generated TTL

3. Click **Visualize**

The I-ADOPT diagram will appear below.

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
│   ├── css
│   ├── index.html
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
