---
name: make-pages-interactive
description: Turn a directory of static HTML pages into a live commenting surface. Injects a feedback library, starts a tiny server, and routes user comments into a JSONL inbox that the agent monitors and responds to by editing the pages. Trigger phrases — "make this page interactive", "make these pages interactive", "let me comment on this page", "add feedback to these pages".
---

# Make Pages Interactive

Turns any folder of HTML files into a place the user can leave inline comments on (text selections, element selections, page-level notes). Comments POST to a local JSONL inbox; you (the agent) Monitor that inbox, edit the HTML in response, append to `feedback/history.json`, and the page auto-reloads with a walkthrough of what changed.

## When to invoke

User says any of:
- "make this page interactive" / "make these pages interactive" → **Setup flow**
- "add feedback to this page" / "let me comment on this page" → **Setup flow**
- "set up feedback on <dir>" → **Setup flow**
- "stop the feedback server" / "kill the server" / "shut it down" → **Stop flow**
- "remove the feedback layer" / "make pages static again" → **Removal flow**
- "update the make-pages-interactive skill" → **Update flow**

## Setup flow (when user wants to make pages interactive)

1. **Identify the target directory.** Usually the user's current working directory or a folder they named. If ambiguous, ask.
2. **Inject the feedback tags** into every `*.html` in that directory:
   ```
   python ~/.claude/skills/make-pages-interactive/scripts/inject.py <dir>
   ```
   Add `--recursive` if the pages live in subfolders. The script is idempotent — safe to re-run. It also creates `<dir>/feedback/inbox.jsonl` and `<dir>/feedback/history.json` if missing.
3. **Pick a port.** Default 5050. Before starting, check what's there:
   ```
   curl -s --max-time 2 http://localhost:5050/info
   ```
   - JSON with `artifact_dir` matching this `<dir>` → reuse it, skip to step 5.
   - JSON with a *different* `artifact_dir` → port is held by another exploration. Either ask the user to free it (`lsof -ti:5050 | xargs kill`) or use port 5051, 5052, … (try the next port; tell the user the URL).
   - No response → port 5050 is free.
4. **Start the server in the background** via Bash with `run_in_background: true`:
   ```
   python ~/.claude/skills/make-pages-interactive/lib/server.py <dir> --port <chosen>
   ```
   The server auto-shuts-down on parent death or 10 min of idle, so you don't need to manage its lifecycle.
5. **Tell the user the URL.** For example: `http://localhost:5050/index.html` (use whatever filename they actually have — `index.html`, `report.html`, etc.). If they have multiple pages, list the top-level ones.
6. **Start a Monitor on the inbox** so new comments notify you immediately:
   ```
   Monitor on path: <dir>/feedback/inbox.jsonl
   ```
   Do NOT poll — let the Monitor notification arrive.

## Live app flow (when the user gives a running app's URL)

Trigger: the user invokes the skill with a URL pointing at an already-running app (e.g. "make http://localhost:5173 interactive", "let me comment on this running app <url>"). This uses the instrumenting reverse proxy (`lib/proxy.py`), NOT the static server — there are no HTML files to inject into; the app is live.

1. **Classify the URL.** `localhost` / `127.0.0.1` / `*.local` → LOCAL (you may edit the app's source in response to comments). Anything else → REMOTE (annotation/discussion only; do NOT edit source you don't own).
2. **Choose a proxy port.** Default 5199. Probe it first:
   ```
   curl -s --max-time 2 http://localhost:5199/__cf/info
   ```
   - JSON with the same `target` → reuse it; skip to step 5.
   - JSON with a *different* `target` → use 5200, 5201, … (try the next port; tell the user the URL).
   - No response → 5199 is free.
3. **Choose a feedback dir.** In a repo: `<repo>/.tmp-shots/cf-live/<host-port>/feedback` (gitignored). For a remote URL with no repo: a temp dir under the system tmp.
4. **Start the proxy in the background** via Bash with `run_in_background: true`:
   ```
   python ~/.claude/skills/make-pages-interactive/lib/proxy.py <target-url> --port <chosen> --feedback-dir <dir>
   ```
   It auto-shuts-down on parent death or 10 min of idle (same as the static server).
5. **Tell the user the proxy URL to open** instead of the original — e.g. `http://localhost:5199/` mirrors the target with commenting enabled. Deep routes work too: `http://localhost:5199/<same path as the real app>`.
6. **Start a Monitor on the inbox:** `<feedback-dir>/inbox.jsonl`. Do NOT poll — let the Monitor notification arrive.
7. **Respond to comments.**
   - LOCAL → map the commented route to the app's source (e.g. `localhost:5173` → the Vite/Next project serving it) and edit the source. HMR reloads the real UI so the user sees the actual result.
   - REMOTE → annotation/discussion only.
   - Either way, **append** a new batch object to `<feedback-dir>/history.json` (directly in the feedback dir — not a nested `feedback/` subdir). Schema matches the static flow (see "Responding to a feedback batch" below). Each `changes[]` entry names the touched file(s) + a description. **Lite mode: OMIT the `anchor` field and do NOT add `data-cf-change` attributes to source.** The page's "processing… → done" banner clears when a `change.in_response_to` matches a submitted comment id, and the History panel lists the changes.

### Bookmarklet fallback (strict-CSP remote sites that won't render through the proxy)

If a remote site refuses to render through the proxy, start the proxy anyway (for its `/__cf/*` endpoints) and give the user this bookmarklet to run on the real site. The feedback endpoints send `Access-Control-Allow-Origin: *`, so cross-origin works:

```
javascript:(function(){window.__CF_CONFIG={feedbackUrl:'http://localhost:5199/__cf/feedback',historyUrl:'http://localhost:5199/__cf/history.json',markSeenUrl:'http://localhost:5199/__cf/mark-seen'};var l=document.createElement('link');l.rel='stylesheet';l.href='http://localhost:5199/__cf/lib/feedback.css';document.head.appendChild(l);var s=document.createElement('script');s.src='http://localhost:5199/__cf/lib/feedback.js';document.body.appendChild(s);})()
```
(Replace 5199 with the actual port.)

## Responding to a feedback batch

When a new batch arrives in `inbox.jsonl`:
- Read the entry. Each comment has a stable `cf_id` and a selector pointing to the exact element/text the user commented on.
- Edit the relevant HTML files to address each comment. Wrap each modified region with `<span data-cf-change="ch-<short-slug>">…</span>` (or add `data-cf-change` to an existing wrapping element) so the post-reload walkthrough can find the change. One anchor per change.
- **Append** a new batch object to the end of `<dir>/feedback/history.json` (newest = last; the library walks from the end to find the latest batch). Schema:
  ```json
  {
    "batch_id": "b-<timestamp-or-slug>",
    "timestamp": "<ISO 8601>",
    "comments": [ /* echo back the inbox comments you addressed */ ],
    "changes": [
      {
        "id": "ch-<slug>",
        "in_response_to": ["<cf_id from inbox>"],
        "anchor": "ch-<slug>",   // must match a data-cf-change in the HTML
        "title": "short, concrete",
        "description": "longer prose (hidden in UI, just for the record)"
      }
    ]
  }
  ```
- The page polls `history.json`, sees the new batch, auto-reloads (scroll position preserved), and offers the user a walkthrough of the changes. The "processing…" banner clears automatically when any `in_response_to` matches a submitted comment id.

## On startup in a directory that already has feedback

If you find `<dir>/feedback/inbox.jsonl` and `<dir>/feedback/history.json` and the skill has been invoked in this session:
1. Scan inbox for comment ids.
2. Scan history's `changes[*].in_response_to` union — those are already processed.
3. If unprocessed comments exist, tell the user the count and ask whether to process now.
4. Either way, set up the Monitor on the inbox.

## Stop flow (user wants to kill the server)

1. Identify the port. If you started the server in this session, you know it. Otherwise check `curl -s http://localhost:5050/info` (try 5051, 5052 if 5050 returns nothing or a different artifact). For the live-app proxy, use `curl -s http://localhost:5199/__cf/info` (note the `/__cf/info` path — the proxy uses this instead of `/info` for identification).
2. Kill it: `lsof -ti:<port> | xargs kill` (use `kill -9` only if a plain kill doesn't free the port within a few seconds — both the static server and the proxy trap SIGTERM and exit cleanly).
3. Confirm: `lsof -i :<port>` should be silent.
4. If you also started a `Monitor` on the inbox in this session, it will keep watching the file — that's fine, the file just won't get new entries.

Note: in most cases the user doesn't need to manually stop the server or proxy. Both auto-shut-down when (a) the parent process dies (e.g. they close the Claude Code window — within ~5–10 s) or (b) no client requests for 10 min. Manual stop is for the case where they want the port back *right now* in the same session.

## Update flow (user wants the latest lib/)

```
python ~/.claude/skills/make-pages-interactive/scripts/update.py
```
Runs `git pull --ff-only` inside the skill dir. Requires git-clone install (the script tells the user how to re-install if not).

## Removal flow (clean static copy)

If the user wants their HTML back to a clean, server-independent state:
```
python ~/.claude/skills/make-pages-interactive/scripts/inject.py <dir> --remove
```
Strips both tags from every `*.html`. Leaves the `feedback/` directory alone (delete manually if not wanted).

## Files in this skill

```
~/.claude/skills/make-pages-interactive/
├── SKILL.md              # this file (agent-facing)
├── README.md             # GitHub-facing docs (human readers)
├── LICENSE
├── lib/
│   ├── feedback.js       # client library: selection + commenting + tour
│   ├── feedback.css      # styles
│   └── server.py         # stdlib-only HTTP server
└── scripts/
    ├── inject.py         # idempotent tag injection / removal
    └── update.py         # git pull --ff-only
```

## Gotchas

- The injected `<link>` and `<script>` reference absolute paths `/lib/feedback.css` and `/lib/feedback.js`. These resolve through `server.py`, which routes `/lib/*` to the skill's own `lib/` directory. So pages only work when opened through this server — opening the HTML file directly in a browser will silently fail to load the feedback widget (the page itself still renders).
- `history.json` order matters: append (don't prepend). The library walks from the end to find the latest batch for the walkthrough.
- `anchor` values must match a `data-cf-change` attribute actually present in the HTML. Typos here cause "anchor not found" warnings post-reload.
