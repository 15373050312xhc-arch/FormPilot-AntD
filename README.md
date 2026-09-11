# FormPilot-AntD — LLM-Agnostic Form Autofill for Ant Design

Fill long job-application forms in seconds, not hours. FormPilot-AntD extracts any Ant Design form's schema, lets you plan fills with any LLM you already use (Doubao, DeepSeek, Kimi, ChatGPT, Claude — your choice), then replays the plan into the live page — **no API key, no cloud calls, no per-fill cost.**

## Why FormPilot-AntD

| Pain with existing tools | What FormPilot-AntD does differently |
|---|---|
| **Pay-per-fill API model** — most auto-fill SaaS charge per form or require an OpenAI key | **Zero-cost client-side planning.** Dump the schema, paste the prompt into whatever LLM you already pay for, save the JSON. No key handling, no rate limits, no surprise bills. |
| **Opaque "AI matched it" results** — you click Fill, hope for the best | **Human-in-the-loop by design.** The plan is a plain JSON file you can read, edit, and re-run. You see every value before it touches the form. |
| **Tied to one LLM vendor** — the tool's prompt is baked in | **LLM-agnostic.** The prompt is a text file. Switch between Doubao / DeepSeek / Kimi / ChatGPT / Claude per form without changing a line of code. |
| **Fragile CSS selectors break on React re-renders** | **Semantic anchoring first.** Uses DOM `id` / `name` / `data-*` anchors; falls back to `label + section` fuzzy match. Stable across re-renders and refactors. |
| **`value`-based radio/select matching** — breaks when business values are UUIDs like `"opt_3a2f"` | **Label-text matching via `pick_label`.** Matches the human-visible text ("是"/"否"), not the internal value. Survives value renames. |
| **Browser extension = permanent permission grant** to read every page you visit | **Local Python script + your own Edge profile.** No extension, no background process, no telemetry. Runs only when you launch it. |
| **One-shot fill — if it fails you start over** | **Retry with re-stamp.** On React re-render losing the temp anchor, the tool re-stamps `data-resume-autofill-id` and retries the fill — no manual restart. |
| **Generic fillers miss Ant Design internals** (cascader, picker, custom dropdown) | **Built for Ant Design.** Dedicated scanners for `.ant-select`, `.ant-picker`, `.ant-cascader` containers — the fields generic tools skip. |

## How It Works

```
┌─────────────┐     ┌───────────────┐     ┌─────────────┐
│  1. DUMP    │────▶│  2. PLAN      │────▶│  3. APPLY   │
│  (local)    │     │  (any LLM)    │     │  (local)    │
└─────────────┘     └───────────────┘     └─────────────┘
Extract form        Paste prompt into     Save plan as
schema + prompt      your LLM client       form_plan.json
                    Get JSON back          Click "Fill"
```

### Step 1 — Dump (local, ~5 seconds)
Launches Edge with your existing profile. You log in, expand all "Add" sections (education, internships, awards — as many as your resume needs), then click the purple "Dump Form" button. The tool:
- Strips scripts/styles, saves clean HTML
- Extracts a **minimal semantic outline** (form/label/input + key attrs) — ~90% smaller than raw DOM
- Scans field anchors via 3 strategies: main scan → `.ant-select`/`.ant-picker`/`.ant-cascader` container scan → radio options extraction
- Assembles a self-contained prompt with the outline + your resume JSON embedded

### Step 2 — Plan (your LLM, your rules)
Copy `outputs/form_prompt.txt` into any LLM client. The prompt is structured so the model returns a JSON plan — one entry per field, each with `anchor_id`, `kind`, `value` or `pick_label`, and a `note` explaining the reasoning. You:
- Review the plan (every value is visible before it touches the form)
- Edit any value you disagree with
- Save as `outputs/form_plan.json`

### Step 3 — Apply (local, ~30 seconds for 80 fields)
Re-launch the tool, enter the same form page, click the green "Apply Plan" button. The tool:
- Stamps each target element with a temp `data-resume-autofill-id`
- Dispatches by `kind`: text / textarea / dropdown / radio / cascader / date / checkbox
- Matches radio/select by **visible label text** (`pick_label`), not internal `value`
- On React re-render losing the temp anchor → **re-stamps and retries**
- Never auto-submits — you review before clicking submit

## Quick Start

### Requirements
- Windows 10/11
- Python 3.10+ (3.11 recommended)
- Microsoft Edge

### Run

```cmd
Double-click RUN.cmd
```

First launch auto-installs Playwright + PyMuPDF and downloads the Chromium browser engine.

### Usage

1. Choose `[3] v3 mode → [1] Dump form schema`
2. Browser opens — log in to the target site, navigate to the form page
3. Manually expand all "Add" buttons (education / internship / award sections, as needed)
4. Click the purple "Dump Form" button (top-left) or press `Ctrl+Shift+D`
5. Tool generates 4 files in `outputs/`:
   - `form_schema.json` — field anchor list
   - `form_outline.txt` — minimal DOM structure (embedded in prompt)
   - `form_prompt.txt` — ready-to-paste LLM prompt
   - `form_page.html` — full HTML (backup)

6. **Copy the full content of `form_prompt.txt`**, paste into your LLM client (Doubao / DeepSeek / Kimi / ChatGPT / Claude — your call)
7. Save the returned JSON as `outputs/form_plan.json`

8. **Re-run `RUN.cmd → [3] v3 mode → [2] Apply form plan`**
9. Browser opens — enter the same form page
10. Click the green "Apply Plan" button (top-left) or press `Ctrl+Shift+P`
11. Tool fills all fields automatically — review before submitting

## Key Design Decisions

### Why client-side planning instead of API calls?

| API-based tools | FormPilot-AntD |
|---|---|
| Need API key management | **No key needed** |
| Pay per request | **Free** — use any LLM subscription you already have |
| Resume data sent to cloud | **Stays local** — you paste it into your own LLM client |
| Locked to one model | **Switch freely** — different LLM per form if you want |
| Hard to debug "why this value?" | **Plan is a JSON file** — read every `note` field |
| Rate-limited | **No rate limits** — your LLM client's quota is the only ceiling |

### Anchoring strategy

1. **id-first** — `resume_basicInfo_name` and similar semantic ids
2. **label fallback** — when id is missing, match by `label + section` text
3. **refresh-stable** — ids don't depend on DOM structure, survive React re-renders

### Radio / select matching

- Uses **`pick_label` (visible text)** instead of internal `value`
- 5-level Strategy cascade: label tag → `.ant-radio-wrapper` → JS direct DOM manipulation
- Survives value renames (`"opt_3a2f"` → still matches "是")

## File Structure

```
FormPilot-AntD/
├── RUN.cmd                   ← Launch entry
├── launcher.py               ← Orchestrator + auto-install deps
├── .gitignore
├── README.md
├── outputs/
│   ├── dump_form_schema.py   ← Step 1: extract schema
│   ├── apply_form_plan.py    ← Step 2: replay plan
│   ├── form_filler_v2.py     ← fill_* function library
│   ├── resume_data.json      ← Example resume data
│   ├── form_plan.json        ← Example fill plan
│   ├── form_schema.json      ← Example field schema
│   └── __init__.py
└── work/                     ← Browser profile (auto-created on first run)
```

## Compatibility

| Form type | Support |
|---|---|
| Ant Design forms | **Full support** |
| Element UI forms | Requires extending fill_* functions |
| Native HTML forms | Partial support |
| iframe-nested forms | Not supported |
| Shadow DOM forms | Not supported |

## Tech Stack

- **Python 3.11** + Playwright (browser automation)
- **Microsoft Edge** (Chromium engine)
- **Ant Design** component library adapter

## Disclaimer

This tool is for educational purposes only. Users must comply with the target website's Terms of Service and applicable laws. The author assumes no responsibility for any consequences arising from the use of this tool.

## License

MIT
