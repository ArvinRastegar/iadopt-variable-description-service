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
