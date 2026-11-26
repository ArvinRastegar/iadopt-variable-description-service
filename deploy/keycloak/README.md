# Signing in with ROHub: what each client needs, and runbooks

People can sign in with ROHub, a Keycloak that another institution runs. This file says what that
institution has to set for us, and what our operator does. "Signing in with ROHub" in
[the manual](../../docs/manual.md#signing-in-with-rohub) lists the settings. `<...>` stands for a value
that is not published here.

`<public URL>` is `IADOPT_PUBLIC_URL`: `http://localhost:5173` on a development machine,
`https://<host>` in production. The client is public: there is no client secret anywhere.

## What each client must have

| Setting | Development client | Production client |
|---|---|---|
| Client type | Public, no secret | Public, no secret |
| Flows | Authorization Code only: no implicit, password, device or service-account flow | The same |
| PKCE | `S256` required, wanted | `S256` required, **a condition** |
| Callback addresses | Exactly `<public URL>/api/auth/callback`; the production one may stay beside it | Exactly `<public URL>/api/auth/callback` and no other, **a condition** |
| Web origins | None is needed | The same; no localhost, **a condition** |
| Return address after sign-out | Exactly `<public URL>/` | Exactly `<public URL>/` |
| Back-channel sign-out | None: a laptop cannot be reached | `<public URL>/api/auth/backchannel-logout`, with the session id |
| Front-channel sign-out | Off | Off |
| Base URL | `<public URL>/` | `<public URL>/` |
| Scopes | `openid`, `profile`, `email` | The same |
| ID-token signature | `RS256`, `PS256` or `ES256` | The same |
| Roles, audience mappers | None | None |

**The conditions for production.** The production client stays switched off until its operator says that
it requires `S256` and has no localhost entry. A client that is off refuses everything, so the evidence
comes in this order:

1. The client is switched on. The live site stays in `local`, so nobody can sign in through it.
2. Before anything else, run this in the backend's container (`docker compose exec backend ...`, or `oc exec` into the
   backend's pod). The command needs the three settings of the client, which change nothing while the mode is `local`:

   ```bash
   env IADOPT_OIDC_ISSUER=<issuer> IADOPT_OIDC_CLIENT_ID=<client ID> IADOPT_PUBLIC_URL=<public URL> python -m app.cli oidc check --pkce --refused-callback http://localhost:5173/api/auth/callback --refused-callback <public URL>/ --refused-callback <public URL>/api/auth/callback/x --refused-callback https://<another host>/api/auth/callback
   ```

   Every line must say `ok`, and the exit code must be 0. That no web origin is left for localhost cannot be seen from
   outside: for that the client's operator has to be taken at their word.
3. When a check fails, the client is switched off again.

The other rows are wanted and are no condition. Without the return address a sign-out ends on ROHub's
page; without the back-channel address a session ended at ROHub lasts here until its own end.

## Runbooks

**Restart** in them means: start the backend again with its new settings. With Docker Compose that is
`docker compose up -d backend` (a plain `docker compose restart` does not read `.env` again); on OpenShift, change the
secret and roll the backend's deployment out again.

### 1. Try the sign-in on a development machine

1. Add to the local `.env`, and make sure that no other line sets the same names:

   ```
   IADOPT_ENVIRONMENT=development
   IADOPT_AUTH_MODE=both
   IADOPT_OIDC_ISSUER=<issuer of the development realm>
   IADOPT_OIDC_CLIENT_ID=<client ID>
   IADOPT_PUBLIC_URL=http://localhost:5173
   IADOPT_COOKIE_SECURE=false
   ```

2. Rebuild and start: `docker compose up --build -d`. The first start of this version migrates the
   database after a verified backup. `docker compose logs backend` must show
   `The sign-in mode is both (development).` A start that stops names the setting at fault.
3. Open exactly `http://localhost:5173`, not `127.0.0.1`. Press **Sign in**, then **Sign in with ROHub**.
   You come back to the same page with your work.
4. Make the account the owner of this machine's database, if it has none yet. The username is in the
   account menu ("Signed in as ..."):

   ```bash
   docker compose exec backend python -m app.cli owner bind --username NAME
   ```

   With an owner already bound, use `owner rebind --username NAME --reason TEXT`. The **Admin** entry
   appears after a reload.
5. What the account may do: as the owner it needs no approval, so it may do everything, with the admin
   page. Any other ROHub account can use the public tools and see its account until an admin approves it
   on the admin page; until then the page says "An admin must approve your account first." and the account
   menu says "Waiting for approval".
6. Back to passwords only: set `IADOPT_AUTH_MODE=local` (or remove the line) and run
   `docker compose up -d`. Sessions that ROHub started count nowhere then; nothing else changes.

### 2. Switch a hosted deployment to ROHub

Only as part of the cutover: before it, people would lose their password sign-in, and accounts would
exist without terms. Accounts are not linked before this step.

1. The conditions above hold, in their order.
2. Find the number of proxies in front of the backend, with `FORWARDED_ALLOW_IPS` unset: set
   `IADOPT_TRUSTED_PROXY_HOPS`, start, and compare the address the audit log shows for a known visitor
   with the visitor's own. A number that is too small shows a proxy's address; one that is too large
   lets visitors choose theirs (send a request with `X-Forwarded-For: 192.0.2.1` to see).
3. Set `IADOPT_AUTH_MODE=oidc`, `IADOPT_OIDC_ISSUER`, `IADOPT_OIDC_CLIENT_ID`, `IADOPT_PUBLIC_URL`,
   `IADOPT_COOKIE_SECURE=true`, and `IADOPT_OIDC_POST_LOGOUT=true` once the return address is registered.
   Restart; the log says `The sign-in mode is oidc (production).`
4. The owner signs in once with ROHub and does nothing else there; the account menu says "Signed in as NAME". That
   is a new account, which waits and is no admin. Fold it into the owner's local account, to which the owner was bound
   after the first release with roles (manual, "Accounts, admins and the owner"):

   ```bash
   python -m app.cli legacy show --username NAME
   python -m app.cli legacy fold --from NEW_ID --into OWNER_ID --reason "The owner's ROHub sign-in"
   ```

   `legacy show` prints the new account's id, issuer and subject. After the next sign-in the owner is the owner again,
   with its records and saved ORCID iDs. Do not bind the new account first: the owner's account cannot be folded, and
   its history stays. Only an owner who has no local account binds the new one, with
   `owner bind --issuer ISS --subject SUB`, or `owner rebind` with the same options and `--reason TEXT`.
5. Leave `IADOPT_DAILY_DECOMPOSITIONS` empty: a number lets people in without an approval. First fold the
   new accounts that belong to a local one (`legacy fold`): one that was ever approved cannot be folded any
   more. Then approve the accounts that wait on the admin page, the local ones that were linked or folded
   into among them.
6. Back to passwords: `IADOPT_AUTH_MODE=local`, restart.

### 3. ROHub is not reachable, changed its keys, a client setting or its address

- **Not reachable.** New sign-ins end on the sign-in page with "ROHub cannot be reached right now."
  Open sessions and the public tools keep working. Nothing to do here; `oidc check` shows what is wrong.
- **Changed keys.** Nothing to do: an unknown key is read at once, at most once a minute.
- **A changed client setting.** People see "ROHub did not complete the sign-in." Run `oidc check --pkce`
  and compare the client with the table above.
- **A new issuer address.** Set `IADOPT_OIDC_ISSUER` and restart. Sessions of the old issuer no longer
  count, and **everyone gets a new account**, since accounts belong to issuer and subject. No command moves an
  account to the new issuer: `legacy link` and `legacy fold` take only local accounts with a password and no identity.

### 4. End sessions

- One person's: block the account on the admin page and unblock it. Both kinds of session end at once.
- Everybody's: set a new `IADOPT_SESSION_SECRET` and restart. Everyone has to sign in again.

### 5. The owner cannot sign in

`python -m app.cli owner rebind --username NAME --reason TEXT` moves the binding to another account, or
restores the owner's account when it was blocked or demoted.

### 6. Roll back

- To passwords only: `IADOPT_AUTH_MODE=local`, restart.
- To the previous image: roll the frontend back together with the backend. No ROHub session is a session
  there, and every block holds, except that of the bootstrap account unless its settings are unset first
  (manual, "Rolling back").

## The local stack

For trying what a development client of the hosted provider cannot show on a laptop, above all a sign-out that
the provider starts, and for working without it. `compose.yml` starts Keycloak in development mode and a mail
catcher on this machine only, and `realm.json` is the realm it begins with: one public client for
`http://localhost:5173`, registration with a confirmed email, and nobody in it. Nothing is kept: stopping it forgets
every account, and the next start has new subjects, so the application makes new accounts.

1. Start the application's own stack first, so that its network exists: `docker compose up -d`.
2. Make up an administrator for this run and start Keycloak, in the project folder. Stay in this terminal: Compose and the
   check need both values again, stopping it too.

   ```bash
   export KEYCLOAK_ADMIN_USERNAME=local-admin KEYCLOAK_ADMIN_PASSWORD="$(openssl rand -hex 16)"
   docker compose -f deploy/keycloak/compose.yml up -d
   ```

3. Set in `.env`, in place of other lines with the same names, and run `docker compose up -d` again:

   ```
   IADOPT_ENVIRONMENT=development
   IADOPT_AUTH_MODE=both
   IADOPT_OIDC_ISSUER=http://keycloak.localhost:8180/realms/iadopt-local
   IADOPT_OIDC_CLIENT_ID=iadopt-local
   IADOPT_PUBLIC_URL=http://localhost:5173
   IADOPT_OIDC_POST_LOGOUT=true
   IADOPT_COOKIE_SECURE=false
   ```

4. Open `http://localhost:5173`, press **Sign in**, **Sign in with ROHub** and then **Register**. The mail that confirms
   the address is at `http://localhost:8125`. Use a browser that opens names ending in `.localhost` by itself, as Chrome
   and Firefox do. Whoever registers waits for an approval, as with ROHub.
5. Let the check play the browser. It registers people with made-up addresses and leaves their accounts in the
   application's database. It needs the project's Python environment, `.venv`, with `backend/requirements.txt` (made
   once with `python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt`):

   ```bash
   .venv/bin/python backend/tools/keycloak_check.py
   ```

   Every step must say `ok`, and the exit code must be 0. It sends nothing to an address that is not this machine.
6. Stop it with `docker compose -f deploy/keycloak/compose.yml down`, and put the lines of `.env` back.

Where the project folder has another name, set `IADOPT_NETWORK` to the application's network (`docker network ls`).
