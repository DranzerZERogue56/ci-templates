"""Tests for repo-kind detection, file planning and generation. Run: python -m unittest discover -s tests"""
import datetime as dt
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import rollout  # noqa: E402

CTX = rollout.Context(owner="someone", repo="demo", default_branch="main", tag="v1.0.0")


class DetectTests(unittest.TestCase):
    def kind(self, *files, read=None):
        return rollout.detect_kind(list(files), read or (lambda p: None))[0]

    def test_each_kind(self):
        self.assertEqual(self.kind("package.json", "src/a.ts"), "node-ts")
        self.assertEqual(self.kind("tsconfig.json"), "node-ts")
        self.assertEqual(self.kind("pyproject.toml", "app/x.py"), "python")
        self.assertEqual(self.kind("requirements-dev.txt"), "python")
        self.assertEqual(self.kind("server.py"), "python")
        self.assertEqual(self.kind("build.gradle.kts", "app/src/Main.kt"), "kotlin-android")
        self.assertEqual(self.kind("settings.gradle", "gradlew"), "kotlin-android")
        self.assertEqual(self.kind("project.godot", "main.gd"), "godot")
        self.assertEqual(self.kind("index.html", "style.css", "app.js", "img/logo.png", "CNAME"), "static-site")

    def test_generic_fallbacks(self):
        self.assertEqual(self.kind("README.md", "notes.txt"), "generic")
        self.assertEqual(self.kind(), "generic")
        self.assertEqual(self.kind("index.html", "Main.java"), "generic")  # html plus real code is not a static site

    def test_priority_and_notes(self):
        kind, notes = rollout.detect_kind(["project.godot", "package.json"], lambda p: None)
        self.assertEqual(kind, "godot")
        self.assertTrue(any("node-ts" in n for n in notes))
        kind, _ = rollout.detect_kind(["package.json", "requirements.txt"], lambda p: "{}")
        self.assertEqual(kind, "node-ts")

    def test_monorepo_note(self):
        kind, notes = rollout.detect_kind(["README.md", "web/package.json"], lambda p: None)
        self.assertEqual(kind, "generic")
        self.assertTrue(any("subfolders" in n for n in notes))

    def test_plain_js_note(self):
        _, notes = rollout.detect_kind(["package.json"], lambda p: '{"name":"x"}')
        self.assertTrue(any("plain JavaScript" in n for n in notes))
        _, notes = rollout.detect_kind(["package.json"], lambda p: '{"devDependencies":{"typescript":"5"}}')
        self.assertFalse(notes)


class RenderTests(unittest.TestCase):
    def test_every_kind_renders_valid_yaml_without_placeholders(self):
        for kind in rollout.KINDS:
            files = rollout.render(kind, CTX)
            for path, text in files.items():
                self.assertNotRegex(text, r"\{\{[A-Z_]+\}\}", f"{kind}:{path}")
                if path.endswith(".yml"):
                    yaml.safe_load(text)
            caller = yaml.safe_load(files[".github/workflows/ci.yml"])
            self.assertEqual(caller["permissions"], {"contents": "read"})
            self.assertEqual(caller["jobs"]["ci"]["uses"], f"someone/ci-templates/.github/workflows/{kind}.yml@v1.0.0")
            self.assertEqual(caller[True]["push"]["branches"], ["main"])  # YAML reads the key "on" as True
            self.assertEqual(set(caller[True]), {"pull_request", "push", "schedule"})
            cfg = yaml.safe_load(files[".github/ci.config.yml"])
            self.assertEqual(cfg["language"], kind)
            deps = yaml.safe_load(files[".github/dependabot.yml"])
            ecos = [u["package-ecosystem"] for u in deps["updates"]]
            self.assertEqual(ecos[0], "github-actions")
            self.assertEqual(ecos[1:], [rollout.ECOSYSTEM[kind]] if kind in rollout.ECOSYSTEM else [])

    def test_workflow_file_exists_for_each_kind(self):
        for kind in rollout.KINDS:
            self.assertTrue((rollout.REPO_ROOT / ".github" / "workflows" / f"{kind}.yml").is_file(), kind)

    def test_odd_branch_name_is_quoted_safely(self):
        ctx = rollout.Context("o", "r", "claude/some-branch-x1", "v1.0.0")
        text = rollout.render("generic", ctx)[".github/workflows/ci.yml"]
        self.assertEqual(yaml.safe_load(text)[True]["push"]["branches"], ["claude/some-branch-x1"])

    def test_cron_is_stable_and_valid(self):
        self.assertEqual(rollout.cron_for("a"), rollout.cron_for("a"))
        m, h, *rest = rollout.cron_for("anything").split()
        self.assertTrue(0 <= int(m) < 60 and 4 <= int(h) <= 7)
        self.assertEqual(rest, ["*", "*", "1"])

    def test_no_email_addresses_in_templates(self):
        for p in rollout.TEMPLATES.iterdir():
            text = p.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"[\w.+-]+@[\w-]+\.[\w.]+", p.name)


class PlanTests(unittest.TestCase):
    def test_fresh_repo_gets_all_five(self):
        plan = rollout.plan_files(["package.json", "README.md"], "node-ts", CTX)
        self.assertEqual(
            sorted(f.path for f in plan.write),
            sorted([".github/workflows/ci.yml", ".github/ci.config.yml", ".github/dependabot.yml", ".github/PULL_REQUEST_TEMPLATE.md", "CONTRIBUTING.md"]),
        )
        self.assertFalse(plan.proposal)

    def test_never_overwrites_existing_files(self):
        files = ["package.json", ".github/dependabot.yaml", "contributing.md", ".github/pull_request_template.md"]
        plan = rollout.plan_files(files, "node-ts", CTX)
        self.assertEqual({f.path for f in plan.write}, {".github/workflows/ci.yml", ".github/ci.config.yml"})
        self.assertEqual(len(plan.skipped), 3)

    def test_existing_workflows_make_an_inert_proposal(self):
        plan = rollout.plan_files(["package.json", ".github/workflows/build.yml"], "node-ts", CTX)
        self.assertTrue(plan.proposal)
        self.assertTrue(plan.conflicts and "build.yml" in plan.conflicts[0])
        for f in plan.write:
            self.assertTrue(f.path.startswith(".github/ci-proposal/"), f.path)

    def test_proposal_never_overwrites_a_previous_proposal(self):
        plan = rollout.plan_files([".github/workflows/x.yaml", ".github/ci-proposal/ci.yml"], "generic", CTX)
        self.assertNotIn(".github/ci-proposal/ci.yml", {f.path for f in plan.write})

    def test_pr_template_folder_is_respected(self):
        plan = rollout.plan_files(["a.py", ".github/PULL_REQUEST_TEMPLATE/bug.md"], "python", CTX)
        self.assertNotIn(".github/PULL_REQUEST_TEMPLATE.md", {f.path for f in plan.write})


class ScopeTests(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 18, tzinfo=dt.timezone.utc)
    CUT = NOW - dt.timedelta(days=365)

    def test_classify(self):
        ok = {"name": "a", "isFork": False, "isArchived": False, "defaultBranchRef": {"name": "main"}}
        recent = self.NOW - dt.timedelta(days=5)
        self.assertEqual(rollout.classify(ok, set(), recent, self.CUT), "")
        self.assertEqual(rollout.classify({**ok, "isFork": True}, set(), recent, self.CUT), "fork")
        self.assertEqual(rollout.classify({**ok, "isArchived": True}, set(), recent, self.CUT), "archived")
        self.assertEqual(rollout.classify(ok, {"a"}, recent, self.CUT), "opted out")
        self.assertEqual(rollout.classify({**ok, "name": "A"}, {"a"}, recent, self.CUT), "opted out")
        self.assertEqual(rollout.classify({**ok, "defaultBranchRef": None}, set(), recent, self.CUT), "empty repo (no default branch)")
        self.assertEqual(rollout.classify(ok, set(), None, self.CUT), "no commits")
        self.assertTrue(rollout.classify(ok, set(), self.NOW - dt.timedelta(days=400), self.CUT).startswith("inactive"))

    def test_optout_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "o.txt"
            p.write_text("# c\nFoo  # why\n\nbar\n", encoding="utf-8")
            self.assertEqual(rollout.load_optout([p, Path(d) / "missing.txt"]), {"foo", "bar"})

    def test_apply_needs_repos(self):
        with self.assertRaises(SystemExit):
            rollout.main(["--apply", "--owner", "x"])


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class ApplyTests(unittest.TestCase):
    """Runs the real commit and push logic against a local bare repository standing in for GitHub."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.bare, seed, self.clone = t / "origin.git", t / "seed", t / "clone"
        git(t, "init", "-q", "--bare", "-b", "main", str(self.bare))
        git(t, "init", "-q", "-b", "main", str(seed))
        (seed / "package.json").write_text("{}\n")
        (seed / "keep.txt").write_text("original\n")
        git(seed, "add", "-A")
        git(seed, "-c", "user.name=t", "-c", "user.email=none", "commit", "-qm", "init")
        git(seed, "push", "-q", str(self.bare), "main")
        git(t, "clone", "-q", "--no-checkout", str(self.bare), str(self.clone))

    def tearDown(self):
        rollout._rmtree(Path(self.tmp.name))

    def out(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout

    def test_apply_pushes_only_the_new_branch_with_only_new_files(self):
        plan = rollout.plan_files(rollout.list_tree(self.clone), "node-ts", CTX)
        urls = []
        rollout.apply_repo(self.clone, plan, CTX, "", lambda c, p, t: urls.append(1) or "https://example.invalid/pr/1")
        heads = self.out(self.bare, "for-each-ref", "--format=%(refname)", "refs/heads").split()
        self.assertEqual(sorted(heads), ["refs/heads/ci/standard-pipeline", "refs/heads/main"])
        diff = self.out(self.bare, "diff", "--name-status", "main", rollout.BRANCH).splitlines()
        self.assertEqual(len(diff), 5)
        self.assertTrue(all(line.startswith("A\t") for line in diff), diff)
        self.assertEqual(sorted(self.out(self.bare, "ls-tree", "-r", "--name-only", "main").split()), ["keep.txt", "package.json"])
        self.assertEqual(urls, [1])

    def test_refuses_when_branch_exists(self):
        plan = rollout.plan_files(rollout.list_tree(self.clone), "node-ts", CTX)
        rollout.apply_repo(self.clone, plan, CTX)
        other = Path(self.tmp.name) / "clone2"
        git(Path(self.tmp.name), "clone", "-q", "--no-checkout", str(self.bare), str(other))
        with self.assertRaises(RuntimeError):
            rollout.apply_repo(other, plan, CTX)

    def test_report_lists_everything(self):
        res = [rollout.RepoResult(name="x", kind="node-ts", plan=rollout.plan_files(["package.json", ".github/workflows/a.yml"], "node-ts", CTX)),
               rollout.RepoResult(name="old", skip_reason="inactive (last commit 2024-01-01)")]
        text = rollout.build_report(res, "someone", "dry run", 12, dt.datetime(2026, 11, 18, tzinfo=dt.timezone.utc))
        self.assertIn("| x |", text)
        self.assertIn("existing workflows: a.yml", text)
        self.assertIn("| old | inactive", text)


class LocalModeTests(unittest.TestCase):
    def test_local_writes_missing_files_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n")
            (root / "CONTRIBUTING.md").write_text("mine\n")
            plan = rollout.write_local(root, CTX)
            self.assertEqual(plan.kind, "python")
            self.assertEqual((root / "CONTRIBUTING.md").read_text(), "mine\n")
            self.assertTrue((root / ".github" / "workflows" / "ci.yml").is_file())


if __name__ == "__main__":
    unittest.main()
