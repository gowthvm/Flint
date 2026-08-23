# Release Checklist

Follow this checklist for every Flint release. Work top-to-bottom. Do not skip steps.

---

## 1. Decide the version number

Follow semver. Bump:
- **PATCH** (1.3.x): bug fixes only, no new features
- **MINOR** (1.x.0): new features, no breaking changes
- **MAJOR** (x.0.0): breaking changes or major redesign

Current version lives in **four** places that must all match:

| File | What to update |
|---|---|
| `core/version.py` | `APP_VERSION = "X.Y.Z"` |
| `version_info.txt` | `filevers`, `prodvers`, `FileVersion`, `ProductVersion` (all four) |
| `setup.py` | `version="X.Y.Z"` |
| `flint-web/*.html` | Every footer: `vX.Y.Z` — plus `index.html` JSON-LD `softwareVersion`, hero badge, status strip; `download.html` hero; `about.html` technology section |

---

## 2. Update changelog

Edit `flint-web/changelog.html`:

1. Add a new `<h2>` block at the **top** of the changelog list (before the previous latest entry).
2. Move the `chip on` class from the old latest entry to the new one.
3. Use this template:

```html
<h2 id="vX-Y-Z">vX.Y.Z <span class="chip on">latest</span></h2>
<p><time datetime="YYYY-MM-DD">Month Day, Year</time></p>
<ul>
  <li><strong>Feature:</strong> description</li>
  <li><strong>Fix:</strong> description</li>
  <li><strong>Test:</strong> description</li>
  <li><strong>Docs:</strong> description</li>
  <li><strong>Perf:</strong> description</li>
</ul>
```

4. Group items by type: Feature, Fix, Test, Docs, Perf. One item per `<li>`.

---

## 3. Update website docs if needed

If the release changes user-facing behavior, update the relevant website page:

| Change type | File to update |
|---|---|
| New CLI flag or command | `flint-web/cli.html` — add/update command card |
| New feature or behavior change | `flint-web/docs.html` — update relevant section |
| New feature (user-facing) | `flint-web/features.html` — add row or update text |
| New comparison point | `flint-web/compare.html` — update matrix |
| Updated install steps | `flint-web/download.html` |
| New FAQ-worthy question | `flint-web/faq.html` — add entry |
| Project milestone | `flint-web/about.html` — update timeline |

If only internal/test/CI changes, skip this step.

---

## 4. Verify all version numbers match

Run these commands before committing:

```powershell
# Should return exactly 4 hits (one per file):
rg "APP_VERSION|1\.3\.0" core/version.py setup.py version_info.txt

# Should return 0 hits (no stale versions anywhere):
rg "1\.2\.[0-2]" --include "*.py" --include "*.txt" --include "*.html"

# Verify footers — every HTML file should show the new version:
rg "v1\.3\.0" flint-web/*.html
```

---

## 5. Run the full test suite

```powershell
# Lint
ruff check .

# Type check
mypy core ui main.py --ignore-missing-imports

# Tests
python -m pytest -v
```

All three must pass. Fix any failures before continuing.

---

## 6. Build and smoke test locally

```powershell
# Build native extension
python setup.py build_ext --inplace

# Build exe with PyInstaller
python -m PyInstaller --clean --noconfirm flint.spec

# Launch and confirm it starts without crash
.\dist\flint.exe
# Wait ~10 seconds, confirm window appears, close it

# Verify version in output
.\dist\flint.exe --version
```

---

## 7. Commit, tag, and push

```powershell
# Stage all changes
git add -A

# Commit with descriptive message
git commit -m "release: vX.Y.Z — brief summary of changes"

# Tag
git tag vX.Y.Z

# Push commit and tag together
git push && git push --tags
```

Pushing the tag triggers the GitHub Actions release workflow which:
1. Runs full CI (lint, type check, tests)
2. Builds `flint.exe` (PyInstaller), signs it, computes SHA-256
3. Builds sdist + wheels for Python 3.10–3.13
4. Runs pip install smoke test
5. Publishes GitHub Release with `flint.exe` + SHA-256
6. Publishes to PyPI as `flint-usb`

---

## 8. Verify deployment

After the workflow completes (~10 minutes):

```powershell
# Check GitHub Release exists
gh release view vX.Y.Z

# Check PyPI version
pip index versions flint-usb

# Test install in clean venv
python -m venv .test-venv
.\.test-venv\Scripts\Activate.ps1
pip install flint-usb
flint --version
deactivate
Remove-Item -Recurse .test-venv
```

---

## 9. Deploy website (if docs changed)

If step 3 modified any files in `flint-web/`:

```powershell
cd flint-web
git add -A
git commit -m "docs: update website for vX.Y.Z"
git push
```

Vercel auto-deploys on push to `main`.

---

## 10. Post-release

- [ ] Confirm `flint --version` matches the tag
- [ ] Confirm `pip install flint-usb` installs the new version
- [ ] Skim the GitHub Release page — assets present, SHA-256 visible
- [ ] Skim the live website — footer shows correct version, changelog entry present
- [ ] Close any related issues with "shipped in vX.Y.Z"

---

## Quick reference: version locations

| File | Field | Format |
|---|---|---|
| `core/version.py` | `APP_VERSION` | `"1.3.0"` |
| `version_info.txt` | `filevers` / `prodvers` | `(1, 3, 0, 0)` |
| `version_info.txt` | `FileVersion` / `ProductVersion` | `"1.3.0"` |
| `setup.py` | `version` | `"1.3.0"` |
| `flint-web/index.html` | JSON-LD | `"softwareVersion": "1.3.0"` |
| `flint-web/index.html` | hero badge | `v1.3.0` |
| `flint-web/index.html` | status strip | `v1.3.0` |
| `flint-web/*.html` | footer | `v1.3.0` |
| `flint-web/download.html` | hero text | `Flint v1.3.0` |
| `flint-web/about.html` | technology section | `v1.3.0` |
| `flint-web/changelog.html` | latest entry header | `v1.3.0` |
