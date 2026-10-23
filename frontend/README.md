# Homeo editor panel

The source of the Homeo sidebar panel (design §19): a CodeMirror 6 editor for `.homeo` files
with syntax highlighting, inline errors, entity autocompletion, save and "Explain".

HACS only ships `custom_components/homeo/`, so the panel is bundled into one module,
`custom_components/homeo/frontend/homeo-panel.js`, which is committed. After changing anything
here, rebuild it and commit both:

```sh
cd frontend
npm ci          # once
npm test        # unit tests for the highlighter, statement lookup and diagnostics
npm run build   # writes ../custom_components/homeo/frontend/homeo-panel.js
```

CI checks that the committed bundle matches the source.

- `src/homeo-panel.js`: the `<homeo-panel>` element HA loads (it receives `hass` and `narrow`).
- `src/language.js`: the highlighter (a CodeMirror stream parser mirroring `core/lexer.py`),
  coloured with HA's own code editor theme variables.
- `src/complete.js`, `src/explain.js`, `src/diagnostics.js`, `src/files.js`, `src/styles.js`:
  completion, the Explain pane, error positions, file names, styles.

The panel talks to the integration over websocket commands defined in
`custom_components/homeo/websocket.py` (`homeo/files`, `homeo/files/subscribe`,
`homeo/file/read`, `homeo/file/check`, `homeo/file/save`, `homeo/file/delete`, `homeo/explain`).
