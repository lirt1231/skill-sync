# Releasing Skill Sync

## Release gate

1. Confirm `main` is clean and synchronized with `origin/main`.
2. Review `CHANGELOG.md` and move Unreleased entries under the release version.
3. Update `skill_sync/version.py` using a PEP 440 version such as `0.1.0b1`.
4. Run the full test suite on Linux, macOS, and Windows CI.
5. Build and install the wheel in an isolated environment.
6. Scan the complete Git history for credentials and private authored content.
7. Confirm README support claims and known limitations match the release.
8. Confirm the repository license and GitHub security settings.

## Local verification

```bash
python -m unittest discover -s tests
node --check skill_sync/web_static/app.js
git diff --check

python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
python -m venv .release-venv
.release-venv/bin/python -m pip install --no-index --no-deps dist/*.whl
.release-venv/bin/skill-sync version
```

Use `.release-venv/Scripts/` instead of `.release-venv/bin/` on Windows.

## Publish

Create an annotated tag only from the verified `main` commit:

```bash
git tag -a v0.1.0-beta.1 -m "Skill Sync 0.1.0 beta 1"
git push origin main v0.1.0-beta.1
```

Create a prerelease on GitHub, attach the wheel and source archive, and use the
matching changelog section as release notes. Publishing to PyPI is a separate,
explicit decision and is not part of the initial technical preview.
