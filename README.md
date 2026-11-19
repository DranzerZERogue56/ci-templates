# ci-templates

One standard build-and-check pipeline for every repo, kept in one place.

Each repo gets a tiny `ci.yml` that points at a shared, versioned workflow here. The shared workflow builds the
project, runs its tests, scans for leaked secrets and audits dependencies. Anyone can change how it behaves
for one repo (a settings file) or for all repos (edit the shared workflow and release a new tag).

## What is in here

| Folder | What it holds |
|---|---|
| `.github/workflows/` | The shared workflows, one per kind of project: `node-ts`, `python`, `kotlin-android`, `godot`, `static-site`, `generic`. Also this repo's own `ci.yml`. |
| `templates/` | The files copied into each repo: the small caller `ci.yml`, `ci.config.<kind>.yml` (settings), `dependabot.yml`, a pull request template and `CONTRIBUTING.md`. |
| `tools/rollout.py` | Detects each repo's kind and adds those files on a branch, with a pull request. Dry run by default. |
| `tests/` | Tests for the tool and for the lock-down rules of the workflows. |

Every shared workflow has the same shape: a `config` job reads the settings, then a secrets scan, a build
(one job per language version) and a dependency audit run side by side. The `generic` kind only does the secrets
scan and the audit.

## Add a repo by hand

1. Pick the kind: `node-ts` (package.json / tsconfig), `python`, `kotlin-android` (Gradle), `godot`
   (project.godot), `static-site` (only HTML/CSS/JS) or `generic` (anything else).
2. Copy these from `templates/` into the repo and fill in the `{{...}}` parts:
   - `ci.yml` to `.github/workflows/ci.yml` (set `{{OWNER}}`, `{{WORKFLOW}}` = the kind, `{{TAG}}`, `{{DEFAULT_BRANCH}}`, `{{CRON}}`)
   - `ci.config.<kind>.yml` to `.github/ci.config.yml`
   - `dependabot.yml` to `.github/dependabot.yml` (keep `github-actions`, add the ecosystem for your language)
   - `PULL_REQUEST_TEMPLATE.md` to `.github/`, and `CONTRIBUTING.md` to the repo root
3. Open a pull request. The CI checks run on it. Merge when they are green.

Or let the tool do it for you:

```
python tools/rollout.py                              # dry run over all your repos: prints a plan, writes a report
python tools/rollout.py --repos my-repo              # dry run for one repo
python tools/rollout.py --apply --repos my-repo      # push branch ci/standard-pipeline and open one pull request
python tools/rollout.py --local ../my-new-repo       # write the files into a local folder, no git, no GitHub
```

Needs Python 3, `git`, and the `gh` CLI logged in. The tool only adds files. It never overwrites a file, never
touches an existing branch, never merges, and skips forks, archived repos, quiet repos (no real commit in 12
months, change with `--months`) and anything in an opt-out file. If a repo already has workflows, it adds an
inactive proposal in `.github/ci-proposal/` and lists the conflict in the report instead. Opt-outs go in
`tools/optout.txt` (public, so no private names) or in a local file passed with `--optout`.
The report goes to `--report PATH` (default: `~/Brain/02-Projects/CI Templates/rollout-report.md` if that
folder exists, otherwise `rollout-report.md`).

## Change what the pipeline does

**For one repo:** edit `.github/ci.config.yml`. The comments there explain each setting. You can change language
versions, override any command, set a step to `skip`, set the audit level, or turn the audit or secrets scan off.
Leave a value empty to get the automatic default. Order of precedence: an input in the repo's `ci.yml`, then
`ci.config.yml`, then the built-in default. Fake test keys that trip the secrets scan can be listed (one path
pattern per line) in `.github/secrets-ignore.txt`.

When a step has nothing to run (no tests yet, tool not installed) it is skipped with a visible notice and a line
in the run summary. It never passes silently.

**For every repo:** fork this repo (or edit it), change the workflow under `.github/workflows/`, run the tests,
and cut a new tag. Repos pick it up when their pinned tag moves (see below). A different design for one kind is
also fine: copy a workflow, rename it, and point a repo's `ci.yml` at it.

## How updates flow

- Each repo's `ci.yml` points at an exact tag, like `.../node-ts.yml@v1.0.0`.
- Dependabot (weekly, in each repo) sees a newer tag and opens a pull request that bumps it. Nothing merges by itself.
- Inside this repo every third-party action is pinned to a full commit hash with the version in a comment, and
  Dependabot also proposes updates to those. Tools downloaded in the workflows (OSV-Scanner, Godot) are pinned and
  checksum-checked.

To refresh a pin by hand: `gh api repos/OWNER/ACTION/commits/TAG --jq .sha` gives the commit hash for a tag.

## Cut a new tag

1. Change the workflow or template, run `python -m unittest discover -s tests`, and lint with actionlint.
2. Bump `VERSION` (for example `1.0.1`) and commit to `main` through a pull request.
3. Tag and publish: `git tag v1.0.1 && git push origin v1.0.1`, and optionally `gh release create v1.0.1 --generate-notes`.
4. Repos get a Dependabot pull request for the new tag. Use a new minor version (`v1.1.0`) for new behaviour and
   a new major (`v2.0.0`) when a repo has to change its own files.

## New repos

Copy the templates into any new repo with `python tools/rollout.py --local PATH` right after creating it, or mark this
repo as a GitHub template repository (see the handoff notes) so new repos start with the files in place.

## Good to know

- Only the repo root is built. A project that lives in a subfolder needs its own `ci.yml` settings (commands that `cd` into it).
- Workflows are read-only: they get a token that can only read the repo, no secrets, no write access.
- Private repos spend GitHub Actions minutes. Pushes build only the default branch; pull requests build their branch.
- The secrets scan prints file and line only, never the matching text, and only sees tracked files, not history.
- The workflow scripts are checked by actionlint but not by shellcheck. Gradle projects are only audited if they commit a lockfile.
