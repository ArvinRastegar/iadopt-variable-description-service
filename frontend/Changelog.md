# Changelog

## unreleased

### Redesigned interface
* Rebuilt the main, sign-in and admin pages in React + TypeScript with Tailwind CSS and shadcn/ui; Bootstrap is gone
* New flow: a start page to describe a variable, a workspace with the diagram, a component list, a Turtle editor (CodeMirror) and the raw model output, and a publications page
* The diagram keeps the existing layout engine and gets a new look: colours per I-ADOPT component, vocabulary-link and review marks, zoom, keyboard selection
* An inspector replaces the editor overlay for labels, IRIs, descriptions, shapes, constraints, additions and deletions, and has a "checked" mark per component that can be undone; unsaved Turtle edits can be applied from it too
* Publishing is a three-step dialog (check, ORCID iD, confirm) that shows the result links instead of opening a new tab; the nanopublication metadata can be added from the dialog or the Turtle tab
* Retracting happens from the publications list (or by pasting a reference) and keeps retracted entries, marked as retracted
* Light and dark themes, bundled fonts (no requests to other hosts), drafts kept in this browser; a shared `?ttl=` link offers to restore the draft it replaced
* The sign-in page only returns to same-site addresses after signing in
* The reasoning switch is gone: decompositions always run without reasoning, the default setup (the request still sends `disable_thinking: true`)
* `remote.html`, the page URLs, the API requests (apart from the saved ORCID iD routes and the decomposition record below), the activity events and the stored publications are unchanged
* Every password field has an eye button that shows or hides the password; creating a user or resetting a password asks for it twice and saves only when both match
* An account keeps several ORCID iDs (new `/api/auth/me/orcids` routes): publishing and retracting offer the saved iDs, or take another one that can be saved to the account. This replaces "Remember my ORCID iD on this device"; an iD remembered there is offered until it is saved or used without saving
* Decomposition record (D-037): for a signed-in account the workspace keeps the decomposition's `record_id`, starts a record when Turtle is opened or pasted (`POST /api/records`), and sends one step per applied change with its component changes, per "Looks right" and per Undo (`POST /api/records/{id}/events`), with the browser's device and screen (`client`, also sent with decompose and publish). Anonymous visitors and deployments with authentication disabled send none of this
* "Mark as best", next to Publish and in the review list once every component is checked, marks the version shown, with an optional note of up to 500 characters; components without "Looks right" bring a warning, not a block. Publishing needs the mark and names the record (`record_id`); the dialog offers the mark in place and explains a refusal. The local draft keeps the record for the account it belongs to

* The admin page and its link follow what the backend allows (`capabilities` in `GET /api/auth/me`), not roles. The accounts table shows role, block state, how each account signs in and the last login; each row offers only the actions the rules allow: Block (with a reason), Unblock, Make admin, Remove admin, Edit, Reset password and History. Edits no longer send roles or the active state, and a new account is always an ordinary user
* The admin page lists the accounts that wait for approval first, with their number, and has Approve and Withdraw approval with an optional note; it says by whom and when an account was approved. A waiting account reads "Waiting for approval" in its menu, and why its work is refused, and the page asks again when the person comes back to it. Activity events of an account whose work is not recorded carry nothing of the work. On a phone the admin page's header shows icons only: it was wider than the screen, and dialogs with it
* Signing in with ROHub, where the deployment offers it (`IADOPT_AUTH_MODE` `both` or `oidc`): the sign-in dialog and `login.html` show "Sign in with ROHub", beside the password form in `both` and without it in `oidc`. The page is left for ROHub and comes back to the same place with the work, which this tab keeps meanwhile; where the browser cannot keep it, the page asks first. `login.html` says why a sign-in with ROHub did not work. In `local`, the default, signing in is as before
* Signing out of a session that ROHub started goes on to ROHub's own sign-out. The account menu has a second entry, "Sign out and clear this browser", which also deletes the draft, the remembered ORCID iD and the list of publications from this browser. A sign-out that fails is shown, and nothing is cleared then
* A session that ROHub started sends its CSRF token with every request that changes something; the token is only in memory, never in the browser's storage

### Bugfixes
* (De)serializing of blank node Entities with Constrains did not keep the proper link
* Labels and descriptions with quotes, backslashes or line breaks are written as valid Turtle
* Descriptions of components, system parts and constraints are read from Turtle again; before, any edit in the diagram also removed them from the Turtle
* Labels are measured and drawn as plain text, so markup in a label (for example from a shared `?ttl=` link) can no longer run on the main page or the embed page (`remote.html`)
* The sign-in page only returns to pages of this application, so a crafted `login.html?redirect=` link can no longer send people to another site after they sign in, or at once when they already are; any other address leads to the main page
* Why a decomposition failed comes into view by itself: on a phone the message was below the diagram, out of sight

### Features
* Allow to change display order of Variable components

### Changes
* Use different hue for individual Entity types


## 0.3.0

* first public release
