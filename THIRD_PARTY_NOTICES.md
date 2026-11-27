# Third-party notices

## I-ADOPT Visualizer

Parts of `frontend/` come from the **I-ADOPT Visualizer** by Sirko Schindler.

- Source: <https://github.com/SirkoS/iadopt-vis>, [doi:10.5281/zenodo.18097903](https://doi.org/10.5281/zenodo.18097903)
- Licence: [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/) (CC BY 4.0). It comes with no
  warranties.
- These parts stay under CC BY 4.0. The MIT licence in [LICENSE](LICENSE) does not apply to them.
- Changes: the interface was rebuilt in React and TypeScript (commit `71120a3`, 24 October 2026) around the layout engine and the
  model; export, parsing and writing of Turtle and JSON-LD were adapted.

Compared on 25 November 2026, commit `96f2c4b` of this repository, with commit `639b62a` (18 August 2026) of the source, by path below
`frontend/`:

**Unchanged (43 files):**

- `.`: `ReadMe.md`, `eslint.config.js`, `pnpm-workspace.yaml`, `remote.html`, `remotetest.html`
- `css`: `error.css`, `svg.css`
- `src`: `config.js`, `remote.js`
- `src/lib`: `parse.js`, `triggerRedraw.js`
- `src/lib/createLayout`: `equalWidth.js`, `proportionalWidth.js`, `splitText.js`
- `src/lib/editor`: `addActions.js`, `state.js`
- `src/model`: `models.js`
- `src/ui`: `showError.js`
- `test`: `createLayout_layoutText.browser.test.js`, `model_toJSONLD_parseJSONLD.unit.test.js`
- `test/createLayout`: `Width.js`, `splitText.js`
- `test/model`: `_roundtrip.js`, `parseJSONLD.js`, `toJSONLD.js`
- `test/_schema`: `layout.schema.json`, `variable.schema.json`
- `test/_fixture`: `Variable_System_Constraint.js`, `Variable_System_Constraint.jsonld`, `full.jsonld`, `example1.ttl`,
  `example2.ttl`, `example3_asymSys_bnode.ttl`, `issue001.ttl` to `issue010.ttl`

**Changed (17 files):**

- `.`: `Changelog.md`, `index.html`, `package.json`, `pnpm-lock.yaml`, `vite.config.js`, `vitest.browser.config.js`
- `src/lib`: `createLayout.js`, `draw.js`, `export.js`, `extract.js`
- `src/lib/createLayout`: `getTextDims.js`
- `src/model`: `parseJSONLD.js`, `toJSONLD.js`, `toTurtle.js`
- `test`: `createLayout.browser.test.js`, `export_getPNGBlob.browser.test.js`, `extract.unit.test.js`

The other 80 files of `frontend/` do not exist under the same path in the source at that commit.

## I-ADOPT Corpus

The definitions and decompositions in `backend/data/measured/demonstrations-top25.json`,
`backend/data/measured/example-pool.json` and `backend/data/measured/matrix-finder.json` come from the **I-ADOPT
Corpus**, release v2.0.1, by the creators listed in its record.

- Source: [doi:10.5281/zenodo.18101358](https://doi.org/10.5281/zenodo.18101358) (concept DOI), release v2.0.1
- Licence: [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/) (CC BY 4.0). It comes with no
  warranties.
- These parts stay under CC BY 4.0. The MIT licence in [LICENSE](LICENSE) does not apply to them.
- Changes: the variables were derived from the corpus's Turtle files by the I-ADOPT lab's canonical projection (each
  definition with the six fields of its decomposition). `demonstrations-top25.json` holds 25 of them;
  `example-pool.json` holds all 102 with the lab's folds and, not from the corpus, the vector PSNC's
  `Qwen3-Embedding-0.6B` gave each definition (`backend/tools/build_example_pool.py`); `matrix-finder.json` shows all 102
  as the matrix finder's prompt shows them (`backend/tools/build_finder_data.py`).

## I-ADOPT Patterns

The pattern names and definitions, the table of constraint types, and the guidance texts built from them in
`backend/data/measured/matrix-finder.json` come from the **I-ADOPT Patterns**, modelled by Barbara Magagna, Sirko
Schindler, Stuart Chalk and Marvin Friedl.

- Source: <https://github.com/mabablue/I-ADOPT-patterns-playground>, commit `ecca3470bebc90cc70ce6dca9785f2dd3bf7f9c0`
  (2026-11-09)
- Licence: [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/) (CC BY 4.0), see the LICENSE file of
  that repository. It comes with no warranties.
- These parts stay under CC BY 4.0. The MIT licence in [LICENSE](LICENSE) does not apply to them.
- Changes: the I-ADOPT lab read the pattern files and the table of constraint types into JSON, left out their examples
  and issue links, filed pattern 44 (reference area) as a matrix pattern and corrected one decision-tree leaf; the finder's
  prompt lists them, and its guidance texts state a matrix pattern's name and definition.

The pattern tags of the 102 corpus variables that the finder's prompt shows (the `patterns` of each example) were read
by the lab from the issue labels of <https://github.com/mabablue/I-ADOPT-examples-playground> as they were on
2026-11-19. That repository declares no licence.
