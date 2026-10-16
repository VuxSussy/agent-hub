#!/usr/bin/env python3
"""agent-hub: let Claude, Codex, Antigravity/Gemini... work on one repo without stepping on each other.

State (tasks + locks) lives in <git-common-dir>/hub/state.json, so every git worktree
of the repo sees the same board. Team config (who is good at what) lives in .agent-hub.json
at the repo root and is committed.
"""
import argparse
import fcntl
import fnmatch
import json
import os
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

HUB = Path(__file__).resolve()
TEMPLATES = HUB.parent / "templates"
ACTIVE = ("doing", "review")  # statuses that hold file locks

DEFAULT_CONFIG = {
    "strict": False,  # True: an agent may only edit files inside a task it has claimed
    "agents": {
        "claude": {
            "strengths": ["plan", "architecture", "refactor", "review", "debug", "docs"],
            "cmd": "claude -p {prompt} --permission-mode acceptEdits",
        },
        "codex": {
            "strengths": ["impl", "tests", "fix", "scripts", "cli", "review"],
            "cmd": "codex exec --full-auto {prompt}",
        },
        "antigravity": {
            "strengths": ["ui", "frontend", "browser", "research", "large-context"],
            "cmd": "",  # IDE agent: use `hub prompt <id>` and paste the brief
        },
    },
}


# ---------- git / paths ----------

def git(*args, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def repo_root():
    return Path(git("rev-parse", "--show-toplevel") or os.getcwd())


def state_dir():
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    d = Path(common) / "hub" if common else repo_root() / ".hub"
    d.mkdir(parents=True, exist_ok=True)
    return d


def main_root():
    """Root of the main checkout (config lives there, even when called from a worktree)."""
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common).parent if common else repo_root()


def load_config():
    for base in (repo_root(), main_root()):
        p = base / ".agent-hub.json"
        if p.exists():
            return json.loads(p.read_text())
    return DEFAULT_CONFIG


@contextmanager
def board(write=False):
    d = state_dir()
    with open(d / ".lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        p = d / "state.json"
        s = json.loads(p.read_text()) if p.exists() else {"next": 1, "tasks": {}}
        yield s
        if write:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False))
            tmp.replace(p)


# ---------- scope / locks ----------

def _prefix(pattern):
    out = []
    for part in pattern.strip("/").split("/"):
        if any(c in part for c in "*?["):
            break
        out.append(part)
    return out


def overlaps(a, b):
    """Conservative: two scopes conflict unless their literal prefixes diverge."""
    pa, pb = _prefix(a), _prefix(b)
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]


def matches(path, pattern):
    pattern = pattern.strip("/")
    return path == pattern or fnmatch.fnmatch(path, pattern) or path.startswith(pattern + "/")


def lock_holders(s, path):
    return [t for t in s["tasks"].values() if t["status"] in ACTIVE and any(matches(path, f) for f in t["files"])]


def conflicts(s, task, agent):
    return [
        (o, f, g)
        for o in s["tasks"].values()
        if o["status"] in ACTIVE and o["owner"] != agent and o["id"] != task["id"]
        for f in task["files"] for g in o["files"] if overlaps(f, g)
    ]


def rel(path):
    p = Path(path).resolve()
    root = Path(git("rev-parse", "--show-toplevel", cwd=p.parent if p.parent.exists() else None) or repo_root())
    try:
        return str(p.relative_to(root.resolve()))
    except ValueError:
        return None  # outside the repo


# ---------- helpers ----------

def die(msg, code=1):
    print(f"hub: {msg}", file=sys.stderr)
    sys.exit(code)


def me(args):
    a = getattr(args, "as_", None) or os.environ.get("HUB_AGENT")
    if not a:
        die("who are you? pass --as <agent> or set HUB_AGENT")
    return a


def get(s, tid):
    t = s["tasks"].get(tid.upper())
    if not t:
        die(f"no task {tid}")
    return t


def note(t, who, text):
    t["notes"].append({"by": who, "at": time.strftime("%Y-%m-%d %H:%M"), "text": text})


def suggest(role, cfg, exclude=()):
    agents = cfg["agents"]
    for name, a in agents.items():
        if name not in exclude and role and role in a["strengths"]:
            return name
    return next((n for n in agents if n not in exclude), None)


def deps_done(s, t):
    return all(s["tasks"].get(d, {}).get("status") == "done" for d in t["after"])


# ---------- commands ----------

def cmd_init(args):
    root = repo_root()
    if not (root / ".agent-hub.json").exists():
        (root / ".agent-hub.json").write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    protocol = (TEMPLATES / "AGENTS.md").read_text().replace("{HUB}", f"python3 {HUB}")
    begin, end = "<!-- agent-hub:begin -->", "<!-- agent-hub:end -->"
    block = f"{begin}\n{protocol}\n{end}\n"
    agents_md = root / "AGENTS.md"
    text = agents_md.read_text() if agents_md.exists() else ""
    if begin in text:
        text = text[: text.index(begin)] + block + text[text.index(end) + len(end) + 1:]
    else:
        text = (text + "\n" if text else "") + block
    agents_md.write_text(text)
    # Claude Code and Gemini/Antigravity read their own file; point them at AGENTS.md.
    for name, extra in (("CLAUDE.md", "You are agent `claude`."), ("GEMINI.md", "You are agent `antigravity`.")):
        p = root / name
        cur = p.read_text() if p.exists() else ""
        if "@AGENTS.md" not in cur:
            p.write_text(f"@AGENTS.md\n\n{extra}\n\n{cur}")
    # Claude Code hook: hard-block edits to files another agent has locked.
    sp = root / ".claude" / "settings.json"
    sp.parent.mkdir(exist_ok=True)
    settings = json.loads(sp.read_text()) if sp.exists() else {}
    hook_cmd = f"python3 {HUB} hook claude"
    pre = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if not any(hook_cmd in h.get("command", "") for e in pre for h in e.get("hooks", [])):
        pre.append({"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": hook_cmd}]})
        sp.write_text(json.dumps(settings, indent=2) + "\n")
    # git pre-commit: catches every agent (Codex, Antigravity, humans) at commit time.
    hooks_dir = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir") or ".") / "hooks"
    pc = hooks_dir / "pre-commit"
    if git("rev-parse", "--git-dir") is None:
        print("! not a git repo: skipped pre-commit hook and worktrees (run `git init` first)")
    elif pc.exists() and str(HUB) not in pc.read_text():
        print(f"! {pc} already exists; add this line to it:  python3 {HUB} precommit || exit 1")
    else:
        hooks_dir.mkdir(exist_ok=True)
        pc.write_text(f"#!/bin/sh\npython3 {HUB} precommit\n")
        pc.chmod(0o755)
    state_dir()
    print(f"agent-hub ready in {root}\n  edit .agent-hub.json to tune who is good at what")


def cmd_add(args):
    cfg = load_config()
    with board(write=True) as s:
        tid = f"T{s['next']}"
        s["next"] += 1
        for d in args.after:
            get(s, d)
        t = {
            "id": tid, "title": args.title, "desc": args.desc or "", "files": args.files, "role": args.role,
            "after": [d.upper() for d in args.after], "status": "todo", "owner": None,
            "assign": args.to or suggest(args.role, cfg), "reviewer": None, "branch": None,
            "worktree": None, "notes": [],
        }
        s["tasks"][tid] = t
    print(f"{tid} added -> suggested for {t['assign']}")


def cmd_claim(args, agent=None):
    agent = agent or me(args)
    with board(write=True) as s:
        t = get(s, args.id)
        if t["status"] in ACTIVE and t["owner"] == agent:
            print(f"{t['id']} already yours ({t['status']})")
            return t
        if t["status"] != "todo":
            die(f"{t['id']} is {t['status']} (owner {t['owner']})")
        if not deps_done(s, t):
            die(f"{t['id']} waits on {', '.join(t['after'])}")
        if args.files:
            t["files"] = args.files
        c = conflicts(s, t, agent)
        if c:
            lines = "\n".join(f"  {f} overlaps {g} held by {o['owner']} ({o['id']})" for o, f, g in c)
            die(f"cannot claim {t['id']}, scope conflict:\n{lines}\n  -> pick another task or wait for them")
        t.update(status="doing", owner=agent)
        if args.worktree:
            root = main_root()
            branch = f"hub/{agent}/{t['id']}"
            wt = root.parent / f"{root.name}--{agent}-{t['id']}"
            if git("worktree", "add", "-b", branch, str(wt), cwd=root) is None:
                die(f"git worktree add failed for {wt}")
            t.update(branch=branch, worktree=str(wt))
        note(t, agent, "claimed")
    print(f"{t['id']} claimed by {agent}; locked: {', '.join(t['files']) or '(read-only task)'}")
    if t["worktree"]:
        print(f"  work in: {t['worktree']}  (branch {t['branch']})")
    return t


def cmd_next(args):
    agent, cfg = me(args), load_config()
    strengths = cfg["agents"].get(agent, {}).get("strengths", [])
    with board() as s:
        ready = [t for t in s["tasks"].values()
                 if t["status"] == "todo" and deps_done(s, t) and not conflicts(s, t, agent)]
    ready.sort(key=lambda t: (t["assign"] != agent, t["role"] not in strengths, int(t["id"][1:])))
    if not ready:
        print("nothing ready for you")
        return
    t = ready[0]
    print(f"{t['id']}: {t['title']}  [role {t['role']}, suggested {t['assign']}]")
    if args.claim:
        args.id, args.files = t["id"], None
        cmd_claim(args, agent)


def cmd_done(args):
    agent, cfg = me(args), load_config()
    with board(write=True) as s:
        t = get(s, args.id)
        if t["owner"] != agent:
            die(f"{t['id']} belongs to {t['owner']}")
        note(t, agent, args.m or "done")
        if args.no_review:
            t["status"] = "done"
        else:
            t["status"] = "review"
            t["reviewer"] = args.reviewer or suggest("review", cfg, exclude=(agent,))
    print(f"{t['id']} -> {t['status']}" + (f", reviewer: {t['reviewer']}" if t["status"] == "review" else ""))


def cmd_review(args):
    agent = me(args)
    with board(write=True) as s:
        t = get(s, args.id)
        if t["status"] != "review":
            die(f"{t['id']} is {t['status']}, not in review")
        if agent == t["owner"]:
            die("you cannot review your own work; another agent must")
        note(t, agent, ("APPROVED: " if args.approve else "CHANGES REQUESTED: ") + (args.m or ""))
        t["status"] = "done" if args.approve else "doing"
    print(f"{t['id']} -> {t['status']}")
    if args.approve and t["branch"]:
        print(f"  merge it: git merge {t['branch']}   then: git worktree remove {t['worktree']}")


def cmd_note(args):
    agent = me(args)
    with board(write=True) as s:
        note(get(s, args.id), agent, args.m)
    print("noted")


def cmd_release(args):
    agent = me(args)
    with board(write=True) as s:
        t = get(s, args.id)
        if t["owner"] != agent and not args.force:
            die(f"{t['id']} belongs to {t['owner']} (use --force if they are gone)")
        t.update(status="todo", owner=None)
        note(t, agent, f"released: {args.m or ''}")
    print(f"{t['id']} back to todo")


def cmd_status(args):
    with board() as s:
        tasks = list(s["tasks"].values())
    if not tasks:
        print("board is empty. add work: hub add \"title\" -f 'src/x/**' -r impl")
        return
    order = {"doing": 0, "review": 1, "todo": 2, "done": 3}
    for t in sorted(tasks, key=lambda t: (order[t["status"]], int(t["id"][1:]))):
        if t["status"] == "done" and not args.all:
            continue
        who = t["owner"] or f"~{t['assign']}"
        extra = f" review:{t['reviewer']}" if t["status"] == "review" else ""
        extra += f" after:{','.join(t['after'])}" if t["after"] and t["status"] == "todo" else ""
        print(f"{t['id']:>4} {t['status']:<6} {who:<12} {t['title']}{extra}")
        if t["status"] in ACTIVE and t["files"]:
            print(f"{'':12}locks {', '.join(t['files'])}")


def cmd_show(args):
    with board() as s:
        print(json.dumps(get(s, args.id), indent=2, ensure_ascii=False))


def brief(s, t, agent):
    reviewing = t["status"] == "review" and agent == t["reviewer"]
    others = [o for o in s["tasks"].values()
              if o["status"] in ACTIVE and o["owner"] != agent and o["id"] != t["id"]]
    lines = [
        f"You are agent `{agent}` working on task {t['id']}: {t['title']}",
        t["desc"],
        f"Scope under review (read-only for you): {', '.join(t['files'])}" if reviewing else
        f"Files you own (edit ONLY these): {', '.join(t['files']) or 'none - read-only task'}",
    ]
    if others:
        lines.append("Locked by other agents, do NOT edit:")
        lines += [f"  - {', '.join(o['files'])} ({o['owner']}, {o['id']})" for o in others if o["files"]]
    if t["notes"]:
        lines.append("History / handoff notes:")
        lines += [f"  - [{n['by']}] {n['text']}" for n in t["notes"]]
    if reviewing:
        lines.append(f"Your job: REVIEW {t['owner']}'s work"
                     + (f" on branch {t['branch']}" if t["branch"] else "")
                     + f". Then run: python3 {HUB} review {t['id']} --as {agent} --approve|--changes -m '<findings>'")
    else:
        lines.append(f"When finished run: python3 {HUB} done {t['id']} --as {agent} -m '<what changed, how to verify>'")
        lines.append("Need a file outside your scope? Do not edit it: add a note "
                     f"(python3 {HUB} note {t['id']} --as {agent} -m ...) or a new task.")
    return "\n".join(l for l in lines if l)


def cmd_prompt(args):
    agent = me(args)
    with board() as s:
        print(brief(s, get(s, args.id), agent))


def cmd_run(args):
    agent, cfg = me(args), load_config()
    tmpl = cfg["agents"].get(agent, {}).get("cmd")
    if not tmpl:
        die(f"agent {agent} has no cmd in .agent-hub.json; use `hub prompt {args.id} --as {agent}` and paste it")
    with board() as s:
        t = get(s, args.id)
        reviewing = t["status"] == "review" and t["reviewer"] == agent
    if not reviewing:
        args.files = None
        t = cmd_claim(args, agent)
    with board() as s:
        t = get(s, args.id)
        prompt = brief(s, t, agent)
    cwd = t["worktree"] or str(repo_root())
    command = tmpl.replace("{prompt}", shlex.quote(prompt))
    print(f"$ ({cwd}) {tmpl.split()[0]} ...  [{agent} on {t['id']}]")
    sys.exit(subprocess.call(command, shell=True, cwd=cwd, env={**os.environ, "HUB_AGENT": agent}))


def check_path(path, agent, cfg):
    """Return None if `agent` may edit `path`, else a reason."""
    r = rel(path)
    return None if r is None else check_rel(r, agent, cfg)


def check_rel(r, agent, cfg):
    with board() as s:
        holders = lock_holders(s, r)
    foreign = [t for t in holders if t["owner"] != agent]
    if foreign:
        t = foreign[0]
        return f"{r} is locked by {t['owner']} for {t['id']} ({t['title']}). Do not edit it; leave a note or pick another task."
    if cfg.get("strict") and not holders:
        return f"{r} is not in any task you claimed (strict mode). Claim a task covering it first."
    return None


def cmd_check(args):
    reason = check_path(args.path, me(args), load_config())
    if reason:
        die(reason, 2)
    print("ok")


def cmd_hook(args):
    """Claude Code PreToolUse hook: exit 2 blocks the edit and shows the reason to Claude."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    ti = data.get("tool_input", {})
    path = ti.get("file_path") or ti.get("notebook_path")
    if path:
        reason = check_path(path, args.agent, load_config())
        if reason:
            die(f"[agent-hub] {reason}", 2)


def cmd_precommit(args):
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or ""
    agent = os.environ.get("HUB_AGENT") or (branch.split("/")[1] if branch.startswith("hub/") else None)
    if not agent:
        return  # human commit on a normal branch: only warn-free pass
    staged = (git("diff", "--cached", "--name-only") or "").splitlines()
    cfg = load_config()
    # paths are already repo-relative; git sets GIT_DIR in hooks, which confuses toplevel lookups
    bad = [r for f in staged if (r := check_rel(f, agent, cfg))]
    if bad:
        die("commit blocked:\n  " + "\n  ".join(bad), 1)


def main():
    p = argparse.ArgumentParser(prog="hub", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def c(name, fn, help, agent=True):
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(fn=fn)
        if agent:
            sp.add_argument("--as", dest="as_", metavar="AGENT", help="agent name (or env HUB_AGENT)")
        return sp

    c("init", cmd_init, "set up AGENTS.md, CLAUDE.md, GEMINI.md, hooks in this repo", agent=False)
    sp = c("add", cmd_add, "add a task", agent=False)
    sp.add_argument("title")
    sp.add_argument("-d", "--desc")
    sp.add_argument("-f", "--files", action="append", default=[], help="scope glob, repeatable: -f 'src/api/**'")
    sp.add_argument("-r", "--role", help="plan|impl|tests|review|ui|research|refactor|docs...")
    sp.add_argument("--after", action="append", default=[], help="task id that must be done first")
    sp.add_argument("--to", help="assign to agent (default: best match by role)")
    sp = c("claim", cmd_claim, "take a task and lock its files")
    sp.add_argument("id")
    sp.add_argument("-f", "--files", action="append", help="override scope")
    sp.add_argument("-w", "--worktree", action="store_true", help="work in an isolated git worktree")
    sp = c("next", cmd_next, "show the best ready task for you")
    sp.add_argument("--claim", action="store_true")
    sp.add_argument("-w", "--worktree", action="store_true")
    sp = c("done", cmd_done, "finish your task (goes to cross-review)")
    sp.add_argument("id")
    sp.add_argument("-m")
    sp.add_argument("--reviewer")
    sp.add_argument("--no-review", action="store_true")
    sp = c("review", cmd_review, "approve or bounce another agent's task")
    sp.add_argument("id")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--changes", action="store_true")
    sp.add_argument("-m")
    sp = c("note", cmd_note, "leave a handoff note on a task")
    sp.add_argument("id")
    sp.add_argument("-m", required=True)
    sp = c("release", cmd_release, "give a task back (unlock)")
    sp.add_argument("id")
    sp.add_argument("-m")
    sp.add_argument("--force", action="store_true")
    sp = c("status", cmd_status, "show the board", agent=False)
    sp.add_argument("-a", "--all", action="store_true")
    sp = c("show", cmd_show, "full task json", agent=False)
    sp.add_argument("id")
    sp = c("prompt", cmd_prompt, "print the brief to paste into an agent")
    sp.add_argument("id")
    sp = c("run", cmd_run, "claim + launch the agent's CLI with the brief")
    sp.add_argument("id")
    sp.add_argument("-w", "--worktree", action="store_true")
    sp = c("check", cmd_check, "exit 2 if you may not edit PATH")
    sp.add_argument("path")
    sp = c("hook", cmd_hook, argparse.SUPPRESS, agent=False)
    sp.add_argument("agent")
    c("precommit", cmd_precommit, argparse.SUPPRESS, agent=False)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
