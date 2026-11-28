# I-ADOPT Variable Description Service: manual

This is the manual. The overview is in the [README](../README.md), the measurements are in
[evaluation.md](evaluation.md).

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

What a session may do is decided by the backend, one capability per route, and reported in
`GET /api/auth/me`; the pages show what it allows.

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

By default the decomposition step (2–4) runs the best pipeline found in the experiments of the sibling `iadopt-lab`
project, with model `Qwen3.8-27B` on PSNC, reasoning disabled, `T=0.5`, `top_p=1.0`, `max_tokens=16000`:

* **Examples by similarity.** PSNC's embedding model `Qwen3-Embedding-0.6B` turns the definition into a vector, and the
  prompt shows the 25 of the lab's 102 corpus variables whose definitions are most similar, the most similar last
  (`backend/data/measured/example-pool.json`). If that request brings no vector to use, the prompt shows the 25 fixed
  examples it used before; the finder's matrix statement is still added when it answered (each step helped on its
  own in the lab; the two together were not measured).
* **The matrix finder.** At the same time, the model is asked once (temperature 0) to classify the definition with the
  I-ADOPT patterns, naming the object, the matrix entity and the restricting phrases first
  (`backend/data/measured/matrix-finder.json`). The matrix it decides is stated after the definition in the prompt; if
  its answer is not usable, nothing is stated.
* **The most typical of three answers.** The `matrix-decomposition-v1` prompt, rendered byte-exactly with those
  examples, goes out three times side by side, and the answer that agrees most with the other two is kept: agreement
  is the evaluation scorer's Exact F1 of one answer against the other, both ways (the lab used its Close F1, which
  needs a local model; on the lab's stored answers both gave the same gain), and a tie goes to the first answer the
  lab's check accepts. On the stream page the first answer streams as it comes; if another answer is kept, a note and
  that answer follow.

Each example is shown as its definition followed by its answer as indented JSON, in the shape of the output the model
is asked for. In the lab, on the 102 corpus variables and PSNC, this pipeline scored a Close F1 of 0.694 against 0.590
for the fixed 25 examples, in about 11 seconds per variable (under 30 for every one measured); exploratory, and tuned
on these same 102 variables, so a new variable may score lower. Each run's record keeps what was chosen (its
examples, the matrix stated, the answer kept). How the earlier prompt was measured, and what that does and does not
show, is in [evaluation.md](evaluation.md). **Decompositions are drafts for review, not answers.**

That prompt returns six lexical fields and forbids regenerating the label,
comment, or definition, which is why step 5 exists as a separate call. Changing the
prompt, the examples, the pool, the finder, the number of answers, the model, or any sampling parameter means the
service is no longer running what was measured and the measurements no longer describe it. The pool and the finder's
data are rebuilt only from the lab, with `backend/tools/build_example_pool.py` and `backend/tools/build_finder_data.py`
and the lab's Python; neither sends a request.

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
* `admin.html`: the admin dashboard (statistics, readiness, accounts with their roles and blocks, audit log)
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
git clone https://github.com/ArvinRastegar/iadopt-variable-description-service.git
cd iadopt-variable-description-service
```

---

# 2. Create a `.env` File

Create a file called:

```
.env
```

in the **project root**.

Start from the sample (`cp .env.example .env`). These values are yours to set:

```
IADOPT_SESSION_SECRET=a-long-random-secret
IADOPT_BOOTSTRAP_ADMIN_USERNAME=admin
IADOPT_BOOTSTRAP_ADMIN_PASSWORD=a-strong-password
IADOPT_COOKIE_SECURE=false
OPENROUTER_API_KEY=sk-or-xxxxxxxxxxxxxxxxxxxx
PSNC_API_KEY=your-psnc-api-key
NANOPUB_PRIVATE_KEY=base64-body-of-private-key
NANOPUB_ORCID_ID=0000-0000-0000-0000
NANOPUB_AGENT_INTRO_URI=https://w3id.org/np/...
```

The backend does not start without `IADOPT_SESSION_SECRET` while accounts are on. `IADOPT_COOKIE_SECURE=false` is for plain http on your
own machine only. A provider key must not be empty, even when you do not decompose: the start check asks for it.

Replace the keys with your **OpenRouter API key** and, if you want to use PSNC models, your **PSNC API key**.

These keys are used by the backend to call the selected LLM provider.
The nanopub values are used by the backend to sign and publish nanopublications from the frontend's Turtle payload.
`NANOPUB_ORCID_ID` identifies the **signing profile** (the key owner) only. It is never used as the creator of a nanopublication or of generated Turtle: the person publishing or retracting types their own **Creator ORCID** below the diagram, and publishing or retracting without one is refused.
`NANOPUB_PRIVATE_KEY` and `NANOPUB_PUBLIC_KEY` can be provided either as one-line base64 key bodies or as PEM strings with `\n` escapes.

Optional nanopub settings:

```
NANOPUB_PUBLIC_KEY=...
NANOPUB_LICENSE_URI=https://creativecommons.org/publicdomain/zero/1.0/
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
http://localhost:5173/api/docs
```

(after signing in)

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

   The variable gets its permanent IRI when it is first published, not before: until then the IRI
   in the Turtle (`https://w3id.org/iadopt/variable/<date and time>-NN`) is only a draft name. The
   first publication writes the variable as `https://w3id.org/iadopt/variable/~~~ARTIFACTCODE~~~`,
   and signing puts the nanopublication's own code there, so the IRI is opaque, never the same
   twice, and names the nanopublication that introduced it (`https://w3id.org/np/<code>`). Publishing
   the same record again (after a correction, say) keeps that IRI and marks the new nanopublication
   as superseding the record's previous one, so the nanopublication network, Nanodash's search and
   the [I-ADOPT catalogue](https://mabablue.github.io/catalogue/) show the variable once, at its
   newest version; the catalogue picks it up in its nightly run. A record published before this
   change keeps the IRI it published then. A variable imported into a new record gets a new IRI.
   The publish dialog shows the IRI as published.

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

# Operating the Backend

The backend keeps everything in one SQLite file, `iadopt.sqlite3`, in its state directory
(`/app/state` in the container; `IADOPT_STATE_DIR` or `IADOPT_DB_PATH` change it). One backend
replica with one worker process is expected, but the commands below are safe beside a running
backend. Run them in the backend container, with its settings:

```bash
docker compose exec backend python -m app.cli db status
```

```bash
oc exec deploy/iadopt-backend -- python -m app.cli db status
```

Every command answers 0 on success, 1 on a refusal or failure with the error's name, and 2 on a
usage error.

## Database

| Command | Does |
|---|---|
| `db status` | Prints sizes, free space, applied and pending migrations and each table's rows; writes nothing |
| `db migrate` | Applies pending migrations, as every start does |
| `db backup` | Makes a verified backup and prints its path and size |
| `db reclaim` | Returns free pages to the file system; deletes no row |

**Migrations.** At every start the backend applies the migrations the file does not have yet, in
order. When any is pending it first makes a verified backup in `backups/` beside the database,
named `iadopt-<UTC time>-pre-migration.sqlite3`, and keeps the two newest of these. Backups made
with `db backup` are named `...-manual.sqlite3` and are never deleted by the application. Every
backup holds the same personal data as the database.

**Restore.** With the backend stopped, copy a backup over the database file and remove the old
journal files; the next start migrates it when it is older. With Docker Compose:

```bash
docker compose stop backend
```

```bash
docker compose run --rm --no-deps --entrypoint sh backend -c 'cd /app/state && cp backups/NAME.sqlite3 iadopt.sqlite3 && rm -f iadopt.sqlite3-wal iadopt.sqlite3-shm'
```

```bash
docker compose start backend
```

On OpenShift scale the deployment to 0, do the same copy from a temporary pod that mounts the
claim `iadopt-backend-state`, and scale back to 1.

**When a start stops with `InsufficientSpace`.** A pending migration needs room for the backup, for
the database's size once more when it rewrites the file, and for a reserve of 64 MiB. The database
is unchanged, so the previous image still starts on it. Remove `manual` backups from `backups/`, or
grow the volume, and start again. `SchemaMismatch` or `MigrationChanged` mean that the file is not
one this code can take over; nothing was changed then either.

**Rolling back.** Until the passwords are purged, the previous image runs on a migrated database.
Roll the frontend back together with the backend: the new pages deny everyone the admin page on the
previous backend. Sessions that ROHub started are no sessions there. Unset
`IADOPT_BOOTSTRAP_ADMIN_USERNAME` and `IADOPT_BOOTSTRAP_ADMIN_PASSWORD` **before** you start it: while
they are set it re-enables that account and makes it an admin at every start, without a record, which
undoes a block or revocation of it. Every other block and revocation holds.

## Accounts, admins and the owner

Accounts are ordinary users, admins, or the one owner. Admins manage ordinary accounts, see
activity and download the records; only the owner grants and revokes admin, and blocks admins.
Nobody can block the owner or take its admin rights on the admin page. Each grant, revocation,
block, unblock, approval and withdrawal is kept in the account's history with who did it and why.

**Once, after the first release with these roles, bind the owner** to an existing account. Until
then nobody can grant or revoke admin or block an admin:

```bash
python -m app.cli owner bind --username NAME
```

`owner rebind --username NAME --reason TEXT` moves the binding. The previous owner stays an admin
until the new owner revokes it. Rebinding to the same account restores an owner account that was
blocked or demoted outside these rules, for instance by the previous image after a rollback. Both
commands also take `--account ID`, or `--issuer ISS --subject SUB` for an identity the account holds,
and both end every session of the accounts they bind or unbind.

**The owner's own sign-in.** The owner keeps a password sign-in of their own in every mode, also where
only ROHub signs everybody else in: **Owner sign-in**, a small link under **Sign in with ROHub** on the
sign-in page and in the sign-in dialog. Only the owner's account signs in there; every other name gets
the answer of a wrong password. The owner's sign-in takes 10 tries per network address in 10 minutes,
then one a minute. Set or change the owner's password in the backend's container:

```bash
python -m app.cli owner password
```

On a terminal it asks twice and shows nothing; otherwise it reads one line from standard input. It takes
15 to 1,024 characters, never a password as an argument, and ends the owner's sessions. Where only ROHub
signs people in, only this command changes the owner's password, and the purge keeps the owner's password.

**Bootstrap admin.** `IADOPT_BOOTSTRAP_ADMIN_*` create the first admin on an empty database only.
Once any account exists they change nothing, and the log says so at each start.

**Local accounts.** Until the cutover to the identity provider, admins create local accounts with a
password on the admin page, always as ordinary users, and edit them. Where only ROHub signs people in,
the admin page resets no password, and a local account other than the owner's cannot sign in.

**Approving ROHub accounts.** An account that ROHub signs in waits until an admin or the owner approves
it on the admin page, which lists the waiting accounts first. Until then it can use the public tools and
keeps its saved ORCID iDs; decomposing, saving work, publishing and retracting are refused with
`approval_required` (with a [daily limit](#daily-decomposition-limit) set, only publishing and retracting).
Approved, it may do what a local account may. **Withdraw approval** takes effect at the account's next
request and deletes nothing. The optional note, for example how the terms were accepted, stays in the
account's history; the application itself asks nobody for the terms yet. Admins and the owner need no
approval as long as they are that, nor does a local account until `legacy link` or `legacy fold` makes it a
ROHub account: then it has to be approved once.

**At the cutover** the operator prepares the local accounts, offline. Commands that change an account
need a bound owner, and the account's history keeps the change with the reason given:

| Command | Does |
|---|---|
| `legacy show --account ID` (or `--username NAME`) | Prints an account's role, block state, identities, data and history |
| `legacy link --account ID --issuer ISS --subject SUB --reason TEXT [--keep-admin]` | Lets an identity-provider subject sign in to a local account, keeping its id, records and saved ORCID iDs. An admin stays one only with `--keep-admin` |
| `legacy fold --from ID --into ID --reason TEXT [--keep-admin]` | Moves the identities of a new, empty identity-provider account into a local account, and blocks the emptied one |
| `legacy purge-passwords --confirm-cutover` | Ends every password login except the owner's, in this image and the previous one, and keeps the owner's password. Without the flag it only counts, the owner's not among them |

Link and fold take an account with a password and no identity yet, so do both **before**
`legacy purge-passwords`. Fold a new account **before** you approve it: an account that was ever approved
has a history and counts as empty no more. After the purge nobody but the owner can sign in to a local account
that was neither linked nor folded into. A session of an ordinary one that is still open may decompose, save, publish and
retract nothing (`password_ended`), and no approval changes that; an admin or the owner keeps what the role gives.

## Signing in with ROHub

One switch, `IADOPT_AUTH_MODE`, decides how people sign in:

| Mode | Password | ROHub |
|---|---|---|
| `local` (default) | yes | no; its routes answer 404 |
| `oidc` | only the owner's, through **Owner sign-in** | yes |
| `both`, only with `IADOPT_ENVIRONMENT=development` | yes | yes |

With authentication enabled and no new setting, nothing changes. Disabled authentication now needs
`IADOPT_ENVIRONMENT=development`. A start with a missing or malformed setting stops with one message
that names every setting at fault.

| Setting | Meaning | Default |
|---|---|---|
| `IADOPT_ENVIRONMENT` | `production` or `development`. Only `development` allows `both`, disabled authentication, and `http` to `localhost`, a name ending in `.localhost`, `127.0.0.1` or `[::1]` | `production` |
| `IADOPT_AUTH_MODE` | The switch | `local` |
| `IADOPT_OIDC_ISSUER` | The issuer URL exactly as the provider writes it | needed in `oidc` and `both` |
| `IADOPT_OIDC_CLIENT_ID` | The client ID; the client is public, so there is no secret | needed in `oidc` and `both` |
| `IADOPT_PUBLIC_URL` | Where browsers reach the application: scheme, host and optional port. The callback is `<public URL>/api/auth/callback` | needed in `oidc` and `both` |
| `IADOPT_OIDC_POST_LOGOUT` | `true` once the provider has registered `<public URL>/` as the return address after a sign-out | `false` |
| `IADOPT_COOKIE_SECURE` | Must be `true` with an `https` public URL and `false` with an `http` one | `false` |
| `IADOPT_TRUSTED_PROXY_HOPS` | How many proxies in front of the backend append to `X-Forwarded-For`, 0 to 10 | unset; needed with `oidc` in `production` |
| `IADOPT_SIGN_IN_LIMIT` | Sign-ins one network address may start, and finish, as `<requests>/<seconds>` | `120/300` |

A person who signs in with ROHub for the first time gets an ordinary account, found again by ROHub's
subject, never by email. ROHub must report a verified email. The account waits until an admin approves
it ([Accounts, admins and the owner](#accounts-admins-and-the-owner)), on a development machine as in
production. Roles never come from ROHub: the operator binds the owner with `owner bind`, and the owner
makes admins. In `oidc` the owner still signs in with a password, through **Owner sign-in**
([Accounts, admins and the owner](#accounts-admins-and-the-owner)); the switch ends every other password
sign-in.

"Sign out" ends the session here and then goes to ROHub's sign-out page. "Sign out and clear this
browser" also deletes the draft, the remembered ORCID iD and the list of publications from it.

`python -m app.cli oidc check` asks the configured provider whether it is set up for us; `--pkce` also
sees that the client requires PKCE S256, and `--refused-callback URL` that a callback is refused. It
sends requests to the provider and writes nothing.

[`deploy/keycloak/README.md`](../deploy/keycloak/README.md) says what the client at ROHub must have, holds the
runbooks for an operator, and describes a Keycloak of your own for trying all of this on one machine.

## Daily decomposition limit

Switched off unless one setting is set. It is only for accounts that no admin has approved: approved
accounts, local accounts, admins and the owner have no daily limit, whatever it says.

| Setting | Meaning | Default |
|---|---|---|
| `IADOPT_DAILY_DECOMPOSITIONS` | Decompositions an account that is not approved has per day, a whole number from 1 to 1000; anything else stops the start | empty: it has none and waits for its approval; nothing is counted |

**A number lets people in without an approval.** Everyone who can sign in with ROHub may then decompose
that many times a day, and that work is recorded, although no admin has checked that they accepted the
terms. Only publishing and retracting still need the approval.

Set it, for example to `3`, or empty it, and start the backend again with its settings: with Docker
Compose `docker compose up -d backend` (a plain `restart` does not read `.env` again).

The day is a calendar day in Europe/Berlin. A use is reserved before a model is called, so requests at
the same moment cannot pass the limit together. Beyond it a request is refused with 429, the reason
`daily_limit`, the time of the reset and `Retry-After`; `GET /api/auth/me` shows what is left.

A result counts, and so does a failure of the model, of the way to it or of its output, and a run whose
browser went away. Nothing counts when the definition, the provider or the model was not valid, when no
request went out because a key or the address is missing or malformed, and when the provider refused
every request with a client error other than 408, 409 and 429. A run in which the server stopped is
given back at the next start with the limit switched on.

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
configuration prod runs by default: PSNC, `Qwen3.8-27B`, the best pipeline (setup `measured-best-pipeline`),
temperature 0.5, top_p 1.0 and max_tokens 16000. Pass `--any-backend` to measure another configuration anyway.

Then it sends all 102 corpus definitions through the backend's own pipeline inside the
container. Each definition runs the whole pipeline (the embedding request and the matrix finder,
three answers of which the most typical is kept, the label, and linking with its vocabulary
searches), so one run makes several hundred calls to the provider. Each definition is decomposed
with its own corpus variable left out of its examples and of the finder's, so no prompt shows the
answer being scored. It writes no records. Each answer is scored with the lab's extraction,
validation and Close F1 scorer, as the lab's side tests score their answers. An answer the app
failed to produce, or one the lab cannot use, counts as an empty prediction. (The lab's official
workflow differs: it retries an invalid answer up to three times.)

The gate **passes** when the micro Close F1 of one run on all 102 variables is at least
**0.60** (exit code 0); below that it fails (1). Definitions the provider never answered,
or whose answer was lost, also count as empty. With them the run can still pass, but below
0.60 the gate cannot tell the code from a provider outage, so it does not decide (2, as when
it could not run). The same holds when PSNC failed a definition's embedding request (its prompt
then showed the 25 fixed examples) or the finder's request (its prompt then stated no matrix):
below 0.60 the gate then says how many and does not decide. It prints how many definitions got
examples by similarity and how many fell back, one line per cause with its hint (for example `2
embedding request timed out after 5 s. PSNC's embedding model did not answer in time.`), which
matrix decisions the finder stated and why it failed where it did, and which of the three
answers was kept; the report has the same for each variable. `--threshold`, `--concurrency` (8 at once) and `--report-dir` change the
defaults. Each run that got answers writes a JSON report to `quality-gate-reports/`, which
git ignores. The report holds every answer, the commit, and whether the tree had uncommitted
changes.

Read the verdict with two caveats:

* The gate prints the 25 fixed examples and the other 77 apart, for information. Before the best pipeline the 25
  were the prompt's examples and saw their own answers, so they scored far higher; now no variable sees its own.
  The lab measured the best pipeline at 0.694 (ten runs on PSNC, with each variable's fold of 17 left out rather
  than only the variable; exploratory). The 0.60 on all 102 is a floor against regressions, not a claim that new
  variables score 0.60.
* Runs vary. In the lab, repeated runs with 20 to 30 examples on 72 variables had a standard deviation of 0.016 to
  0.018 ([evaluation.md](evaluation.md)); the spread on all 102 has not been measured. A result near 0.60 can pass or fail
  by chance, so run it again before acting on a narrow result.

---

# Project Structure

```
iadopt-variable-description-service
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
