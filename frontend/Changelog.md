# Changelog

## unreleased

### Redesigned interface
* Rebuilt the main, sign-in and admin pages in React + TypeScript with Tailwind CSS and shadcn/ui; Bootstrap is gone
* New flow: a start page to describe a variable, a workspace with the diagram, a component list, a Turtle editor (CodeMirror) and the raw model output, and a publications page
* The diagram keeps the existing layout engine and gets a new look: colours per I-ADOPT component, vocabulary-link and review marks, zoom, keyboard selection
* An inspector replaces the editor overlay for labels, IRIs, descriptions, shapes, constraints, additions and deletions, and has a "checked" mark per component
* Publishing is a three-step dialog (check, ORCID iD, confirm) that shows the result links instead of opening a new tab; the nanopublication metadata can be added from the dialog or the Turtle tab
* Retracting happens from the publications list (or by pasting a reference) and keeps retracted entries, marked as retracted
* Light and dark themes, bundled fonts (no requests to other hosts), drafts kept in this browser; a shared `?ttl=` link offers to restore the draft it replaced
* The sign-in page only returns to same-site addresses after signing in
* `remote.html`, the page URLs, the API requests, the activity events and the stored publications are unchanged

### Bugfixes
* (De)serializing of blank node Entities with Constrains did not keep the proper link
* Labels and descriptions with quotes, backslashes or line breaks are written as valid Turtle
* Descriptions of components, system parts and constraints are read from Turtle again; before, any edit in the diagram also removed them from the Turtle
* Labels are measured and drawn as plain text, so markup in a label (for example from a shared `?ttl=` link) can no longer run on the main page or the embed page (`remote.html`)

### Features
* Allow to change display order of Variable components

### Changes
* Use different hue for individual Entity types


## 0.3.0

* first public release
