# Rules for this project
- PROJECT_SPEC.md is the source of truth, but it is long. For each milestone, read
  §0, §15, and only the sections that milestone names. Don't load the whole file.
- Implement only the milestone I name.
- If the spec seems wrong or conflicts with the installed library version
  (e.g. sqlglot class names), STOP and tell me. Do not improvise.
- Never weaken, skip, or delete a security check or test to make something pass.
  If a test in tests/security/ seems wrong, stop and explain why.
- Never put values or identifiers into SQL with f-strings or string concatenation
  except as specified below.
  New identifiers created by the app (PDF extraction, spreadsheet import) must be
  validated with `to_snake_identifier()` and `quote_identifier()`. Pre-existing
  schema identifiers (imported SQLite files, SQL dump tables/columns) must pass
  through `quote_existing_identifier(name, known)`.
  Values in user-facing DML queries must ALWAYS be passed as bound `?` parameters.
  For DDL generation in `compile_ddl` (column defaults and `CHECK (col IN (...))`
  lists), values must be safely escaped as SQL literals by doubling single quotes
  or rendered via `sqlglot` expressions. SQLite cannot bind `?` in DDL.
- Never use exec, eval, or pickle.
- Never log or trace passwords, document text, or query results.
- Do not add dependencies that aren't in the spec without asking me.
- Before saying a milestone is done: run `ruff check .` and `pytest`, and show me the output.
- Explain what you built in plain language. I am learning.
- For any milestone with UI work, read DESIGN.md in full, follow it, and report the
  DESIGN.md §8 checklist results before saying the work is done.
- DESIGN.md decides look, layout and wording; PROJECT_SPEC.md decides behaviour and
  security. DESIGN.md and PROJECT_SPEC.md take priority over any design skill or plugin.
  If they conflict with each other, with a skill's advice, or with what NiceGUI supports,
  STOP and ask.
- The UI library is NiceGUI. Never render data, file, user or model text as HTML
  (no ui.html, ui.markdown or AG Grid HTML for such content).
- UI code never builds SQL. What reaches the Executor is only: builder output, SQL the user
  typed in Write SQL mode, or SQL the model proposed in Generate SQL mode. Model-proposed SQL
  is shown to the user, SELECTs run as described in §8.3, and writes always go through the
  review dialog, exactly like typed SQL.