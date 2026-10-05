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

## Spec round: server mode and saved servers (2026-10-05)

Spec only; no code and no test was changed in this round.

- One app, three ways to run: desktop (default, 127.0.0.1), server (no window, owns the database files, `app.db`, backups and the model), client (the desktop window on a saved server; no local data, no local model) · an organization can share databases without any cloud service · intro, §0.6, §8.10, §9.1
- Principle 6 reworded: the local machine or the organization's own network, never cloud · "local only" no longer described the app · §0.6
- SQLite files are never opened over a network share; one process per data folder, enforced by an instance lock file · SQLite locking is unreliable on network file systems and the maintenance lock lives in memory · §6.3
- Existing tests are never edited; new arguments are optional keywords with defaults that keep earlier tests passing (`locks=None` creates a private `DatabaseLocks`); changes to M1-M3 code happen in the milestone that needs them, tests first · Daniel's rule: a committed test stays the fixed point · §3, §11.1, §15
- `build_services` passes one shared `DatabaseLocks` to the registry, the Executor and the BackupService; `test_wiring.py` asserts it · the lock protects nothing if the three hold different objects · §6.3, §8.1
- Busy timeout (`busy_timeout_s`, 5 s) separate from the query timeout; `SQLITE_BUSY` becomes `DatabaseBusy` with a clear message · "waiting for another writer" and "the query ran too long" are different problems · §6.4
- Executor retries only `BEGIN IMMEDIATE` (`busy_retries`, 2); never a statement that ran, a timeout, a rejection, a `RowConflict` or a permission error; one snapshot, one audit entry · repeating a write silently is never safe · §6.6
- Maintenance lock in two layers: presence check (`DatabaseInUse` with the number of other sessions) and an in-memory exclusive lock that never waits; refuse only, no "disconnect them" yet · a clear early refusal, plus no gap between the check and the file operation · §6.3, §6.7
- `snapshot()` takes no lock itself · its callers already hold one (shared or exclusive), and a second would refuse itself · §6.3
- Server-side `sessions` table; browser storage holds `session_id` instead of `user_id`; a new random id at every login, never one from before the login · a sign-in can be ended from the server; no session fixation · §6.1, §8.1
- Idle timeout 30 min in server mode, off in desktop; "Log out everywhere"; own password change, superadmin reset and user deletion end all of the user's sessions; all sessions cleared at process start, so a server restart signs everyone out · Daniel's answers 3 and additions · §6.1
- Menu wording "Log out everywhere" (not "Sign out") · matches the existing "Log out"; docs/DESIGN.md §7 · §8.2
- `bind_host` and `allowed_hosts` are `Settings` fields; Host and Origin checks are built from them; `*` is refused · nothing about who may reach the app is hard-coded · §3, §8.6
- HTTPS: option A only, an administrator-supplied certificate; server mode does not start without it; no plain-HTTP listener · pywebview has no pinning API (only a global ignore-errors switch), and creating a certificate would need a new dependency; the organization's own certificate needs neither · §8.6 item 10
- Setup guide recommends a company domain name with a certificate for that name, which goes into `allowed_hosts` · the client's OS then trusts it without extra steps · §8.6, §14 item 7b
- Server mode login limit is per client address (5 per 300 s, IPv6 by /64) and the account lock is off; a per-account slow-down (after 20 failures across addresses each further attempt waits 10 s) and one audit warning, never a lock · a per-account lock would let any coworker lock out another on purpose · §6.1
- Accepted limit: the slow-down reveals that an account exists after 20 or more guesses on it, and can delay a coworker's login by 10 s · usernames are known inside an organization; a lock would be worse · §6.1, §10
- Client address comes from the socket; `proxy_headers=False`; reverse proxies unsupported · a header can be faked · §8.6 item 13
- `audit_log.client_addr`, added with `PRAGMA user_version` as the schema version of `app.db` · the audit log should show which machine did what · §6.1
- Server mode has no `/setup` page at all; the first superadmin is created with a terminal command on the server machine (`python -m coalescedb.admin_cli create-superadmin`): password typed without echo, never an argument or environment variable, works only while `app.db` has no users, audited as `bootstrap`; built in M24 as a module entry point, wired into the launcher at M11 · a setup page on the network lets the first person to open a new server become its superadmin, and a loopback-only page would have needed `localhost`, which the company certificate does not name · §8.1, §9.1, §10 S45
- Server mode has no "Quit CoalesceDB", stores reduced transparency per user, and limits download and benchmark buttons to superadmins · found while re-reading §8 for "coworker with an account": each was something one user could do to everyone · §8.2, §6.22
- Server mode refuses a non-loopback `ollama_host`; the sidecar always binds 127.0.0.1 · the model's port has no login · §3, §9.2
- Model requests: Ollama's `OLLAMA_NUM_PARALLEL` set by the sidecar AND our own `LLMQueue` (same number of slots, cap of 10 waiting, one request per user, visible position, cancel) · Ollama's queue is invisible, 512 deep, has no per-user limit and is not ours when the administrator runs their own Ollama · §6.10
- One setting, `llm_parallel_server` (3), for Ollama's parallel value, the queue slots and the benchmark · three numbers that must agree · §3
- Analytics: `max_analysis_jobs` (2) at once, one per user, a queue of 10 · a few people must not use every CPU core · §6.24
- The desktop client is the only supported client; a browser works but is unsupported and no security depends on blocking it (Daniel's answer 1) · login, roles and the Executor protect the data for any client · §8.10, §10
- Saved servers ("connection profiles"): name, https-only address, optional username; never a password; per OS user in `servers.json` on the client; "Test connection" must pass before saving · like saved sessions in MobaXterm or Royal TSX · §8.10
- The saved username is a reminder next to the server name; the login box is not prefilled · prefilling needs the name in the URL or a page script · §8.10
- Client window: no pywebview bridge (`js_api` never set, nothing exposed), navigation locked to the chosen server's origin, `IGNORE_SSL_ERRORS` never set · a compromised or fake server page must have no way into the client machine · §8.10, §10 S47
- The no-bridge rule is the security boundary; the navigation lock is a second layer. If pywebview cannot refuse a navigation, sending the window back to the server's origin after it lands elsewhere is an acceptable fallback; stop and ask only if neither is possible · a page in the window has no way into the local process whichever page it is · §8.10, §15 (M24)
- The `js_api` / `.expose(` / `IGNORE_SSL_ERRORS` source scan is its own file, `tests/security/test_client_window_scan.py`, created at M24 · M24 is built before M17, where `test_no_code_execution.py` arrives · §11.1, §15 (M24)
- Limit of this version: switching server means closing and reopening the window, unless M24 finds that pywebview's window menu can offer "Switch server…" · a server page cannot send the window back to the local start screen · §8.10, §15 (M24)
- Trust stores: on macOS Python's `ssl` usually does not use the Keychain while the webview does; expected fix is the `truststore` package (PyPA, MIT), to be approved at M24, not added now · "Test connection" must agree with what the window accepts · §15 (M24)
- New milestone M24 "Server and client mode", built right after M22; from M24 on every later milestone must also work in server and client mode · later features are then built into an app that already has both modes · §15
- Multi-user groundwork is split: locks, busy handling and retry rules in M5; sessions, hosts from settings and the presence check in M6; queue in M7; ladder and parallel benchmark in M15; analytics cap in M17; service setup and installer in M11/M12 · each piece is built where the code it changes is built · §15
- Server ladder: `qwen2.5-coder` 7B, then 1.5B, then 0.5B (`model_ladder_server`); desktop keeps 1.5B, 0.5B; desktop "try 7B first" is a superadmin switch, off by default · a server usually has the memory, and one model serves several people · §3, §6.22
- Code gets a ladder only through `settings.ladder_for(task="sql")`; `model_ladder_server` is "the server variant of a task ladder" · **dependency:** a pending spec round adds per-task ladders ("sql", "documents", "language"), Qwen3 candidates and LoRA adapter options for M20; these names were chosen so that round adds fields and task names without renaming anything · §3
- `KNOWN_MODEL_SIZES` in exact bytes from the registry manifests: 7B 4,683,074,048; 1.5B 986,048,576; 0.5B 397,808,000 · the old `986 * 1024 * 1024` and `397 * 1024 * 1024` overstated both by about 5% (Ollama's "MB" is decimal), and the RAM rule uses these numbers · §6.22
- Server-mode benchmark sends 3 requests at once and measures speed by the clock; every request must reach `benchmark_min_gen_tps` · Ollama's `eval_duration` leaves out time spent in its queue, so a one-at-a-time Ollama would look fast · §6.22
- RAM rule: 1.5 × file size + (slots − 1) × context memory per slot; the per-slot values (7B 470 MB, 1.5B 235 MB, 0.5B 100 MB at 8192 context) are calculated, not measured, and marked provisional until M15 · parallel requests each need their own context memory · §6.22
- `app_settings` key/value table in `app.db` at M15: fixed key allowlist, superadmin only, every change audited (Daniel's answer 6) · `Settings` is frozen at startup; a switch a superadmin flips needs a home · §6.22
- Evals compare 7B, 1.5B and 0.5B; the 7B run is required (the development Mac has 16 GB); M10 also checks current Apache-2.0/MIT coding models on Ollama; ladder order follows measured results; every ladder model has its licence in §1 · the order should come from numbers, and the app is distributed · §1, §11.2, §15 (M10)
- Windows service wrapper (NSSM or `pywin32`) is decided at M11 (Daniel's answer 7) · it would be a new dependency · §15 (M11)
- Workflow update to "From M5: port check (127.0.0.1 only)": from M24 the port and offline checks are per mode (desktop: 127.0.0.1 only; server: only the configured address and port, HTTPS only) · the earlier line stays as written; this entry extends it · §15 (M24)
- §13: "multi-machine or network sharing" removed; cloud hosting, internet exposure, reverse proxies, SSO/LDAP, more than one server, a browser as a supported client and "disconnect them" added · the first is now in scope, the rest are stated limits · §13

## Spec round: models, per-task ladders and Ollama bundling (2026-10-05)

Spec only; no code and no test was changed, and nothing was installed.

- Three AI tasks, "sql", "documents" and "language", each with a desktop and a server ladder, read only through `settings.ladder_for(task)` · a model that is best at SQL need not be best at reading documents or rewording · §3, §6.10
- The per-task ladder fields default to None, meaning "use the sql ladder of that mode" · behaviour is unchanged until an eval decision changes a default · §3
- `ladder_for`, `model_ladder_server` and the per-task fields are added at M7, not M15 (supersedes the milestone given in the server-mode round; the benchmark that walks the ladders stays in M15) · the client needs a model per task and mode as soon as it exists · §3, §15 (M7)
- `try_7b_first` / `desktop_try_7b` keep their names and now mean "put the first model of that task's server ladder first" · no rename; with today's ladders that model is the 7B · §3, §6.22
- `AIStatus` holds `active_models` (task to model) from M15; `active_model` stays as a read-only property for "sql" · no test builds an `AIStatus` and nothing reads it before M15, so no test is edited · §5, §15 (M15)
- A task that is off never borrows another task's model · the gating table and the evals are per task · §6.10, §6.22
- The benchmark tests each distinct model once and reuses the result for every task that uses it; its prompt stays the text-to-SQL prompt · it measures the computer's speed with a model, not task quality · §6.22
- Download consent lists each distinct missing model once and the total size · a user should see the whole download before agreeing · §6.22
- Ollama may keep 1 or 2 models loaded, decided per computer: 2 only if the tasks use two distinct models and free RAM covers both; a model that passes its own RAM rule is never disabled because two do not fit together (supersedes the fixed `OLLAMA_MAX_LOADED_MODELS=1` of the server-mode round) · Daniel's answer: no reload delay where memory allows, no lost feature where it does not · §6.22, §9.2
- That choice is made before the sidecar starts, from the per-task choice stored in `benchmark.json`; with no stored choice it is 1, and two-at-once applies from the next start · the sidecar starts before the benchmark has chosen models, and it is not restarted mid-session · §6.22
- With an Ollama the app did not start, the RAM rule adds the models together and the status panel says so; `COALESCEDB_OLLAMA_MANAGED` tells the app which case it is in · the app cannot set that Ollama's limit · §3, §6.22, §9.1
- A model switch is shown as "Loading model" only when it takes longer than `model_switch_notice_s` (3 s, provisional until measured at M15) · a short reload is not worth a message · §6.22
- Feature gating is by task and model; each task is on or off on its own · one weak task should not switch off the others · §6.22
- Qwen3 1.7B, 0.6B (desktop) and 8B (server) are eval candidates next to `qwen2.5-coder` 1.5B, 0.5B and 7B; candidates, not replacements · the ladders change only on measured results · §1, §11.2
- The newer Qwen generation found installed on the development Mac (`qwen3.5:9b`) and other Apache-2.0/MIT models join the M10 candidate check under the same licence and pinning rules (widens the earlier "coding models" wording) · Daniel's instruction; a general model can win "documents" or "language" · §11.2
- Before a candidate is evaluated: licence (Apache-2.0 or MIT only), pinned tag and exact size go into the §1 table; explicit `-q4_K_M` tags when they exist, otherwise the default tag with its reported quantization · the app is distributed, and a tag's contents must not change under its name · §1, §11.2
- Recorded today: `qwen3:8b` is Apache-2.0 and 5,225,374,496 bytes (Q4_K_M); `qwen3:1.7b` is 1,359,279,776 and `qwen3:0.6b` 522,640,096 bytes, licence and quantization of those two to confirm at M10 · sizes from the registry manifests; only the 8B page was read · §1, §6.22
- Thinking is switched off: `"think": false` on `/api/chat`, sent only for models whose `/api/show` lists the `thinking` capability · thinking text costs time and breaks SQL and JSON parsing; what Ollama does with the field on other models is unchecked (M7 check) · §6.10
- How Ollama exposes it was checked in its documentation and on the installed 0.33.2 (`/api/show` capabilities); that `think: false` silences Qwen3 itself is not verified, because no Qwen3 model is installed: an M10 check with a STOP · nothing may be installed in a spec round · §6.10, §15 (M10)
- Safety net `strip_think_blocks`: every `<think>...</think>` block is removed before `strip_code_fences` or JSON validation; a response that is only a think block counts as empty; `message.thinking` is never used, shown or logged · a switch can fail or be missing in a model's template · §6.9, §6.10, §11.1
- Temperature per model in `model_temperatures`, default still 0; the value used is stored in `LLMCall.temperature` and written with each eval result · some models are tuned for a non-zero temperature, and results must say what was used · §3, §5, §11.2
- M10 decides "sql" and "documents" for both ladders; "language" shares the sql model until M19, where its evals exist and the same rule is applied · explanations and chart requests are built at M19 and M16, after M10 · §11.2, §15 (M10, M19)
- A task gets its own model only if it beats the shared model by at least 5 points on that task's suite and passes `benchmark_min_gen_tps` on the eval machine; otherwise all tasks share one · one download, one loaded model, less memory, no switching · §11.2
- Ladder order in `Settings` is set from measured results by a spec change, each logged here with its numbers · the order should come from evidence · §11.2, §6.22
- The fine-tuning base is chosen after the M10 evals (currently Qwen2.5-Coder-1.5B-Instruct) · it should be the model the desktop sql ladder actually starts with · §16.1
- M20 compares three ways to ship the fine-tune per task: (a) one combined model, (b) two LoRA adapters through `ADAPTER`, (c) two merged models · the cheapest option that keeps the eval gain wins · §16.5
- Found for (b): Ollama's documentation (v0.33.2) lists Safetensors adapters only for Llama, Mistral and Gemma, not Qwen2; GGUF adapters are accepted with no architecture list; it advises against QLoRA adapters · so (b) is unproven for this base; four checks at M20, and (b) is dropped, not worked around, if one fails · §16.5, §15 (M20)
- Adapters are trained on the exact runtime base (the Instruct variant); the app never loads PyTorch, `transformers` or `peft` · a mismatched base gives erratic output; all inference stays in Ollama · §16.5
- If a separate documents model or adapter ships, the SQL dataset drops its extraction share · each fine-tune then trains only on its own task · §16.2, §16.5
- `model_store` registers whichever option ships and each task's ladder names its model (`finetuned_ladder`, new `finetuned_models`) · Daniel's text cited §6.21 for `model_store`; it is §6.22, which is what the spec uses · §3, §6.22
- Bundled Ollama includes GPU libraries: CUDA and ROCm on Windows, CUDA on Linux with ROCm as a separate variant, Metal through the standard macOS binary; no drivers are bundled · most users cannot install GPU runtimes themselves · §9.2
- A missing or unusable GPU is never an error: Ollama runs on the CPU and the benchmark picks a model or disables AI · the app must start on any machine · §9.2, §6.22
- Before building at M11: the redistribution terms of the bundled GPU libraries are read and recorded (STOP if one may not be redistributed) and the supported GPUs are listed for the README · those libraries are not under Ollama's MIT licence, and the terms were not verified in this round · §9.2, §15 (M11)
- M11 records each build's size (unpacked, compressed, five largest packages, bundled Ollama separately) in `packaging/SIZES.md` · seen today: Ollama's archives are about 1.4 to 1.5 GB on Windows and Linux before the app is added · §9.3, §15 (M11)
- Linux is a server-only build at M11: headless, no pywebview, a systemd unit and a Docker image with CUDA and ROCm tags; desktop stays Windows and macOS · Daniel's answer: servers commonly run Linux; a Linux desktop needs its own checks · §9.3, §9.4, §13
- Server mode is tested on Linux from M24: M24 adds a minimal `ci.yml` (ubuntu, ruff, pytest) and M12 extends it (supersedes "CI workflows are M12" for that one job) · Daniel's answer: not only at M11 · §11.3, §15 (M24, M12)
- §13 gains "Linux desktop build" and "download GPU support on demand" · both are possible later; neither is planned · §13

## M4: SQL guard and role permissions

- `sqlglot==30.21.0` pinned; every string in `test_sql_guard.py` was parsed with it before the tests were locked · sqlglot's parsing and class names change between versions, and the tests are the contract · §1, §6.2, `requirements.txt`
- `validate(sql, role, known_tables, *, max_sql_chars=10_000)`; the default equals the `Settings` default (a test compares them) and the Executor always passes `settings.max_sql_chars` · the guard has no way to reach a settings object, and a keyword keeps it a plain function · §6.2 step 1, §6.6 step 2, M5 row of §15
- On every rejection `normalized_sql` is `None`; tested for every rejected string · rejected SQL can never be run by mistake · §6.2
- A parse failure is any sqlglot error (`ParseError` and `TokenError`, e.g. an unclosed quote) or a `RecursionError`; the reason shown is a fixed sentence · sqlglot's error text can quote the SQL; sqlglot itself runs out of recursion at about 40 nested calls or brackets, so deeper SQL is refused, not crashed on · §6.2 step 2
- The `sqlglot` logger is set to ERROR when the guard is imported · its "unsupported syntax" warning contains the SQL text, which must not reach logs · §6.2, CLAUDE.md
- A CREATE with any sqlglot property is refused, not only the known ones · TEMP / TEMPORARY, VIRTUAL ... USING and STRICT are all properties in this version and no allowed statement has one, so an allowlist also covers properties a later version adds · §6.2 step 4
- `CREATE TEMP VIEW` is refused like a temp table · same reason as temp tables · §6.2 step 4
- The WITHOUT ROWID check has no test that isolates it · sqlglot 30.21.0 cannot parse WITHOUT ROWID and falls back to `exp.Command`, which step 4 rejects first. **Re-check on any sqlglot upgrade** · §6.2 step 4, §11.1
- Known limitation: `ALTER TABLE ... ADD COLUMN` without a column type is refused · sqlglot 30.21.0 cannot parse it; the safe direction, and the designer always writes a type. No test. Re-check on any sqlglot upgrade · §6.2, §6.29
- "CREATE of any other kind" is tested with `CREATE SEQUENCE s`, and a real `CREATE TRIGGER` is rejected at step 3 · a trigger body always contains `;`, so sqlglot sees two statements, and it never yields a CREATE of kind TRIGGER · §6.2 steps 3 and 4, §11.1
- Step 7 also refuses tables and function calls whose name starts with `pragma_` or `sqlite_` (lower-cased, with or without quotes or a schema prefix); the only exception is reading `sqlite_master` / `sqlite_schema` in a SELECT. `SELECT sqlite_version()` is refused, no exception · `pragma_table_info('t')` and `sqlite_dbpage('main')` are table-valued functions that sqlglot parses as ordinary calls inside a SELECT · §6.2 step 7
- The authorizer already refuses the `pragma_` table functions for both roles; `sqlite_dbpage` is blocked only by the guard, because it is absent from this SQLite build (3.53.1). **Re-check with the packaged SQLite at M11** · two layers for `pragma_`, one for `sqlite_dbpage` · §6.2 step 7, §6.4
- Function names are read from each call as it renders for SQLite, lower-cased, as well as from sqlglot's parsed name; this applies to `FORBIDDEN_FUNCTIONS` and to the `sqlite_` / `pragma_` rule · sqlglot turns some calls into nodes of their own (`sqlite_version()`), and the rendered form is what runs · §6.2 steps 6 and 7
- To read a call's rendered name, the guard renders the call alone with its arguments replaced by NULL · rendering every nested call in full took 13 s on an 8 KB chain of ANDs; this way it takes 0.05 s · `security/sql_guard.py`
- ALTER TABLE is destructive unless every action is ADD COLUMN · DROP COLUMN and both renames are destructive by the spec; an action the guard does not know counts as destructive too · §6.2 step 9
- The unknown-table warning compares names without case and skips CTE names, `sqlite_master` / `sqlite_schema`, the name in DROP INDEX / DROP VIEW and a rename's new name · none of those is a missing table; SQLite treats table names as case-insensitive · §6.2 step 8
- `test_sql_guard.py` asserts outcomes (`allowed`, `kind`, `is_destructive`, `reasons`, `normalized_sql`), never sqlglot class names, and runs the re-rendered builder and designer SQL on a real SQLite file · the tests stay valid when sqlglot renames a class, and prove the SQL that runs does what was asked · §11.1, §6.27, §6.29
- Break-a-rule exercise: 13 rules weakened one at a time on a throwaway branch (size, one statement, CREATE properties, DROP kinds, viewer permissions, write inside SELECT, forbidden functions, rendered names, `_app_`, `sqlite_master` writes, rename and DELETE destructiveness, `normalized_sql` on rejection); every one made tests fail; branch deleted · proves the tests catch a weakened rule · §15 (M4)
- A schema prefix must be absent or `main` (any letter case) on every table, view and index reference; three-part names are refused · `CREATE TABLE temp.t2 (a)` creates a temporary table without the TEMP keyword that step 4 looks for; the authorizer also refuses it, so this is a second layer · §6.2 step 7, `test_sql_guard_names.py`
- The `_app_` / `sqlite_` / `pragma_` rule applies to index and view names as well as tables · `CREATE INDEX _app_i ON t (a)` passed the guard before · §6.2 step 7
- `dbstat` is reserved as a whole name, as a table and as a function call, in any letter case · it lists every table's pages and sizes, `_app_` tables included. **The authorizer does not refuse `dbstat`** (it ran for viewer and admin on SQLite 3.53.1), so the guard is its only layer · §6.2 step 7, M11 row of §15
- Known limitation: any schema prefix on the index name in `CREATE INDEX` is refused, `main` included; found from sqlglot's token stream (a dot between INDEX and ON), not from the parsed tree · sqlglot 30.21.0 reads `CREATE INDEX main.i ON t (a)` as an index named `main` on a table named `i` and cannot re-render it. Re-check on any sqlglot upgrade · §6.2
- Known limitation: SQL nested deeper than sqlglot can parse (about 40 nested calls or brackets, about 90 subqueries) is refused with an ordinary rejection · sqlglot runs out of recursion; the builder's largest query must pass the guard, checked at M22 · §6.2, M22 row of §15
- Accepted: the guard's re-render changes some function names, for example `substr` becomes `SUBSTRING` and `IFNULL` becomes `COALESCE`; users see the changed name in Show SQL · both forms mean the same in SQLite; `SUBSTRING` needs SQLite 3.34 or later, re-checked for the packaged SQLite at M11 · §6.2 step 10, M11 row of §15
- Rules found after `test_sql_guard.py` was locked are tested in a new file, `tests/security/test_sql_guard_names.py` · existing test files are never edited · §11.1
