## Multi-agent protocol (agent-hub)

Several AI agents (claude, codex, antigravity, ...) work on this repo at the same time.
The shared board is the single source of truth. `hub` below means: `{HUB}`

### Rules (non-negotiable)
1. **No task, no edit.** Before changing files, claim a task: `hub next --as <you> --claim` or `hub claim T3 --as <you>`.
2. **Stay inside your scope.** Only edit files matching your task's `files` globs. Files locked by another agent are off-limits, even for a "tiny fix".
   Check with `hub check <path> --as <you>` when unsure (exit code 2 = forbidden).
3. **Need something outside your scope?** Do not edit it. Leave a note (`hub note T3 --as <you> -m "..."`) or add a task (`hub add "..." -f <glob> -r <role>`).
4. **Never review your own work.** `hub done T3 --as <you> -m "<what changed, how to verify>"` sends it to another agent for review.
5. **Write handoffs for a cold reader.** Notes must say what changed, why, what is left, and how to test. The next agent has none of your context.
6. **Do not rewrite another agent's decisions silently.** Disagree in a note or in review (`--changes -m`), not by reverting their code.
7. Run `hub status` when you start and before you finish.

### Who does what (default routing; edit `.agent-hub.json`)
| Agent | Lead on | Typical tasks |
|---|---|---|
| claude | plan, architecture, refactor, review, debug, docs | split features into tasks with clear file scopes; design interfaces; hard bugs; review others |
| codex | impl, tests, fix, scripts, cli | implement a well-specified task fast; write/repair tests; terminal-heavy work |
| antigravity | ui, frontend, browser, research, large-context | build UI and verify it in the browser; read large codebases/docs |

Typical flow: **claude plans** (adds tasks with disjoint `files` scopes and `--after` dependencies) → **codex / antigravity implement in parallel** → **a different agent reviews** → merge.

### Interfaces first
When two tasks touch each other (API ↔ UI), the planner first creates a small task that writes the shared contract
(types, API schema, function signatures). The implementation tasks depend on it (`--after`) and must not change it;
contract changes go through a note to the contract owner.
