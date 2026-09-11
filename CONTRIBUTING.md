# Contributing to FormPilot-AntD

Thanks for your interest in making FormPilot-AntD better! The project welcomes contributions of all sizes — bug fixes, new field scanners, platform adapters, docs improvements.

## Ways to contribute

- **Bug reports** — open an issue with reproduction steps
- **Feature ideas** — open an issue tagged `enhancement`
- **Platform adapters** — add scanners for Element UI / Naive UI / Arco Design
- **Platform presets** — tested URL + field-id conventions for Zhaopin, Beisen, 51job, etc.
- **Docs** — typo fixes, clearer explanations, better examples

## Quick contribution paths

| You want to... | Start here | File to edit |
|---|---|---|
| Fix a radio/select fill bug | `apply_form_plan.py` → `form_filler_v2.py` | `form_filler_v2.py` |
| Add a missing field kind scanner | `dump_form_schema.py` → `SCAN_FIELDS_V2_JS` | `dump_form_schema.py` |
| Support Element UI | new `fill_*` for `.el-select`, `.el-radio-group` | `form_filler_v2.py` |
| Add a platform preset (e.g., Zhaopin) | add entry in README Compatibility | `README.md` |

## Dev setup

```cmd
git clone https://github.com/15373050312xhc-arch/FormPilot-AntD.git
cd FormPilot-AntD
py launcher.py
```

Use the [live demo form](https://15373050312xhc-arch.github.io/FormPilot-AntD/demo.html) as your test target — no login required.

## Pull request checklist

- [ ] Target branch is `main`
- [ ] Tested against the [demo form](https://15373050312xhc-arch.github.io/FormPilot-AntD/demo.html) or a real recruitment form
- [ ] No secrets / personal data in commits
- [ ] If adding a new field kind, add an entry to the demo form `docs/demo.html` so others can test

## Code style

- Minimal edits preferred — don't refactor unrelated code in the same PR
- Keep `fill_*` functions self-contained (don't import internal state from other fills)
- Comments in Chinese or English, both are fine

## Releasing

Maintainers handle releases. The current release process:

1. Bump version notes in `CHANGELOG.md` (if it exists)
2. Tag `vX.Y.0` with annotated tag
3. Create GitHub Release from tag, upload zip of project files
4. Update README Compatibility table if a new platform is supported

## License

By contributing, you agree your contributions are licensed under the MIT license.
