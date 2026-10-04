# DESIGN.md

UI rules for CoalesceDB. Read this file before writing or changing any NiceGUI
layout, component, color, font, or piece of interface text. These rules sit
alongside CLAUDE.md and PROJECT_SPEC.md. If a rule here conflicts with the
spec or with the installed NiceGUI/Quasar version, STOP and tell me. Do not
improvise a workaround.

The target is a fast, dense desktop database tool in the family of TablePlus,
Linear, and native macOS utilities. It should look like it was designed by a
person who uses it every day, not generated from a SaaS landing page template.

---

## 1. Banned outright

Never produce any of the following. No exceptions, no "subtle" versions.

**Visual**
- Purple, violet, or indigo gradients. Any multi-color gradient on buttons,
  headings, text, or backgrounds.
- Neon glows, colored `shadow-*` halos, glowing borders, "aurora" blobs.
- Pill-shaped buttons. Do not use `rounded-full` or the Quasar `rounded` prop on
  buttons, inputs, or chips. (`rounded-full` is allowed only for status dots.)
- Floating bento grids of feature cards.
- AI sparkle icons, magic wands, stars, robots, or brains to represent the
  model. The text-to-SQL feature uses a plain, functional icon (for example
  `terminal` or `code`) or no icon.
- Emojis anywhere in the UI, in labels, in notifications, in log output, in
  code comments, or in commit messages.
- Gradient text (`bg-clip-text text-transparent`).
- Oversized hero banners, splash sections, or marketing-style welcome screens.

**Motion**
- Scroll-triggered animations, parallax, fade-in-on-scroll, staggered card
  entrances, typewriter effects, bouncing or pulsing elements.
- Allowed motion: short functional transitions only (150 ms or less, `ease-out`)
  for hover, focus, panel open/close, and loading states. Respect reduced
  motion: no animation that conveys information by itself.

**Content**
- Fake reviews, testimonials, user counts, star ratings, "trusted by" logos,
  uptime claims, or invented metrics. Every number on screen comes from the
  user's actual data or the app's actual state.
- Placeholder data that looks real. Empty states say the thing is empty.
- Vague hero or header text ("Unlock the power of your data", "Your data,
  reimagined", "Supercharge your workflow"). Headers name the thing on screen.
- Marketing words: seamless, effortless, powerful, supercharge, unleash,
  revolutionize, next-gen, magic, delightful, blazing, AI-powered.
- Em dashes (the long dash) in any UI string, tooltip, notification, or doc
  string shown to the user. Use a period, comma, colon, or parentheses. In-app
  separators use a middle dot (`·`) or a vertical rule.
- Exclamation marks in UI text.
- Unnecessary subheadings. A panel gets one title. Do not add a subtitle or
  tagline under it unless it carries information the title cannot (for
  example the active database path).

---

## 2. Aesthetic: liquid glass and Aero depth

The interface is built from translucent layers over a quiet tinted base.
Depth comes from translucency and hairline borders, not from heavy shadows.

**Layer stack (back to front)**

| Layer | Purpose | Light mode | Dark mode |
|---|---|---|---|
| 0. Window base | App background | `bg-slate-100` with a very faint cool tint | `bg-slate-950` with a faint midnight-blue tint |
| 1. Panels | Sidebar, schema tree, inspector | `bg-white/65 backdrop-blur-md border border-white/40` | `bg-slate-900/60 backdrop-blur-md border border-slate-700/40` |
| 2. Content | Tables, editor, results | `bg-white/80 border border-slate-200/70` | `bg-slate-900/75 border border-slate-700/50` |
| 3. Elevated | Dialogs, menus, context bars, toasts | `bg-white/75 backdrop-blur-lg border border-white/50` | `bg-slate-800/70 backdrop-blur-lg border border-slate-600/40` |

**Rules**
- Elevated surfaces get a specular top edge: a 1px highlight on the top border
  only (`border-t-white/60` in light, `border-t-white/10` in dark).
- Shadows, when used, are small and neutral: `shadow-sm` on layer 3 only. No
  colored shadows. No `shadow-2xl`.
- Glass tint: panels may carry a very faint accent tint (around 2 to 4 percent
  of the accent color) to give the layers life. Never a visible color wash.
- The window base may use one extremely subtle, low-contrast radial tint of the
  slate/marine palette so the blur has something to refract. It must not read
  as a gradient. No purple, no multi-hue blends, no blobs.
- Corner radius: `rounded-md` for controls and inputs, `rounded-lg` for panels
  and cards, `rounded-xl` maximum for dialogs. Table cells are square.

**Performance (required, this app must run on a 2017 ThinkPad)**
- `backdrop-blur` is expensive. Use it only on a handful of large, static
  surfaces (sidebar, top toolbar, dialogs, menus). Never on table rows, list
  items, cells, or anything rendered many times or inside a scrolling list.
- Provide a reduced-transparency mode (setting or automatic fallback) that
  swaps blurred surfaces for solid equivalents (`bg-white`, `bg-slate-900`).
  The UI must look complete and intentional in that mode.

---

## 3. Palette and theming

Both light and dark mode are first-class. Use NiceGUI's native
`ui.dark_mode()`. Every surface, border, and text color must be specified for
both modes.

**Base**
- Dark: deep slate and marine (`slate-950`, `slate-900`, zinc with a blue
  undertone). Never pure `#000000`.
- Light: ceramic and frosted tones (`slate-50`, `slate-100`, cool whites).
  Never pure `#FFFFFF` as the window base (white is fine on content surfaces).
- No flat, clinical grayscale. Neutrals lean slightly cool.

**Accents (semantic, use only for their meaning)**

| Role | Use for | Light | Dark |
|---|---|---|---|
| Primary (cobalt/sapphire) | Primary actions, focus rings, selection, links | `#2563EB` | `#4C82F7` |
| Positive (emerald) | Success, active connection, committed changes | `#059669` | `#34D399` |
| Warning (amber) | Unsaved edits, slow model, schema warnings | `#D97706` | `#FBBF24` |
| Negative (coral/crimson) | Destructive actions, errors, failed queries | `#DC2F3C` | `#F26D6D` |
| Info (slate-blue) | Neutral notices | `#475569` | `#94A3B8` |

Register them once at startup so Quasar components inherit them:

```python
ui.colors(
    primary='#2563EB',
    positive='#059669',
    warning='#D97706',
    negative='#DC2F3C',
    info='#475569',
)
```

- One primary button per view. Everything else is flat or outlined.
- Destructive buttons are never primary-colored and always confirm.
- Color is never the only signal. Pair it with text or an icon (for example
  a failed query shows "Query failed" plus the error, not just a red border).
- Text contrast meets WCAG AA in both modes, including on glass surfaces.
  Check text over the blurred layers, not just over solid ones.

**Dark mode check**: confirm that Tailwind `dark:` variants respond to
`ui.dark_mode()` in the installed NiceGUI version before relying on them. If
they do not, STOP and tell me. Do not paper over it with a hand-written
stylesheet.

---

## 4. Typography

**Banned as the primary UI font**: Inter, Roboto, Poppins, Space Grotesk,
Montserrat, and unstyled browser defaults.

**Use**
- UI text: Geist (first choice), IBM Plex Sans or Plus Jakarta Sans as
  alternates, falling back to the system stack
  (`-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif`).
- Data, SQL, identifiers, column types, row counts, query plans: JetBrains Mono
  (first choice), falling back to `"SF Mono", "Fira Code", ui-monospace,
  monospace`.

**Loading fonts**: the app is local-first and packaged as an executable, so
fonts must be bundled with the app (woff2 files served through
`app.add_static_files`), never fetched from Google Fonts or any CDN at runtime.
Adding font files is adding an asset: ask me before adding them.

**Scale** (desktop, compact)

| Role | Size | Weight | Notes |
|---|---|---|---|
| View title | `text-lg` | 600 | `tracking-tight`. One per view. |
| Section label | `text-xs` | 600 | Uppercase allowed, `tracking-wide`, muted color |
| Body / controls | `text-sm` | 400 to 500 | Default for almost everything |
| Table cells | `text-[13px]` | 400 | Monospace for values, `tabular-nums` |
| Table headers | `text-xs` | 600 | Muted color, column type shown beside name |
| Metadata, hints | `text-xs` | 400 | Muted color |

- Numbers in tables, stats, and status bars use `tabular-nums` and are
  right-aligned.
- Nothing larger than `text-2xl` anywhere in the app.

---

## 5. Layout and information density

Design for a desktop window at 1280x800 and up. The data is the interface.

**Shell**
- Left: collapsible sidebar with connections and the schema tree (tables,
  views, columns with types).
- Top: a single compact toolbar (around 40 px tall) with the active database,
  the main actions for the current view, and the dark mode toggle.
- Center: the working area (data grid, SQL editor, query builder, chart).
- Bottom: a thin status bar with row count, query time, connection state, model
  state. Real values only.
- Optional right inspector for row details, column metadata, or the model's
  explanation of a result.

**Density rules**
- Table row height around 28 to 32 px. Cell padding `px-2 py-1`.
- Panel padding `p-2` to `p-3`. Gaps `gap-1` to `gap-2`. No `p-8`, no `gap-8`,
  no `py-16` sections.
- Show column metadata (type, nullable, primary key, foreign key) in headers or
  on hover, not hidden behind extra clicks.
- Query results must be visible without scrolling past chrome. If a layout
  pushes the first result row below the fold at 1280x800, it is wrong.
- No mobile-style stacked cards for tabular data. Tables are tables.
- No centered, max-width marketing column. Use the full window width.

**Controls sit next to what they act on**
- Search and filter chips directly above the table they filter.
- "Run query" attached to the editor, not floating elsewhere.
- Pagination and export at the bottom edge of the results grid.
- Row actions in a context menu or row hover toolbar, not in a separate panel.

**Empty, loading, and error states**
- Empty: one line stating the fact and the next action ("No tables. Import a
  file or create a table."). No illustrations, no mascots.
- Loading: inline spinner or skeleton rows in place. No full-screen overlays
  for operations under a second.
- Error: the actual error message in monospace, plus what the user can do.
  Never a generic "Something went wrong."

---

## 6. NiceGUI idioms

- Style with Tailwind via `.classes('...')` and Quasar props via
  `.props('...')`. Do not inject inline `style=` strings or separate CSS files
  for things Tailwind or Quasar props can express. The only allowed global
  additions are font-face declarations for the bundled fonts and the theme
  color registration, both in one place at startup.
- Define reusable styles once as constants in a single theme module (for
  example `PANEL`, `ELEVATED`, `TOOLBAR_BTN`) and reuse them. Do not repeat
  long class strings across files.
- Buttons: `.props('flat no-caps dense')` for toolbar actions,
  `.props('unelevated no-caps')` for the one primary action. Never the
  `rounded`, `push`, or `glossy` props. `no-caps` always: Quasar's default
  uppercase buttons are not allowed.
- Inputs: `.props('dense outlined')`. Labels stay visible; placeholders are not
  labels.
- Tables: `ui.table` / `ui.aggrid` with dense props, sticky headers, and
  monospace cells.
- Dialogs: `ui.dialog` with the layer 3 glass style, a title, the content, and
  right-aligned actions (cancel left of confirm).
- Notifications: `ui.notify` with the semantic type (`positive`, `warning`,
  `negative`), short text, no emojis, no exclamation marks.
- Icons: Material Symbols (outlined) at a consistent size, used only when they
  speed up recognition. Icon-only buttons need a tooltip.

```python
# Example: toolbar with contextual actions (illustrative, adapt to the theme module)
with ui.row().classes(
    'w-full items-center gap-1 px-2 h-10 '
    'bg-white/65 dark:bg-slate-900/60 backdrop-blur-md '
    'border-b border-slate-200/70 dark:border-slate-700/40'
):
    ui.label('orders').classes('text-sm font-semibold tracking-tight')
    ui.label('12,408 rows').classes('text-xs text-slate-500 tabular-nums')
    ui.space()
    ui.button('Filter', icon='filter_list').props('flat no-caps dense')
    ui.button('Export CSV', icon='download').props('flat no-caps dense')
    ui.button('Run query', icon='play_arrow').props('unelevated no-caps dense color=primary')
```

---

## 7. Microcopy

Neutral, technical, short. Sentence case. Verb first for actions.

| Write | Not |
|---|---|
| Run query | Run your query now! |
| Table schema | Explore your table's structure |
| Export CSV | Download your data |
| Import file | Bring in your data effortlessly |
| Generate SQL | Ask AI ✨ |
| Explain result | Get AI-powered insights |
| Model unavailable. AI features disabled. | Oops! Our AI is taking a break |
| 3 unsaved changes | You have some unsaved changes! |
| Delete table "orders"? This cannot be undone. | Are you sure? |

- Refer to the local model by what it does ("Generate SQL", "Explain"), not as
  "AI" with a personality. No "I" or "I'm thinking" from the app.
- Say exactly what will happen on destructive actions and name the object.
- Units and counts always included ("1.2 s", "48 rows", "3 columns").

---

## 8. Before you say a UI change is done

Check the change against this list and tell me the result:

1. No item from section 1 appears (search the diff for `gradient`, `purple`,
   `violet`, `indigo`, `rounded-full`, `rounded` prop, `shadow-2xl`, emojis,
   the long dash, `!` in strings).
2. It looks correct in light mode, dark mode, and reduced-transparency mode.
3. No `backdrop-blur` on repeated or scrolling elements.
4. Fonts are the bundled ones, not a fallback by accident.
5. At 1280x800 the primary data is visible without scrolling.
6. Every number on screen comes from real app state.
7. All new UI strings follow section 7.
