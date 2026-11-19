# How we work in this repo

The default branch (`{{DEFAULT_BRANCH}}`) is protected: it always passes its checks and always works.
Nothing is committed straight to it.

## Branches

Make a short-lived branch for every change, named for what it is:

- `feature/<short-name>` for new things
- `fix/<short-name>` for bug fixes
- `docs/<short-name>` for documentation only

Keep each branch small and open a pull request as soon as it is useful. The pull request template
asks for the few things a reviewer needs. Delete the branch after it is merged.

## Checks

Every pull request runs the standard CI pipeline (see `.github/workflows/ci.yml`). It builds, tests,
scans for leaked secrets and audits dependencies. Fix red checks before merging.

To change what the pipeline does for this repo, edit `.github/ci.config.yml`. The comments in that
file explain every setting.
