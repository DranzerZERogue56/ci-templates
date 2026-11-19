"""Lock-down checks on the reusable workflows, plus a run of their config and secrets-scan scripts under bash."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"
REUSABLE = ["node-ts", "python", "kotlin-android", "godot", "static-site", "generic"]
SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def load(name):
    return yaml.safe_load((WF / f"{name}.yml").read_text(encoding="utf-8"))


def triggers(doc):
    return doc.get("on", doc.get(True))


def steps_of(doc):
    for job_name, job in doc["jobs"].items():
        for step in job.get("steps", []):
            yield job_name, step


class LockDownTests(unittest.TestCase):
    def test_reusable_workflows_are_workflow_call_only(self):
        for name in REUSABLE:
            self.assertEqual(set(triggers(load(name))), {"workflow_call"}, name)

    def test_read_only_permissions_everywhere(self):
        for name in REUSABLE:
            doc = load(name)
            self.assertEqual(doc["permissions"], {"contents": "read"}, name)
            for job_name, job in doc["jobs"].items():
                self.assertEqual(job["permissions"], {"contents": "read"}, f"{name}:{job_name}")

    def test_every_job_has_a_timeout(self):
        for name in REUSABLE:
            for job_name, job in load(name)["jobs"].items():
                self.assertIn("timeout-minutes", job, f"{name}:{job_name}")

    def test_actions_pinned_to_full_hashes_with_version_comment(self):
        for path in list(WF.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                m = re.match(r"\s*- uses: (\S+)(.*)$", line) or re.match(r"\s+uses: (\S+)(.*)$", line)
                if m:
                    self.assertRegex(m.group(1), SHA, f"{path.name}: {line.strip()}")
                    self.assertRegex(m.group(2), r"#\s*v\d", f"{path.name}: missing version comment: {line.strip()}")

    def test_checkout_never_keeps_credentials(self):
        for path in WF.glob("*.yml"):
            for _, step in steps_of(yaml.safe_load(path.read_text(encoding="utf-8"))):
                if str(step.get("uses", "")).startswith("actions/checkout"):
                    self.assertIs(step["with"]["persist-credentials"], False, path.name)

    def test_no_expressions_inside_run_scripts(self):
        """Script injection guard: untrusted values may only reach shell through env, never by ${{ }} inside run."""
        for path in WF.glob("*.yml"):
            for job_name, step in steps_of(yaml.safe_load(path.read_text(encoding="utf-8"))):
                if "run" in step:
                    self.assertNotIn("${{", step["run"], f"{path.name}:{job_name}")

    def test_no_dangerous_triggers_or_secrets(self):
        for path in WF.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("pull_request_target", text, path.name)
            self.assertNotIn("secrets.", text, path.name)
            self.assertNotIn("secrets: inherit", text, path.name)

    def test_every_kind_has_secrets_and_audit_jobs(self):
        for name in REUSABLE:
            jobs = load(name)["jobs"]
            self.assertTrue({"config", "secrets", "audit"} <= set(jobs), name)
            if name != "generic":
                self.assertIn("build", jobs, name)

    def test_shared_blocks_are_identical_across_workflows(self):
        """The secrets job and the stage runner are copied into every workflow; they must not drift apart."""
        texts = {n: (WF / f"{n}.yml").read_text(encoding="utf-8") for n in REUSABLE}

        def block(text, start, end):
            return text[text.index(start): text.index(end)]

        secrets = {n: block(t, "  secrets:\n", "\n  build:\n" if "\n  build:\n" in t else "\n  audit:\n") for n, t in texts.items()}
        self.assertEqual(len({s.strip() for s in secrets.values()}), 1, "secrets job differs between workflows")
        runner = {n: block(t, "          fail=0\n", "          run_stage ") for n, t in texts.items() if "          fail=0\n" in t}
        self.assertEqual(len(runner), 5)
        self.assertEqual(len(set(runner.values())), 1, "stage runner differs between workflows")
        osv = {n: block(t, "  audit:\n", "\n") if False else t[t.index("      - name: Install OSV-Scanner"):] for n, t in texts.items() if "OSV_SHA256" in t}
        self.assertEqual(len(set(osv.values())), 1, "OSV audit steps differ")

    def test_self_ci_is_locked_down_too(self):
        doc = yaml.safe_load((WF / "ci.yml").read_text(encoding="utf-8"))
        self.assertEqual(doc["permissions"], {"contents": "read"})


BASH = shutil.which("bash")
JQ = shutil.which("jq")


@unittest.skipUnless(BASH and JQ, "needs bash and jq on PATH")
class ScriptTests(unittest.TestCase):
    """Executes the real run: scripts from the workflows in a scratch repo."""

    def script(self, wf, job, step_name):
        for step in load(wf)["jobs"][job]["steps"]:
            if step.get("name") == step_name:
                return step["run"]
        raise KeyError(step_name)

    def run_script(self, script, cwd, env=None):
        full = {**os.environ, **(env or {})}
        return subprocess.run([BASH, "-c", "set -eo pipefail\n" + script], cwd=cwd, env=full, capture_output=True, text=True)

    def config_out(self, wf, files, env):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.txt"
            out.write_text("")
            repo = Path(d) / "repo"
            (repo / ".github").mkdir(parents=True)
            for rel, text in files.items():
                (repo / rel).write_text(text, encoding="utf-8")
            res = self.run_script(self.script(wf, "config", "Read .github/ci.config.yml"), repo, {**env, "GITHUB_OUTPUT": str(out)})
            lines = dict(l.split("=", 1) for l in out.read_text().splitlines() if "=" in l)
            return res, lines

    def test_config_defaults_file_and_input_precedence(self):
        env = {"DEFAULT_VERSIONS": '["22","24"]', "IN_VERSIONS": "", "IN_INSTALL": "", "IN_TYPECHECK": "", "IN_LINT": "", "IN_TEST": "", "IN_BUILD": "", "IN_AUDIT_LEVEL": ""}
        res, out = self.config_out("node-ts", {}, env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(out["versions"], '["22","24"]')
        self.assertEqual((out["audit"], out["secrets_scan"], out["audit_level"]), ("true", "true", "high"))
        cfg = 'versions: ["20", "22"]   # comment\ntest_command: "npm run ci:test"\nlint_command: skip\naudit: false\naudit_level: critical\n'
        res, out = self.config_out("node-ts", {".github/ci.config.yml": cfg}, env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(out["versions"], '["20","22"]')
        self.assertEqual(out["test_command"], "npm run ci:test")
        self.assertEqual(out["lint_command"], "skip")
        self.assertEqual((out["audit"], out["audit_level"]), ("false", "critical"))
        res, out = self.config_out("node-ts", {".github/ci.config.yml": cfg}, {**env, "IN_TEST": "make test", "IN_VERSIONS": '["24"]'})
        self.assertEqual((out["test_command"], out["versions"]), ("make test", '["24"]'))

    def test_config_rejects_bad_versions(self):
        env = {"DEFAULT_VERSIONS": '["22"]', "IN_VERSIONS": '["22; rm -rf /"]', "IN_INSTALL": "", "IN_TYPECHECK": "", "IN_LINT": "", "IN_TEST": "", "IN_BUILD": "", "IN_AUDIT_LEVEL": ""}
        res, _ = self.config_out("node-ts", {}, env)
        self.assertNotEqual(res.returncode, 0)
        env["IN_VERSIONS"] = "not json"
        res, _ = self.config_out("node-ts", {}, env)
        self.assertNotEqual(res.returncode, 0)

    def secrets_run(self, files, ignore=None):
        script = [s for s in load("generic")["jobs"]["secrets"]["steps"] if s.get("name", "").startswith("Scan")][0]
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            for rel, text in files.items():
                p = repo / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            if ignore is not None:
                (repo / ".github").mkdir(exist_ok=True)
                (repo / ".github" / "secrets-ignore.txt").write_text(ignore, encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            return self.run_script(script["run"], repo, {"PATTERN": script["env"]["PATTERN"]})

    def test_secrets_scan_catches_and_never_prints_the_value(self):
        fake = "ghp_" + "A1b2C3d4" * 5  # built at runtime so this file itself holds no secret-shaped text
        res = self.secrets_run({"src/a.txt": f"token = {fake}\n", "ok.txt": "nothing here\n"})
        self.assertEqual(res.returncode, 1)
        self.assertIn("src/a.txt:1", res.stdout)
        self.assertNotIn(fake, res.stdout + res.stderr)

    def test_secrets_scan_passes_clean_and_honours_ignore_file(self):
        self.assertEqual(self.secrets_run({"a.txt": "hello\n"}).returncode, 0)
        fake = "AKIA" + "ABCDEFGH" * 2
        self.assertEqual(self.secrets_run({"test/fixture.txt": fake + "\n"}).returncode, 1)
        self.assertEqual(self.secrets_run({"test/fixture.txt": fake + "\n"}, ignore="# fake keys\ntest/**\n").returncode, 0)


if __name__ == "__main__":
    unittest.main()
