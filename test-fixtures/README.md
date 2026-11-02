# Test fixtures — folder-nested controller URL resolution

These reproduce the reported case where a Managed Controller lives **inside folders**, so
you can verify that `resolve_controller_url()` builds the correct endpoint.

## The bug (fixed)

For a controller shown in CJOC at `/cjoc/job/releases/job/v2609a/`:

| | value |
|---|---|
| `getFullName()` | `releases/v2609a` |
| **old** resolved URL | `https://host/releases/v2609a` ❌ (treated folders as a nested URL path) |
| **real** endpoint | `https://host/releases-v2609a/` |
| **new** resolved URL | `https://host/releases-v2609a` ✅ |

CloudBees serves each Managed Controller at a **single host-root path segment** with the
folder path **flattened using hyphens**. The fix replaces `/` with `-` when it has to
reconstruct the URL from the full name (i.e. when `getEndpointUrl()` is empty/relative).
An absolute `getEndpointUrl()` from the OC is still trusted as-is.

## 1. Fast offline check (no live environment)

```bash
python -m unittest tests.test_resolve_controller_url -v
```

Pins the flattening behaviour, including the exact `releases/v2609a` case and the
top-level regression guard.

## 2. End-to-end against a live CloudBees CI

**Step A — create folder-nested controllers.** Open the **CJOC** script console
(`<CJOC_URL>/manage/script`) and paste `create_sample_controllers.groovy`. It creates:

```
releases/v2609a              -> releases-v2609a
releases/v2609b              -> releases-v2609b
team-a/qa/controller-x       -> team-a-qa-controller-x
top-level-controller         -> top-level-controller   (unchanged / regression guard)
```

The script prints each `fullName -> expected endpoint` so you can eyeball it. (Starting /
provisioning the controllers is driven by your cluster's managed-controller config — do it
from the UI if you want the full per-controller pass to succeed. Discovery and URL
resolution are exercised regardless.)

**Step B — run the inventory:**

```bash
python generate_environment_inventory.py --oc-url <CJOC_URL>
```

**Step C — verify** in the newest
`output/<stamp>_environment-inventory/operations-center/controllers-discovered.csv`: the
`URL` column for `v2609a` must read `…/releases-v2609a`, **not** `…/releases/v2609a`.

**Step D (optional) — sample jobs.** To give a folder-nested controller a realistic job
ecosystem, open **that controller's** script console
(`<CONTROLLER_URL>/manage/script`, e.g. `…/releases-v2609a/manage/script`) and paste
`create_sample_jobs.groovy`. Re-run the inventory to see them in that controller's
`jobs.csv` / `job-type-summary.csv`.

## Files

| File | Run in | Purpose |
|------|--------|---------|
| `create_sample_controllers.groovy` | **CJOC** script console | Folder-nested Managed Controllers |
| `create_sample_jobs.groovy` | **Controller** script console | Folder-nested sample jobs (freestyle / declarative / scripted / timer) |
| `../tests/test_resolve_controller_url.py` | local Python | Offline unit test of the resolver |
