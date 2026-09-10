# Copilot CLI Bridge

Type `!` in the **Microsoft Copilot** chat box to reach into your own machine —
attach real files, point Copilot at a directory with an extension filter, search
your code, and save its answers back to disk.

```
!cd ~/Projects/myapp          set the working directory
!up src ts,tsx                attach every .ts/.tsx file in src/ to the message
!grep "TODO" src -C 2         search, and drop the hits in the chat box
!ctx                          one-shot project briefing as an attachment
!save Button.tsx              write Copilot's last code block to a file
```

The `!` line never reaches Copilot. It runs locally, and the **result** is what
lands in the message — as pasted text or as a genuine file upload.

```
Copilot page ──▶ content.js ──▶ background.js ──▶ HTTP ──▶ bridge server ──▶ your files
     ▲                                                                          │
     └──── text in the composer, or a real attachment ◀────────────────────────┘
```

## Why it's driven by you, not by Copilot

The original idea was to have Copilot emit `#!run` blocks and let the bridge
execute them. In practice Copilot won't play along — it insists it can't touch
your filesystem and refuses the protocol, even when told the local tool does the
running. So the model is inverted: **you** issue the commands, and Copilot just
receives well-formed context it can actually use.

The `#!run` path still exists but is **off by default** (`!auto on` enables it).
Leave it off: anything that lands in the page, including text Copilot quotes from
a web page, could contain a `#!run` block.

## Firefox only

Chrome and Edge refuse to let *any* extension script `copilot.microsoft.com` —
injection fails with "the extensions gallery cannot be scripted". There's no
workaround from inside an extension, so this runs in Firefox.

## Setup

```bash
cd ~/Projects/pinyridgelabs/scripts/copilot-cli-bridge
./launch-firefox-bridge.sh
```

That one command generates a token if you don't have one, starts the bridge
server on `127.0.0.1:18765`, and launches Firefox with the extension loaded on a
persistent profile (your Copilot sign-in is remembered). Requires Node.js.

If something else already holds that port, the launcher walks forward to the
first free one and writes the port it settled on into the extension's generated
config — so a machine with a conflict needs nothing edited. `BRIDGE_PORT=9999
./launch-firefox-bridge.sh` pins a specific port instead, and fails loudly rather
than wandering if that one is taken.

```bash
./launch-firefox-bridge.sh --server    # just the server, foreground, for debugging
./launch-firefox-bridge.sh --restart   # restart the server, then Firefox
./launch-firefox-bridge.sh --stop      # stop the server
./launch-firefox-bridge.sh --manual    # server only + how to side-load the extension
```

### Behind a corporate npm registry

The launcher uses [`web-ext`](https://github.com/mozilla/web-ext) to load the
extension. It prefers a copy already on disk — your PATH, `node_modules`, the npx
cache, a global install — and only installs one if it finds none, capped at three
minutes so it can't hang.

If that install fails with **403**, npm is reaching `registry.npmjs.org` instead of
your registry. npm reads that from `.npmrc`; `~/.ssh` has nothing to do with it
(SSH keys only apply to git-protocol dependencies):

```bash
npm config set registry https://<your-registry-host>/api/npm/npm/
npm login --registry https://<your-registry-host>/api/npm/npm/
```

Or skip npm entirely — **web-ext is only a convenience**, and Firefox can load the
extension by hand:

```bash
./launch-firefox-bridge.sh --manual
```

That starts the bridge server and prints the steps: open
`about:debugging#/runtime/this-firefox`, click **Load Temporary Add-on…**, and pick
`firefox-extension/manifest.json`. Firefox forgets temporary add-ons on restart, so
repeat after restarting it.

When the Copilot page loads you'll see a panel confirming the connection. Type
`!help` in the chat box for the full command list.

## The commands

`!help` is the authority; this is the shape of it.

**Send files to Copilot**

| | |
|---|---|
| `!up <path\|glob> [ext]` | Attach real files. A directory attaches what's inside. |
| `!pack <dir> [ext]` | Concatenate many files into ONE attachment with a contents list. |
| `!ctx [dir]` | Project briefing: tree, file-type counts, README, manifests, git state. |
| `!changed [base]` | The diff plus the full text of every changed file. |
| `!shot [--full]` | Screenshot — drag to select — and attach it. |
| `!clip` | Paste your clipboard in. |
| `!url <url>` | Fetch a page or localhost API and paste it as text. |

**Read and search locally**

| | |
|---|---|
| `!ls [dir] [ext] [-r]` | List a directory. |
| `!tree [dir] [--depth 3]` | Structure, depth-limited. |
| `!find '<glob>' [root]` | Find files by name. |
| `!recent [dir] [--days 7]` | What you touched lately. |
| `!cat <file> [--lines 40-90]` | Paste a file, or a line range. |
| `!head` / `!tail` / `!stat` | The usual. |
| `!grep <pat> [path] [-i] [-C 2]` | Search. Regex unless `--fixed`. |

**Get things back out**

| | |
|---|---|
| `!save [path]` | Write Copilot's last code block to a file. `--block N` picks another, `--reply` saves the whole answer. With no path it names the file from the content. |
| `!open <path>` | Open it locally. |

**Shell and tools**

| | |
|---|---|
| `!git <args>` | `!git diff`, `!git log --oneline -20` |
| `!claude <prompt>` | Ask local Claude Code — its MCP servers and skills — and paste the answer. |
| `!sh <command>` | Anything not recognised runs as a shell command anyway: `!npm test` |

**Modifiers, on any command**

| | |
|---|---|
| `-q` | Show the result in the local panel only; don't touch the chat box. |
| `--send` | Insert **and** submit to Copilot immediately. |
| `--file` / `--inline` | Force an attachment, or force inline text. |
| `--ext ts,tsx` | Extension filter. A bare `!ls ~/proj ts,tsx` works too. |
| `--max N` | Cap how many files are touched. |
| `--all` | Include `node_modules`, `.git`, build output. |

**Run buttons.** Any code block Copilot writes that is actually shell gets a
**▶ Run** button in its corner. Click it and the command runs on your machine —
multi-line blocks run as written — with the output in the panel and a one-click
*Send output to Copilot*. Blocks that are source code don't get a button, and
destructive-looking commands (`rm -rf`, `sudo`, `git push --force`, `curl | sh`)
turn the button into **⚠ Run anyway?** and need a second click. Adding a button
never runs anything; only your click does.

Other niceties: **↑/↓** recalls previous `!` commands, **Esc** closes the panel,
the chat box turns pink while you're in `!` mode.

## How it decides text vs. attachment

- Short results are pasted into the composer as fenced text.
- Anything over ~12,000 characters becomes a `.md` attachment automatically, so
  you never paste a wall of text into a box that will choke on it.
- `!up` on more files than Copilot accepts per message (10) bundles them into one
  attachment instead of failing. `--max N` overrides.
- Copilot rejects unfamiliar extensions on upload, so source files are sent as
  `name.ext.txt` with the original path written at the top of the file.

## Security

The bridge runs **anything you type** in the working directory — that's the point,
and it's the same trust level as your terminal. What matters is that nothing else
can drive it:

- The server binds to `127.0.0.1` only and requires a shared token on every
  request. The token lives in `.bridge-token` (git-ignored) and is baked into the
  extension at launch as `firefox-extension/token.js` (also git-ignored).
- The content script only acts on a line **you type** starting with `!`, or on a
  **▶ Run** button you click. Page content can never execute on its own — unless
  you turn `!auto on`, which re-enables the `#!run` scanner. Don't, unless you're
  deliberately driving it that way.
- `!save` writes files. It only ever writes where you point it.
- Activity is logged to `~/.copilot-cli-bridge/bridge.log`, including a `PING`
  line each time the content script connects — useful for confirming it's alive.

## When something doesn't work

Copilot's DOM changes. Type **`!probe`** in the chat box: it reports the composer,
file inputs, drop target, and last-reply elements the bridge can see. That output
is what's needed to fix a selector.

Attachment is tried three ways, in order — assigning to a real `<input type=file>`,
a synthetic paste, then a synthetic drop — and each is verified by watching for the
filename chip to appear, so a change in one path falls through to the next.

If the panel says it can't reach the bridge, the server isn't running:
`./launch-firefox-bridge.sh --restart`.

## Files

```
server/
  bridge-server.js   HTTP server on 127.0.0.1:18765, token auth, /cmd + /run + /ping
  commands.js        every ! command; all parsing and file work lives here
  fsutil.js          path resolution, globbing, walking, filters, formatting
firefox-extension/
  manifest.json      MV2
  background.js      the only part allowed to reach the local server
  content.js         ! command line, panel, composer insertion, file attachment
  bridge-config.js   token + chosen port, generated at launch; git-ignored
launch-firefox-bridge.sh
```

Because every command is resolved server-side, you can test one without a browser:

```bash
curl -s -X POST -H "X-Bridge-Token: $(cat .bridge-token)" \
     -H 'Content-Type: application/json' \
     -d '{"line":"ls server js"}' http://127.0.0.1:18765/cmd
```

## Legacy paths (not in use)

`extension/` + `native-host/` was the Chrome/Edge version, and `userscript/` was a
Tampermonkey version. Chrome and Edge can't script the Copilot domain at all, so
neither is usable; they're kept only for reference. `native-host/host.js` and
`userscript/local-server.js` are superseded by `server/bridge-server.js`.
