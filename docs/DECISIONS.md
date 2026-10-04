# DECISIONS.md

Why the project is the way it is. PROJECT_SPEC.md is the source of truth for what
the app does; if this log and the spec disagree, the spec wins.

Each entry: decision · reason · where it lives. Groups are in build order, newest last.

---

## Before M1: spec v1, v2 features and the fix round

- Local SQLite GUI with a small local model through Ollama; no data leaves the machine · privacy, works offline · §0.6, §1
- Five security layers: login, role from grants (re-checked on every query), sqlglot allowlist guard, SQLite authorizer, read-only connection for viewers · no single layer failing should be enough to cause damage · §0.2, §6.2, §6.4, §6.6
- The model is untrusted: it proposes SQL or JSON only, never code, never permissions · its output is treated like input from a stranger · §0.1, §0.7
- Writes need review and a confirm click; destructive writes also need the typed database name and take a backup first · humans confirm writes · §0.4, §6.6
- PDF import: the model returns JSON only, Python builds the DDL and inserts with parameters · injected text in a document can never become SQL · §6.13, §6.14
- SQL dumps are parsed and rebuilt, never executed · a dump can contain DROP, ATTACH or triggers · §6.19
- `pypdf`, not PyMuPDF · PyMuPDF is AGPL, which conflicts with distributing a closed executable · §1
- `matplotlib` for PNG/SVG/PDF chart files, no `kaleido` · kaleido needs a separate Chrome install, which breaks the executable · §1, §6.23
- Explanations: Python writes every number, the model only rewords; text with an unknown number or a causal claim is discarded · the model must not invent figures · §0.8, §6.26
- Startup benchmark needs 20 tokens/s; tries the 1.5B model, then 0.5B, else AI is disabled; the prompt cache is defeated with a random first line · slow machines stay usable; repeated prompts would measure the cache · §6.22
- Admin authorizer does not deny writes to `sqlite_*` tables · SQLite reports every CREATE/DROP/ALTER as a write to `sqlite_master`, so that rule would block all schema changes; guard step 7 and the PRAGMA deny cover tampering · §6.4
- Authorizer also denies creating and dropping virtual tables · they can run code outside the allowlist · §6.4
- Guard rejects CREATE TEMP/TEMPORARY, VIRTUAL TABLE, STRICT and WITHOUT ROWID · sqlglot parses them as an ordinary table; STRICT would be silently dropped on re-render · §6.2 step 4
- Two quoting functions; `quote_existing_identifier(name, known)` only accepts names read from that database's schema and doubles `"` · imported files keep names like `Order` or `First Name` · §6.8
- Executor is the only class that runs SQL on user databases: `execute`, `execute_many`, `apply_schema`, `dry_run` · role check, guard and audit apply everywhere · §6.6
- Self-correction retries once on a parse error or an `EXPLAIN` dry-run error, never after a permission rejection · a rejected permission must not be retried around · §6.11
- `TableSpec` covers composite primary keys, unique constraints, indexes, multi-column foreign keys and allowed values; `compile_ddl(..., origin="designed"|"imported")` · one deterministic DDL compiler for designed and imported schemas · §6.13
- Column labels and units live in `app.db` (`column_meta`), not in user databases · user databases stay untouched and need no extra write connection · §6.26
- `model_store` registers a GGUF through a blob upload and `/api/create` with the `files` field · current Ollama rejects a `modelfile` field · §6.22
- A fine-tuned model ships only if it beats stock by at least 5 percentage points on text-to-SQL, with no regression elsewhere · otherwise the stock model stays default · §16.6

## Workflow

- Spec changes are committed before the tests, and tests are committed (failing) before the implementation · the tests are fixed before the code they check exists · git log (`Spec:`, `Add ... tests (failing until ...)`, then `M<n>:` commits)
- New tests that would stop a committed test file from importing go into a new file · an import of a name that does not exist yet breaks the whole file · `tests/security/test_db_names.py` (M3)
- From M3, before each milestone commit: `bandit -r src`, `pip-audit`, `/security-review` and a Gemini review against §10 · security scanning of code and dependencies, plus an independent review · `requirements-dev.txt` (bandit and pip-audit pinned), §10
- From M5: canary test, port check (127.0.0.1 only) and offline check · confirms no secrets on disk, no remote access and no network use · §10
- From M6: the docs/DESIGN.md §8 checklist is reported for every milestone with UI work · §15, CLAUDE.md
- Each milestone ends with an annotated tag (`git tag -a mN`) pushed with `--follow-tags` · a fixed point to return to per milestone · git
- Break-a-rule exercise at M4 and M9: on a throwaway branch, weaken one rule and confirm the tests fail; delete the branch · proves the security tests actually catch a weakened rule · §15 (M4, M9)
- Before building the executable, generate a full lock file pinning every package, transitive dependencies included · the packaged app must be reproducible · §15 (M11)
- Anything that must be remembered for a later milestone is written into the spec, in that milestone's §15 row · it has to survive a fresh session · §15 (for example the M6 "check before relying on it" list)

## M1: skeleton, config, errors, models, identifiers

- `Settings` is `@dataclass(frozen=True, kw_only=True)` · a field without a default follows fields with defaults, which a plain dataclass rejects · §3
- All name regexes end in `\Z` and are checked with `.fullmatch()` · `$` also matches before a trailing newline, so `"abc\n"` would slip through · §6.1, §6.8
- `to_snake_identifier` raises `InvalidIdentifier` when nothing usable is left; callers fall back to `column_<n>` / `table_<n>` (1-based), shown next to the original text and highlighted for renaming · the function never invents a name · §6.8, §6.13, §6.15
- Fallback names go through `dedupe()` together with the other new names and the existing tables · a fallback can never collide with a real name · §6.13
- `dedupe` compares case-insensitively and the first occurrence keeps its spelling · SQLite treats `Title` and `title` as the same name; stricter than SQLite (compares all letters, not just A-Z); the safe direction: at worst an unneeded _2 suffix, never a collision · §6.8
- `COALESCEDB_DATA_DIR` replaces `data_dir` in dev and packaged mode · tests must not write into the repo · §3
- `default_model` (first entry of `model_ladder`) is used only until M15; after that `complete()` raises `LLMUnavailable` when no model is given and `AIStatus.active_model` is None; an explicit `model` always wins · one source of truth for the model in use; evals choose their own · §3, §6.10
- Ruff rule UP042 is disabled · the spec defines `Role` and `StatementKind` as `(str, Enum)` · `pyproject.toml`, §5

## M2: AppStore, passwords, AuthService

- New errors `InvalidPassword`, `UserExists`, `UserNotFound`; `login` raises only the generic `AuthError` or `AccountLockedError`; no error message contains a password · login must not reveal whether a username exists · §4, §6.1
- Passwords: minimum 8 characters, maximum 1024; the maximum also applies at login, refused before the user lookup and without hashing, audited as `password_too_long` · Daniel's choice: 8 is easier for non-technical users; Argon2id hashing and the 5-attempt lockout limit guessing. The maximum exists because Argon2 is deliberately slow, so an unbounded password is a cheap way to stall the app · §6.1
- `change_password`: your own needs the current password, and wrong ones count toward the lockout; a superadmin resetting someone else's needs none and clears that user's lockout; anyone else gets `PermissionDenied` · §6.1
- `accessible_databases(user, *, all_databases)`: a superadmin gets every name passed in, as ADMIN; others only their grant rows · AuthService cannot list database files itself (the registry depends on it, not the other way round) · §6.1
- Every `AuthService` method re-loads the actor or user from `app.db` by id · a deleted user or a stale `User` object is refused · §6.1
- `UserNotFound` / `UserExists` are raised only after the permission check · a non-superadmin learns nothing · §6.1
- Lockout: locks after `login_max_failures`; while locked even the right password gets `AccountLockedError`; the lock is not extended; an expired lock restarts the count; success clears it · §6.1
- Known accepted limitation: after `login_max_failures` attempts, a locked account answers `AccountLockedError` while an unknown name answers `AuthError`, so repeated guesses reveal that a username exists. Accepted for a local, single-machine app. · §6.1
- A failed login for an unknown or malformed username is audited as `{"reason": "unknown_user"}`, never the typed text · people sometimes type their password into the username box · §6.1

## M3: registry, connections and authorizer, introspection, snapshot

- `BackupService.snapshot()` and pruning are built in M3, and the service is injected: `DatabaseRegistry(settings, auth, backups)`; `list()` / `restore()` come in M5 · the registry's `delete()` needs a snapshot first · §6.3, §6.7
- `FORBIDDEN_FUNCTIONS` lives in `security/policy.py` as a lowercase frozenset, imported by the authorizer and (from M4) the guard, compared case-insensitively · the two layers can never disagree; the file exists from M3 because the authorizer needs it first · §6.2 step 6, §6.4
- `validate_db_name()` and `WINDOWS_RESERVED_NAMES` (con, prn, aux, nul, com1–9, lpt1–9) live in `identifiers.py` and are used by the registry and AuthService · Windows treats those names as devices; refused on every OS so a database made on a Mac still works on Windows; kept in identifiers because the registry already imports AuthService · §6.8
- New AuthService methods `require_superadmin`, `revoke_all`, `rename_grants` · the registry needs them before it touches files · §6.1
- Stray grants: `create`, `import_file` and rename call `revoke_all` for the new name first · `grant` cannot check that a database exists, so a grant row could be waiting for a name · §6.1, §6.3
- A deleted database's backups move to `backups/_deleted/<name>_<timestamp>/`; `create` and `import_file` also move a leftover `backups/<name>/` there; rename refuses if `backups/<new>/` exists · a new database with a reused name starts with no backups; `_` can never start a database name · §6.3, §6.7
- Delete order: superadmin check, confirm text, snapshot, `revoke_all`, remove the file and its -wal/-shm, move backups to `_deleted` · if the snapshot fails nothing is deleted or revoked · §6.3
- Rename: checkpoint the WAL, record every file move and undo them in reverse on failure, backup folder before grants, grants last · the database ends up fully under the old name if anything fails · §6.3
- `delete` and `rename` are not atomic across files and `app.db` (documented limitation) · no single transaction covers both; the step order keeps the worst case recoverable · §6.3
- `create` removes the new file and its sidecar files if the grant or audit step fails · the name stays free and create can be retried · §6.3
- Connections are opened in autocommit mode (`isolation_level=None`) · callers that need a transaction issue BEGIN/COMMIT themselves, which the Executor relies on · §6.4
- `PRAGMA trusted_schema = OFF` on every user-database connection · SQLite's own advice for files the app did not create; it also refuses forbidden functions inside views (tested) · §6.4
- Viewer connections use `path.resolve().as_uri() + "?mode=ro"`; admin connections use `mode=rw`, never `rwc` · paths with spaces or `#` still open; a missing file raises `DatabaseNotFound` instead of being created · §6.4
- Internal introspection connection: read-only, PRAGMA allowlist `table_info`, `foreign_key_list`, `index_list`, `integrity_check`, plus `data_version`; fixed 30 s timeout · SQLite runs `data_version` internally during `integrity_check` and reports it to the authorizer (checked on 3.53.1) · §6.4
- Authorizer matches the `_app_` prefix case-insensitively and covers views with such a name (review fix) · `_APP_meta` is the same table to SQLite · §6.4
- Admin authorizer denies any create, drop or alter in the `temp` schema (review fix) · `CREATE TABLE temp.x` reaches the temporary schema without the TEMP keyword · §6.4
- `ALTER TABLE t RENAME TO _app_x` is the guard's job, not the authorizer's · SQLite only ever tells the authorizer the old name · §6.4, §11.1 (guard table row, M4)
- Import detects virtual tables by `sqlite_master.rootpage = 0`, with a whitespace-tolerant text match as a second signal (review fix) · a crafted file can disguise the stored SQL text · §6.3
- Imported files with triggers or virtual tables are refused · the authorizer blocks creating them, so they should not arrive by import either · §6.3
- `schema_ddl(path, *, max_chars=6000)` drops row-count comments first, then the least-referenced tables · the project has no tokenizer, so the budget is in characters · §6.5
- `# nosec B608` on exactly two lines in `introspect.py` (row count, sample rows) · both build SQL only from a name quoted by `quote_existing_identifier`; identifiers cannot be bound · `src/coalescedb/db/introspect.py`
- Database-name tests added as a new file instead of being appended to a committed one · see Workflow · `tests/security/test_db_names.py`

## Spec round: NiceGUI, visual builder, docs/DESIGN.md (2026-10-04)

- NiceGUI replaces Streamlit (spec checked against 3.17.1; pin added at M6) · more control over layout and interaction, still all Python; nothing in M1–M3 used Streamlit · §1, §8
- The host is always passed as `127.0.0.1` · NiceGUI's default outside native mode is `0.0.0.0` · §0.6, §8.6
- No `--serve` child process; one entry point `launcher.py`, no `app.py` · NiceGUI runs the server in the main process and opens the pywebview window itself · §9.1
- `run.cpu_bound` is not used; analytics keep their own worker process · it passes work to another process with pickle · §8
- Local-server controls 1 to 7: bind address with a test; Host allowlist (`127.0.0.1`, `localhost`); Origin check on the socket and non-GET requests; cookie `SameSite=Strict` with its own name; server-side upload cap; `storage_secret` of 32 random bytes in `data_dir` (0600); sign-in ends when the app closes (`boot_id`) · NiceGUI checks none of Host, Origin or upload size on the server · §8.6, §10 S28–S34, §11.1
- Accepted limit: programs running as the same OS user, and other OS users reaching the login page · the app does not defend against software already running as the user · §8.6, §10
- `COALESCEDB_BIND_HOST` / `COALESCEDB_PORT` exist only for Docker; any bind host other than `127.0.0.1` shows a permanent status-bar warning; a test checks the default · an env var must not silently undo a security default · §8.2, §8.6, §9.4, §11.1
- Unexpected errors show only the exception class and a trace ID; known errors show the real message and the next step · exception text can contain data; docs/DESIGN.md forbids a generic message · §12, §8.6
- Dark mode is stored per user in `app.db` (`user_prefs`); reduced transparency is per install · NiceGUI's "user" storage follows a browser cookie, not the app user; transparency depends on the computer · §6.1, §8.2
- `AuthService.get_user(user_id)` added; reads `app.db` on every call, returns None for a deleted user · the UI re-loads the user on every page load and action · §6.1, §8.1
- Browser storage holds only `user_id`, `db_name` and `boot_id` · role and user are always re-derived · §8.1
- Layout follows the docs/DESIGN.md shell: sidebar (navigation, databases, schema tree), toolbar (user menu, dark toggle), working area, status bar (row count, timing, model state, consent panel), right inspector; new Data page; no chat transcript · results must stay visible; controls sit next to what they act on · §8.2, §8.3, §8.8
- Query page modes: Build query (default, always available), Generate SQL (model available), Write SQL (advanced); with AI off it opens in Build query · a non-technical user needs no SQL and no model · §8.3, §6.22
- UI strings rewritten to docs/DESIGN.md §7 (for example "Query details", "Simplify wording", "Analyze result") · no long dashes, no exclamation marks, the model is not given a personality · §6.22, §8
- Principle 10: the UI never assembles SQL text; only builder output, SQL typed in Write SQL mode, or SQL the model proposed in Generate SQL mode reaches the Executor · every path gets the same role checks, guard, confirmations, backups and audit · §0.10, CLAUDE.md
- Display rule: data, file, user and model text is shown only through text-escaping components; `ui.html`, `ui.markdown`, `ui.code`, AG Grid HTML columns, renderers and `:`-prefixed options are not used in `ui/` · HTML injection through data · §8, §10 S28
- `ui.html`, `ui.markdown` and AG Grid HTML are banned in `ui/` entirely, enforced by a source scan. Any exception needs a spec change and a test · the strict rule is checkable; a per-case allowance is not · §8, §11.1, CLAUDE.md
- Design skill pbakaus/impeccable considered; not installed. Revisit at M6 (project-level, pinned, read SKILL.md first, mainly for critique). docs/DESIGN.md and the spec take priority over any skill · CLAUDE.md
- Escaping tests use NiceGUI's server-side helper plus a source scan; a real-browser check is to be reconsidered before the first release · a browser test needs a new dev dependency · §11.1
- Query builder (M22): `QuerySpec`; joins only along introspected foreign keys, both directions, at most 4, LEFT JOIN, with a note when a join can repeat rows; fixed operator allowlist; LIKE values escaped; at most 200 columns · §6.27
- Data editor (M23): changes keyed on the primary key with `IS ?` checks on the original values; `RowConflict`; tables without a primary key are read-only; at most 1,000 changes per save; review dialog; snapshot before deletes · a row changed by someone else is reported, not overwritten · §6.28
- `Executor.apply_changes` (built in M5): one transaction, every UPDATE/DELETE must change exactly one row, no table-wide writes · the data editor needs several statements to succeed or fail together · §6.6
- Table designer (M23): create, add column, rename and drop through typed operations; renames stay destructive; the typed confirmation is the database name and the dialog says exactly what to type · guard step 9 already treats renames as destructive and no check is weakened · §6.29
- Build order: M22 right after M6, M23 after M9 · M23 needs `compile_ddl` · §15
- Fonts Geist and JetBrains Mono, bundled as woff2 with their `OFL.txt` at M6 with approval · both SIL Open Font License 1.1, which allows bundling; no font is fetched at runtime · §1, §9.3
- AG Grid Community only · MIT; the Enterprise edition would be loaded from a URL · §1
- Accent colours: `ui/theme.py` registers the light or dark set and calls `ui.colors` again when dark mode changes · `ui.colors` holds one value per role · §8
- M6 row carries the checks to do before relying on them: `NICEGUI_STORAGE_PATH` timing, middleware covering `/_nicegui_ws/`, `ui.codemirror` SQL mode, pywebview private mode, blur rendering in the webview, `dark:` with the pinned version · they could not be verified from documentation alone · §15 (M6)
- docs/DESIGN.md decides look, layout and wording; the spec decides behaviour and security; both win over any design skill or plugin · CLAUDE.md, §8

## Housekeeping: docs/ (2026-10-04)

- `PROJECT_SPEC.md` and `DESIGN.md` moved to `docs/`; `CLAUDE.md` stays at the repo root · Claude Code loads `CLAUDE.md` from the root automatically · §2 file tree
- This decision log is added, appended at the end of each milestone; the spec wins over it · the reasons behind decisions were only in chat history · CLAUDE.md
