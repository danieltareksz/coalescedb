# Rules for this project
- PROJECT_SPEC.md is the source of truth, but it is long. For each milestone, read
  §0, §15, and only the sections that milestone names. Don't load the whole file.
- Implement only the milestone I name.
- If the spec seems wrong or conflicts with the installed library version
  (e.g. sqlglot class names), STOP and tell me. Do not improvise.
- Never weaken, skip, or delete a security check or test to make something pass.
  If a test in tests/security/ seems wrong, stop and explain why.
- Never put values or identifiers into SQL with f-strings or string concatenation.
  Values use ? placeholders; identifiers go through quote_identifier().
- Never use exec, eval, or pickle.
- Never log or trace passwords, document text, or query results.
- Do not add dependencies that aren't in the spec without asking me.
- Before saying a milestone is done: run `ruff check .` and `pytest`, and show me the output.
- Explain what you built in plain language. I am learning.