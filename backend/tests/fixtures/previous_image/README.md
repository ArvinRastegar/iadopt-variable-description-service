# The previous image's SQL (`de1a7af`)

`de1a7af_statements.json` holds every SQL call the backend's stores made at application commit
`de1a7af` (the release live on `prod` since 2026-11-18), in order: startup twice (creating, then
re-enabling and re-promoting the bootstrap admin), accounts, login and sessions, admin edits,
audit rows, saved ORCID iDs, a decomposition with its model calls, an imported record with an
edit, a check, a metadata step, a best mark and a publication, and the admin export.

The increment-1 tests (`tests/test_app_storage.py`, `tests/test_accounts_roles.py`) replay these
statements on files the new code migrated (`AC-app-storage-011`, `AC-accounts-roles-016`): the
previous image must keep working on a migrated file until the owner purges legacy passwords
(D-017). `tests/previous_image.py` replays them.

## How it was made

`capture.py` beside this file runs the stores through a connection that logs every
`execute`, `executemany`, `executescript`, `commit`, `close` and `with` block, and writes the log.
It refuses to run unless the store sources it imports (`auth.py`, `records.py`,
`record_export.py`, `services/call_log.py`, `schemas/common.py`) are the `de1a7af` blobs, so it
cannot capture what later code does. Captured on 2026-11-23 from the working tree at `4cb9b19`,
whose `backend/app` is identical to `de1a7af` (`git diff 4cb9b19 de1a7af -- backend/app` is empty),
with Python 3.12.1 and SQLite 3.43.1. The file records the SHA-256 of each source.

Nothing in the capture reaches a model or the nanopublication registry. Do not regenerate the
file after the increment-1 implementation changes these sources: the script refuses, and a file
captured from new code would no longer describe the previous image.
