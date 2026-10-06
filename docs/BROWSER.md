# Optional browser: Ego Lite

WorkForce can let Claude and Codex use [Ego Lite](https://lite.ego.app/), a browser built for AI agents, to test a locally running web app, check a page, or research. It is off by default, and WorkForce works the same without it.

WorkForce uses Ego Lite's official integration: its `ego-browser` command and its `ego-browser` skill, which Ego Lite installs for Claude Code and Codex itself. WorkForce adds no browser server or wrapper of its own. It checks what is installed, and when you turn the browser on it gives each agent rules: which Space to use, and what browsing never allows.

## Set up

The browser feature needs Ego Lite, which you download yourself from its official site. Nothing else in WorkForce needs it. WorkForce never downloads or installs Ego Lite, imports a browser profile or changes your default browser. You do these steps once:

1. **Download Ego Lite from <https://lite.ego.app/>** and install it. Check that site for the platforms it supports. Use the app download there rather than an install script, so nothing runs with extra permissions.
2. Open it once and finish its onboarding. Onboarding puts `ego-browser` on your PATH and adds the `ego-browser` skill to the agents it finds (`~/.claude/skills/ego-browser` for Claude Code, `~/.agents/skills/ego-browser` for Codex). Importing Chrome data and making Ego Lite your default browser are optional; WorkForce needs neither.
3. Check the command works:
   ```sh
   ego-browser nodejs -e "console.log('ego-browser ready')"
   ```
4. Turn it on in `~/.workforce/team.toml`:
   ```toml
   browser = "ego"
   ```
5. Run `wf doctor`. The `browser (optional)` line should say `Ego Lite ready`. Then start `wf` again: the setting is read when `wf` starts.

To turn it off, set `browser = "off"` (or delete the line) and restart `wf`. `/wf-settings` shows the current state.

Ego Lite's guides suggest running Claude Code with `--dangerously-skip-permissions` or Codex with full access. WorkForce does not need that and does not do it; keep your normal permission settings.

## Use it

Ask in plain words. Claude runs `ego-browser` commands through Bash, so Claude Code asks your permission for them like any other command, unless you have allowed them yourself.

### Test a locally running web app

Start your app (for example `npm run dev` on port 3000), then in `wf`:

> Open http://localhost:3000 in the browser, sign up with a test email, and tell me if the welcome page shows the user's name. Take a screenshot of anything that looks broken.

Claude opens the page in its own Space, clicks through, saves screenshots inside the project or a temp folder, and closes the Space when it is done. A fix it then makes still goes through both reviews before it can be committed.

### Check a logged-in page

Ego Lite uses the logins in its own profile. Log in to the site in Ego Lite yourself first, then:

> In the browser, open https://dashboard.example.com/billing and tell me which plan the account is on. Only read the page; don't change anything.

Claude reads the page and reports. If a step would send, publish, buy or change account settings, it stops and asks you to do that step yourself.

### Codex

When the browser is on and Ego Lite is ready, Codex's plan, ask and `@codex` prompts tell it the browser is available and which Space to use. Codex's reviews don't browse. Codex keeps its usual sandbox (read-only unless you set `/codex-mode write`), and WorkForce does not loosen it for the browser. In testing, Codex used the browser from its read-only sandbox. If a later Codex or Ego Lite version blocks `ego-browser` there, Codex says so and Claude does the browser step instead.

## How Spaces keep agents apart

A Space is Ego Lite's own workspace for one task, with its own tabs. Each agent gets a Space name no other agent uses:

| Agent | Space name |
| --- | --- |
| Claude in `wf` | `wf <project> claude <random tag>`, new each time `wf` starts |
| A Claude sub-agent | the Claude name plus its task in 2-3 words |
| Codex | `wf <project> codex <plan\|ask\|direct> <random tag>`, new for each call |

The rules tell every agent to work only in its own Space, never to list, read, use or close your tabs or other Spaces, and to close its Space with `task.finish({ keep: [] })` when done. If you run `wf` in a separate git worktree, the Space name uses that worktree's folder name.

## What stays the same

- **Commits.** Browser use never counts as a review. A commit still needs both approvals for the exact files. WorkForce's checks apply to the commands the agents run; the agents are told not to start git or other programs from inside a browser script, because that script may run inside Ego Lite rather than in your `wf` session.
- **Permissions.** WorkForce adds no permission rule for the browser. The deny-list, `/wf-allow-outside` and your Claude Code permission prompts work as before.
- **What browsing allows.** Having a browser does not authorize sending messages or emails, posting, publishing, purchases or payments, accepting terms, or changing account settings or passwords. The agents stop and ask you to do those steps.
- **No unattended runs.** The browser is used only while you chat with `wf`; WorkForce does not schedule anything.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `wf` prints "browser = "ego" in team.toml, but the `ego-browser` command was not found" | Download Ego Lite from <https://lite.ego.app/>, install it and finish its onboarding, or put `ego-browser` on your PATH. `wf` starts without the browser until then. |
| "the `ego-browser` skill for Claude Code is missing" | Re-run Ego Lite's onboarding, or install the skill the way Ego Lite's docs describe. WorkForce looks in `~/.claude/skills/ego-browser` (or `$CLAUDE_CONFIG_DIR/skills`). |
| "the `ego-browser` skill for Codex is missing" | Same, for Codex: WorkForce looks in `~/.agents/skills/ego-browser` and `$CODEX_HOME/skills/ego-browser`. Claude can still use the browser. |
| `ego-browser nodejs …` fails | Make sure Ego Lite is running, then run the check command from Set up. If `ego-browser` prints an upgrade notice, decide yourself whether to run `ego-browser upgrade`; the agents are told not to. |
| Codex says its sandbox blocked the browser | Let Claude do the browser step. If it keeps happening, check `wf doctor` and the check command from Set up. |
| A page needs you to log in | Log in yourself in Ego Lite, then ask again. |

## Limitations

- Ego Lite must be installed and running; WorkForce only detects it. It is a separate product with its own license, privacy terms and platform support.
- Spaces separate tabs, not logins: Ego Lite's own docs say the cookie jar is shared with your tabs and other Spaces. An agent logged in as you is logged in as you.
- The limits on what browsing allows are instructions to the agents, backed by your Claude Code permission prompts. Neither Ego Lite nor WorkForce can block a click inside a page. Keep approving browser commands one at a time if a page can spend money or send messages.
- WorkForce's deny-list reads commands, not the browser script inside them, so it can't check where a script saves a screenshot. The rules tell agents to save inside the project or a temp folder.
- Ego Lite does not document running many Spaces at once from separate processes, and Spaces use a lot of memory. Prefer one browser task at a time.
- That Codex can reach the browser from its sandbox was checked live, not by the tests; a Codex or Ego Lite update could change it.
