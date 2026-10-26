# Homeo editor panel

The source of the Homeo sidebar panel (design §19): a CodeMirror 6 editor for `.homeo` files
with syntax highlighting, inline errors, autocompletion (entities, attributes, event types, services
and their fields in `on` actions, `on(...)` settings), hover documentation (builtins, keywords,
entity states, services, and the current values of `let` and `state` names), save and "Explain".

HACS only ships `custom_components/homeo/`, so the panel is bundled into one module,
`custom_components/homeo/frontend/homeo-panel.js`, which is committed. After changing anything
here, rebuild it and commit both:

```sh
cd frontend
npm ci          # once
npm test        # unit tests for the highlighter, completion, hovers and diagnostics
npm run build   # writes ../custom_components/homeo/frontend/homeo-panel.js
```

CI checks that the committed bundle matches the source.

- `src/homeo-panel.js`: the `<homeo-panel>` element HA loads (it receives `hass` and `narrow`).
- `src/language.js`: the highlighter (a CodeMirror stream parser mirroring `core/lexer.py`),
  coloured with HA's own code editor theme variables.
- `src/complete.js`, `src/hover.js`, `src/docs.js`, `src/explain.js`, `src/diagnostics.js`,
  `src/files.js`, `src/styles.js`: completion, hovers, the builtin and keyword docs both show, the
  Explain pane, error positions, file names, styles.

The panel talks to the integration over websocket commands defined in
`custom_components/homeo/websocket.py` (`homeo/files`, `homeo/files/subscribe`,
`homeo/file/read`, `homeo/file/check`, `homeo/file/save`, `homeo/file/delete`, `homeo/explain`,
`homeo/value`).
