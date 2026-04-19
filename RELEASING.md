# Releasing PowerPilz Companion

This document describes the steps to ship a new release of the PowerPilz Companion integration so HACS users pick it up automatically.

## TL;DR

```bash
# 1. Work on main — code, test, commit
git checkout main
# ... normal dev ...

# 2. Bump the version in manifest.json
#    "version": "X.Y.Z"
# Commit the bump (subject: "release: bump version to X.Y.Z").

# 3. Push main
git push origin main

# 4. Fast-forward merge main to release — this triggers the release
git checkout release
git pull --ff-only           # guard: release shouldn't be ahead remotely
git merge main --ff-only
git push origin release
git checkout main
```

The **Release** GitHub Action then:

1. Reads the version from `custom_components/powerpilz_companion/manifest.json`
2. Checks that the tag `vX.Y.Z` doesn't already exist (fails if it does — bump first)
3. Zips the `powerpilz_companion/` integration folder
4. Creates the tag at the new `release` commit
5. Publishes a GitHub Release `vX.Y.Z`
6. Attaches `powerpilz_companion.zip` as an asset (for manual installers)

HACS users pick up the new release automatically from the tag commit tree.

## Golden rules

1. **Never push directly to `release`.** Always work on `main`, then fast-forward merge `main → release`. Anything pushed directly to `release` creates drift and forces merge commits later.
2. **Bump `manifest.json` before every release.** The Release workflow derives the tag from that file and refuses to publish if the tag already exists.
3. **Keep version numbers in sync with the [PowerPilz cards repo](https://github.com/gregor-autischer/PowerPilz)** when the two ship related features together. Not strictly required — they can diverge when only one side changes — but helps users reason about compatibility.
4. **Use fast-forward merge `--ff-only`** for `main → release`. If ff isn't possible, `release` has diverged — stop, investigate, pull, or rebase. Do not create merge commits on `release`.

## Full walkthrough

### 1. Code changes on main

Everything lives on `main`. Feature branches or direct commits are fine for a small project. Each logical change is one commit with a clear message (`feat: …`, `fix: …`, `docs: …`, `chore: …`).

The **Validate** workflow runs on every push to main and every PR. It:

- Validates Python syntax (`ast.parse` every `.py` file under `custom_components/`)
- Validates all JSON files (`json.loads`)
- Validates `services.yaml` (`yaml.safe_load`)
- Runs the [HACS action](https://github.com/hacs/action) with `category: integration` to validate the repo structure
- Runs [Hassfest](https://developers.home-assistant.io/docs/creating_integration_manifest/) to validate the integration manifest

### 2. Version bump

Single commit touching one file:

```diff
# custom_components/powerpilz_companion/manifest.json
-  "version": "0.3.0",
+  "version": "0.4.0",
```

Commit message pattern: `release: bump version to X.Y.Z`.

### 3. Push main

```bash
git push origin main
```

Validate CI runs. It must be green before proceeding.

### 4. Release via the `release` branch

```bash
git checkout release
git pull --ff-only
git merge main --ff-only
git push origin release
git checkout main
```

The **Release** workflow runs on the `release` push. Watch it:

```bash
gh run watch
```

If the tag already exists (`Tag vX.Y.Z already exists`), you forgot to bump the version. Fix:

```bash
git checkout main
# bump manifest.json version higher, commit
git push origin main
git checkout release
git merge main --ff-only
git push origin release
```

### 5. Verify

```bash
gh release view vX.Y.Z
```

Should show:

- Tag `vX.Y.Z`
- Latest = true
- Asset `powerpilz_companion.zip`
- Auto-generated release notes

Optionally, add a hand-written summary:

```bash
gh release edit vX.Y.Z --notes "$(cat release-notes.md)"
```

## Common problems

### "Tag vX.Y.Z already exists"

You pushed `release` without bumping `manifest.json`. Bump the version in `manifest.json`, commit on main, push, then redo the main→release fast-forward.

### Branches have diverged

Someone pushed directly to `release` instead of going through main. Don't force-push. Pull the orphan change back into main first:

```bash
git checkout release
git pull
git checkout main
git merge release --no-edit   # bring the orphan commit into main's history
# resolve any conflicts (usually prefer main's version field)
git push origin main
git checkout release
git merge main --ff-only      # now ff works again
git push origin release
```

### Validate fails on main push

Look at the CI log (`gh run view <id> --log-failed`). Common causes:

- **Python SyntaxError**: a `.py` file has a parse error. The CI runs `ast.parse` on every file under `custom_components/`.
- **JSON parse error**: one of `manifest.json`, `strings.json`, `translations/*.json`, `icons.json` is malformed (trailing comma, missing quote, etc.).
- **Hassfest failure**: `manifest.json` has a metadata issue — wrong `iot_class`, missing `domain`, etc. Look at the Hassfest log for the specific field.
- **HACS action failure**: repo layout doesn't match what HACS expects — e.g. missing `hacs.json`, or the integration folder name doesn't match `manifest.json`'s `domain`.

## Version compatibility with PowerPilz cards

The [PowerPilz](https://github.com/gregor-autischer/PowerPilz) Lovelace cards repo has a Schedule card and a Timer card that both have a native "Companion mode" reading attributes from this integration's entities. The two repos use matching major+minor versions when shipping paired features:

| This integration | PowerPilz cards | Pairing |
| :-- | :-- | :-- |
| v0.3.0 | v0.3.0 | Initial Companion mode on Schedule + Timer cards |

Not a strict requirement — the cards work without this integration (via manual mode), and this integration works without the cards (the entities are regular HA entities usable by any card). Matching versions just makes release-note reading easier.

## When in doubt

- **Something's red and I don't know why** → `gh run list --limit 5` and `gh run view <id> --log-failed`
- **I need to re-release** → bump to the next patch version, push main, ff-merge to release, push release
- **I accidentally published v0.X.Y with a broken integration** → bump to v0.X.Y+1 and release again. Don't force-push or delete tags (HACS caches them).
