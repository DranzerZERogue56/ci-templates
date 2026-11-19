#!/usr/bin/env python3
"""Roll the standard CI pipeline out to GitHub repos (Python 3 standard library + the gh and git CLIs).

Default is a DRY RUN: it clones each in-scope repo shallowly into a work folder, detects what kind of project it
is, prints a plan, and writes a Markdown report. Nothing is pushed unless you pass --apply AND --repos NAME,NAME.

    python tools/rollout.py                          # dry run over every active, non-fork, non-archived repo
    python tools/rollout.py --repos app-one          # dry run for one repo
    python tools/rollout.py --apply --repos app-one  # branch ci/standard-pipeline + one pull request
    python tools/rollout.py --local ../my-new-repo   # write the files into a local checkout (no git, no GitHub)

Safety rules built in: it only ever ADDS files (never overwrites or deletes), never touches an existing branch,
never pushes to a default branch, never merges, skips forks, archived repos and anything in an opt-out file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"
VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
BRANCH = "ci/standard-pipeline"
KINDS = ("node-ts", "python", "kotlin-android", "godot", "static-site", "generic")
BOT_NAME = "ci-templates tool"
BOT_EMAIL = "ci-templates@users.noreply.github.com"

ECOSYSTEM = {"node-ts": "npm", "python": "pip", "kotlin-android": "gradle"}
STATIC_EXT = {
    "html", "htm", "css", "js", "mjs", "json", "md", "txt", "svg", "png", "jpg", "jpeg", "gif", "webp", "avif", "ico",
    "woff", "woff2", "ttf", "otf", "map", "xml", "webmanifest", "pdf", "mp3", "mp4", "wav", "ogg", "webm", "yml",
    "yaml", "toml", "csv", "",
}
PY_ROOT_MARKERS = {"pyproject.toml", "setup.py", "setup.cfg", "Pipfile"}


# --------------------------------------------------------------------------- detection

def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name.lstrip(".") else ""


def detect_kind(files: Iterable[str], read_file: Callable[[str], Optional[str]] = lambda p: None) -> tuple[str, list[str]]:
    """Return (kind, notes). `files` are repo-relative posix paths; `read_file` returns text or None."""
    files = [f for f in files if not f.startswith(".git/")]
    root = {f for f in files if "/" not in f}
    found: list[str] = []
    if "project.godot" in root:
        found.append("godot")
    if root & {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}:
        found.append("kotlin-android")
    if root & {"package.json", "tsconfig.json"}:
        found.append("node-ts")
    if root & PY_ROOT_MARKERS or any(re.fullmatch(r"requirements.*\.txt", f) for f in root) or any(f.endswith(".py") for f in root):
        found.append("python")
    html = [f for f in files if _ext(f) in ("html", "htm")]
    only_static = bool(files) and all(_ext(f) in STATIC_EXT for f in files)
    if html and only_static:
        found.append("static-site")
    notes: list[str] = []
    if found:
        kind = found[0]
        if len(found) > 1:
            notes.append("also looks like: " + ", ".join(found[1:]) + " (using " + kind + ")")
        if kind == "node-ts" and "package.json" in root:
            pkg = read_file("package.json") or ""
            if '"typescript"' not in pkg and "tsconfig.json" not in root:
                notes.append("plain JavaScript (no TypeScript found)")
        return kind, notes
    nested = sorted({f.split("/")[0] for f in files if "/" in f and f.rsplit("/", 1)[-1] in {"package.json", "pyproject.toml", "requirements.txt", "build.gradle", "build.gradle.kts", "project.godot"}})
    if nested:
        notes.append("project files only in subfolders (" + ", ".join(nested[:5]) + "): monorepo? set the kind by hand")
    return "generic", notes


# --------------------------------------------------------------------------- rendering and planning

@dataclass
class Context:
    owner: str
    repo: str
    default_branch: str
    tag: str = f"v{VERSION}"


def cron_for(repo: str) -> str:
    """Spread the weekly run over Monday morning so repos do not all start at once. Deterministic per repo."""
    n = int(hashlib.sha1(repo.encode("utf-8")).hexdigest()[:6], 16)
    return f"{n % 60} {4 + (n // 60) % 4} * * 1"


def _fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    left = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if left:
        raise ValueError("unreplaced placeholder left in template text: " + ", ".join(left))
    return text


def render(kind: str, ctx: Context) -> dict[str, str]:
    """All template files for a kind, keyed by their path in the repo (before conflict handling)."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    values = {"OWNER": ctx.owner, "WORKFLOW": kind, "TAG": ctx.tag, "DEFAULT_BRANCH": ctx.default_branch, "CRON": cron_for(ctx.repo)}
    eco = ["github-actions"] + ([ECOSYSTEM[kind]] if kind in ECOSYSTEM else [])
    blocks = "\n".join(f"  - package-ecosystem: {e}\n    directory: /\n    schedule:\n      interval: weekly" for e in eco)
    values["ECOSYSTEMS"] = blocks
    read = lambda name: (TEMPLATES / name).read_text(encoding="utf-8")
    return {
        ".github/workflows/ci.yml": _fill(read("ci.yml"), values),
        ".github/ci.config.yml": _fill(read(f"ci.config.{kind}.yml"), values),
        ".github/dependabot.yml": _fill(read("dependabot.yml"), values),
        ".github/PULL_REQUEST_TEMPLATE.md": _fill(read("PULL_REQUEST_TEMPLATE.md"), values),
        "CONTRIBUTING.md": _fill(read("CONTRIBUTING.md"), values),
    }


@dataclass
class PlannedFile:
    path: str
    content: str
    why: str = ""


@dataclass
class Plan:
    kind: str
    notes: list[str] = field(default_factory=list)
    write: list[PlannedFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    proposal: bool = False


def _exists(files_lower: set[str], *candidates: str) -> Optional[str]:
    for c in candidates:
        if c.lower() in files_lower:
            return c
    return None


def plan_files(files: Iterable[str], kind: str, ctx: Context, notes: Optional[list[str]] = None) -> Plan:
    """Decide which files to add. Never overwrites: anything that exists is skipped and reported."""
    files = list(files)
    low = {f.lower() for f in files}
    plan = Plan(kind=kind, notes=list(notes or []))
    rendered = render(kind, ctx)
    workflows = sorted(f for f in files if f.lower().startswith(".github/workflows/") and f.lower().endswith((".yml", ".yaml")))
    if workflows:
        # The repo already has CI: add an inert proposal (not under .github/workflows, so GitHub never runs it).
        plan.proposal = True
        plan.conflicts.append("existing workflows: " + ", ".join(w.rsplit("/", 1)[-1] for w in workflows))
        note = (
            "# Proposed standard CI\n\nThis repo already has its own workflows, so nothing here is active.\n"
            f"Files in this folder are a proposal ({kind} pipeline, ci-templates {ctx.tag}).\n\n"
            "To adopt it: move `ci.yml` to `.github/workflows/`, `ci.config.yml` to `.github/`, retire or merge the\n"
            "old workflows, then delete this folder. To decline, delete this folder.\n"
        )
        for path, content, why in (
            (".github/ci-proposal/ci.yml", rendered[".github/workflows/ci.yml"], "proposal only: repo already has workflows"),
            (".github/ci-proposal/ci.config.yml", rendered[".github/ci.config.yml"], "proposal only"),
            (".github/ci-proposal/README.md", note, "proposal only"),
        ):
            if _exists(low, path):
                plan.skipped.append(f"{path} (already exists)")
            else:
                plan.write.append(PlannedFile(path, content, why))
        return plan

    def want(path: str, *existing_names: str) -> None:
        hit = _exists(low, *(existing_names or (path,)))
        if hit:
            plan.skipped.append(f"{path} (already exists as {hit})")
        else:
            plan.write.append(PlannedFile(path, rendered[path]))

    want(".github/workflows/ci.yml")
    want(".github/ci.config.yml")
    want(".github/dependabot.yml", ".github/dependabot.yml", ".github/dependabot.yaml")
    pr_dir = any(f.lower().startswith(".github/pull_request_template/") for f in files)
    if pr_dir:
        plan.skipped.append(".github/PULL_REQUEST_TEMPLATE.md (a pull_request_template folder exists)")
    else:
        want(".github/PULL_REQUEST_TEMPLATE.md", ".github/PULL_REQUEST_TEMPLATE.md", "PULL_REQUEST_TEMPLATE.md", "docs/PULL_REQUEST_TEMPLATE.md")
    want("CONTRIBUTING.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md")
    return plan


# --------------------------------------------------------------------------- opt-out, scope

def load_optout(paths: Iterable[Path]) -> set[str]:
    names: set[str] = set()
    for p in paths:
        if p and Path(p).is_file():
            for line in Path(p).read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    names.add(line.lower())
    return names


def classify(repo: dict, optout: set[str], last_commit: Optional[dt.datetime], cutoff: dt.datetime) -> str:
    """Return '' when in scope, else the reason it is skipped."""
    if repo.get("isFork"):
        return "fork"
    if repo.get("isArchived"):
        return "archived"
    if repo["name"].lower() in optout:
        return "opted out"
    if not repo.get("defaultBranchRef"):
        return "empty repo (no default branch)"
    if last_commit is None:
        return "no commits"
    if last_commit < cutoff:
        return f"inactive (last commit {last_commit.date()})"
    return ""


# --------------------------------------------------------------------------- shell helpers

def run(cmd: list[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} failed: {res.stderr.strip()[:300]}")
    return res


def gh_json(args: list[str]):
    return json.loads(run(["gh", *args]).stdout or "null")


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


def last_commit_date(owner: str, name: str) -> Optional[dt.datetime]:
    res = run(["gh", "api", f"repos/{owner}/{name}/commits?per_page=1", "--jq", ".[0].commit.committer.date"], check=False)
    text = res.stdout.strip()
    if res.returncode != 0 or not text:
        return None
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def shallow_clone(owner: str, name: str, dest: Path, work: Path) -> None:
    """Fresh shallow, blob-less, no-checkout clone under the work folder. Never touches anyone's own clone."""
    clones = (work / "clones").resolve()
    if clones not in dest.resolve().parents:
        raise RuntimeError("refusing to clone outside the work folder")
    if dest.exists():
        _rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(["gh", "repo", "clone", f"{owner}/{name}", str(dest), "--", "--depth", "1", "--filter=blob:none", "--no-checkout"])


def list_tree(clone: Path) -> list[str]:
    out = run(["git", "ls-tree", "-r", "-z", "--name-only", "HEAD"], cwd=clone).stdout
    return [p for p in out.split("\0") if p]


def read_blob(clone: Path, path: str) -> Optional[str]:
    res = run(["git", "show", f"HEAD:{path}"], cwd=clone, check=False)
    return res.stdout if res.returncode == 0 else None


# --------------------------------------------------------------------------- apply

def pr_body(plan: Plan, ctx: Context, trailer: str) -> str:
    lines = [
        f"Adds the standard CI pipeline (`{plan.kind}`) from ci-templates {ctx.tag}.",
        "",
        "**Files added** (nothing existing was changed or removed):",
        *[f"- `{f.path}`" for f in plan.write],
    ]
    if plan.skipped:
        lines += ["", "**Skipped, already present:**", *[f"- {s}" for s in plan.skipped]]
    if plan.proposal:
        lines += ["", "This repo already has workflows, so the new pipeline is only a proposal in `.github/ci-proposal/` and does not run."]
    lines += [
        "",
        "Settings for this repo live in `.github/ci.config.yml`. To undo: close this PR and delete the branch.",
        "Not merged automatically; review the first CI run on this PR before merging.",
    ]
    if trailer:
        lines += ["", trailer]
    return "\n".join(lines) + "\n"


def apply_repo(clone: Path, plan: Plan, ctx: Context, trailer: str = "", create_pr: Optional[Callable[..., str]] = None, commit_trailer: str = "") -> str:
    """Commit the plan on a NEW branch and push only that branch. Returns the PR URL (or '' without create_pr)."""
    if not plan.write:
        raise RuntimeError("nothing to add")
    remote = run(["git", "ls-remote", "--heads", "origin", f"refs/heads/{BRANCH}"], cwd=clone).stdout.strip()
    if remote:
        raise RuntimeError(f"branch {BRANCH} already exists on the remote; refusing to touch it")
    run(["git", "checkout", "-q", "-b", BRANCH], cwd=clone)
    run(["git", "read-tree", "HEAD"], cwd=clone)  # no-checkout clones start with an empty index
    for pf in plan.write:
        target = clone / pf.path
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(pf.content)
        run(["git", "add", "--", pf.path], cwd=clone)
    status = run(["git", "diff", "--cached", "--name-status"], cwd=clone).stdout.splitlines()
    bad = [s for s in status if not s.startswith("A\t")]
    if bad or len(status) != len(plan.write):
        raise RuntimeError("safety check failed: the commit would change something other than new files: " + "; ".join(bad[:3]))
    msg = f"Add standard CI pipeline ({plan.kind}, ci-templates {ctx.tag})\n\nAdds only new files; no existing file is changed."
    if commit_trailer:
        msg += f"\n\n{commit_trailer}"
    run(["git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "-c", "core.autocrlf=false", "commit", "-q", "-m", msg], cwd=clone)
    run(["git", "push", "origin", f"refs/heads/{BRANCH}:refs/heads/{BRANCH}"], cwd=clone)
    if create_pr is None:
        return ""
    return create_pr(ctx, plan, trailer)


def gh_create_pr(ctx: Context, plan: Plan, trailer: str) -> str:
    title = f"Add standard CI pipeline ({plan.kind})"
    out = run(["gh", "pr", "create", "--repo", f"{ctx.owner}/{ctx.repo}", "--base", ctx.default_branch, "--head", BRANCH,
               "--title", title, "--body", pr_body(plan, ctx, trailer)]).stdout.strip()
    return out.splitlines()[-1] if out else ""


# --------------------------------------------------------------------------- report

@dataclass
class RepoResult:
    name: str
    private: bool = False
    kind: str = ""
    last_commit: str = ""
    skip_reason: str = ""
    plan: Optional[Plan] = None
    pr_url: str = ""
    error: str = ""


def build_report(results: list[RepoResult], owner: str, mode: str, months: int, now: dt.datetime) -> str:
    inscope = [r for r in results if not r.skip_reason]
    skipped = [r for r in results if r.skip_reason]
    L = [
        "# CI templates: rollout report", "",
        f"- Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Mode: **{mode}**; ci-templates version: v{VERSION}",
        f"- Repos looked at: {len(results)}; in scope: {len(inscope)}; skipped: {len(skipped)}",
        f"- Scope rule: non-fork, non-archived, not opted out, real last commit within {months} months", "",
        "## In scope (veto any of these by adding the name to the opt-out file)", "",
        "| Repo | Visibility | Kind | Last commit | Will add | Notes / conflicts | PR |", "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(inscope, key=lambda x: x.name.lower()):
        p = r.plan
        if r.error:
            L.append(f"| {r.name} | {'private' if r.private else 'public'} | - | {r.last_commit} | - | ERROR: {r.error} | |")
            continue
        adds = ", ".join(f"`{f.path}`" for f in p.write) if p else ""
        notes = "; ".join((p.notes if p else []) + (p.conflicts if p else []) + (["skipped: " + ", ".join(p.skipped)] if p and p.skipped else []))
        L.append(f"| {r.name} | {'private' if r.private else 'public'} | {r.kind} | {r.last_commit} | {adds} | {notes} | {r.pr_url} |")
    conflicts = [r for r in inscope if r.plan and r.plan.conflicts]
    L += ["", "## Conflicts (existing CI: only a proposal is added, nothing runs)", ""]
    L += [f"- **{r.name}**: {'; '.join(r.plan.conflicts)}" for r in conflicts] or ["- none"]
    L += ["", "## Skipped", "", "| Repo | Reason |", "|---|---|"]
    L += [f"| {r.name} | {r.skip_reason} |" for r in sorted(skipped, key=lambda x: x.name.lower())] or ["| - | - |"]
    return "\n".join(L) + "\n"


def default_report_path() -> Path:
    vault = Path.home() / "Brain"
    if vault.is_dir():
        return vault / "02-Projects" / "CI Templates" / "rollout-report.md"
    return Path("rollout-report.md")


# --------------------------------------------------------------------------- main

def write_local(root: Path, ctx: Context) -> Plan:
    """--local mode: add the files to an existing local checkout. No git, no GitHub, never overwrites."""
    files = [str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts and "node_modules" not in p.relative_to(root).parts]
    kind, notes = detect_kind(files, lambda rel: (root / rel).read_text(encoding="utf-8", errors="replace") if (root / rel).is_file() else None)
    plan = plan_files(files, kind, ctx, notes)
    for pf in plan.write:
        target = root / pf.path
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(pf.content, encoding="utf-8", newline="\n")
    return plan


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--owner", help="GitHub account to roll out for (default: the logged-in gh user)")
    ap.add_argument("--repos", help="comma-separated repo names; required with --apply")
    ap.add_argument("--apply", action="store_true", help="push branch ci/standard-pipeline and open a PR (needs --repos)")
    ap.add_argument("--dry-run", action="store_true", help="plan only; this is the default")
    ap.add_argument("--local", type=Path, help="write the files into this local checkout instead (no git, no GitHub)")
    ap.add_argument("--report", type=Path, default=None, help="where to write the Markdown report")
    ap.add_argument("--work", type=Path, default=REPO_ROOT / ".work", help="work folder for fresh clones")
    ap.add_argument("--months", type=int, default=12, help="a repo is active if its last real commit is newer than this")
    ap.add_argument("--optout", type=Path, action="append", default=[], help="extra opt-out file (one repo name per line); repeatable")
    ap.add_argument("--pr-footer", default="", help="line added at the end of each PR description")
    ap.add_argument("--commit-trailer", default="", help="line added at the end of each commit message")
    args = ap.parse_args(argv)

    if args.apply and not args.repos:
        ap.error("--apply needs --repos NAME,NAME (the tool never rolls out to everything at once)")
    if args.apply and args.dry_run:
        ap.error("--apply and --dry-run contradict each other")

    owner = args.owner or run(["gh", "api", "user", "--jq", ".login"]).stdout.strip()
    if args.local:
        root = args.local.resolve()
        ctx = Context(owner=owner, repo=root.name, default_branch="main")
        plan = write_local(root, ctx)
        print(f"{root}: kind={plan.kind}; added {[f.path for f in plan.write]}; skipped {plan.skipped}; conflicts {plan.conflicts}")
        print("Review and commit the new files yourself. Set the branch name in .github/workflows/ci.yml if it is not 'main'.")
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=round(args.months * 30.4375))
    optout = load_optout([REPO_ROOT / "tools" / "optout.txt", *args.optout, *([Path(os.environ["CI_TEMPLATES_OPTOUT"])] if os.environ.get("CI_TEMPLATES_OPTOUT") else [])])
    listing = gh_json(["repo", "list", owner, "--limit", "1000", "--json", "name,isFork,isArchived,isPrivate,defaultBranchRef"])
    wanted = {n.strip().lower() for n in args.repos.split(",") if n.strip()} if args.repos else None
    if wanted:
        missing = wanted - {r["name"].lower() for r in listing}
        if missing:
            ap.error("unknown repo(s): " + ", ".join(sorted(missing)))
        listing = [r for r in listing if r["name"].lower() in wanted]

    results: list[RepoResult] = []
    for repo in sorted(listing, key=lambda r: r["name"].lower()):
        res = RepoResult(name=repo["name"], private=bool(repo.get("isPrivate")))
        results.append(res)
        # Cheap checks first; the activity lookup (one API call) only for repos that could still be in scope.
        reason = classify(repo, optout, dt.datetime.now(dt.timezone.utc), cutoff)
        last = None
        if not reason or reason == "no commits":
            last = last_commit_date(owner, repo["name"])
            res.last_commit = str(last.date()) if last else ""
            # An explicitly named repo is allowed even when quiet; forks, archived and opt-outs still block it.
            reason = classify(repo, optout, last, cutoff if not wanted else dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        res.skip_reason = reason
        if reason:
            print(f"SKIP  {res.name}: {reason}")
            continue
        ctx = Context(owner=owner, repo=res.name, default_branch=repo["defaultBranchRef"]["name"])
        try:
            clone = args.work / "clones" / res.name
            shallow_clone(owner, res.name, clone, args.work)
            files = list_tree(clone)
            kind, notes = detect_kind(files, lambda p: read_blob(clone, p))
            res.kind = kind
            res.plan = plan_files(files, kind, ctx, notes)
            print(f"PLAN  {res.name}: {kind}; add {len(res.plan.write)} file(s)" + (f"; CONFLICT {res.plan.conflicts}" if res.plan.conflicts else "") + (f"; skip {len(res.plan.skipped)}" if res.plan.skipped else ""))
            if args.apply:
                if not res.plan.write:
                    print(f"  nothing to add for {res.name}")
                    continue
                res.pr_url = apply_repo(clone, res.plan, ctx, args.pr_footer, gh_create_pr, args.commit_trailer)
                print(f"  PR: {res.pr_url}")
        except Exception as exc:  # one bad repo must not stop the others
            res.error = str(exc)[:200]
            print(f"ERROR {res.name}: {res.error}")

    report = args.report or default_report_path()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(build_report(results, owner, "APPLY" if args.apply else "dry run", args.months, now), encoding="utf-8")
    print(f"Report: {report}")
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
