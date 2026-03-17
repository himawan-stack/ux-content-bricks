# UX Content Bricks — Notion Automation

This repo watches a Notion database for rows whose **Status** is set to **Needs draft**, looks up similar **Approved** bricks, generates a draft via OpenAI, and writes results back to Notion.

## What it does
For each row where `Status = Needs draft`:
1. Finds approved candidates with the same **Component type** and **Component slot**.
2. Picks the top few closest matches.
3. Generates 2 variants (max) and marks one as recommended.
4. Writes to:
   - `Matched approved`
   - `AI draft`
   - `AI notes`
5. Sets Status to `Needs review` (or whatever you configure).

By default it **won't overwrite** `AI draft` if it already has content.

## 1) Notion setup
In your Notion database, make sure these properties exist:
- **Status** (select)
- **Component type** (select or text)
- **Component slot** (select or text)
- **Approved copy** (text)
- **Matched approved** (text)
- **AI draft** (text)
- **AI notes** (text)

> Property names can be customised via env vars (see below).

## 2) Create a Notion integration
1. Go to Notion: Settings → Connections → Develop or manage integrations.
2. Create a new integration, copy the **Internal Integration Token**.
3. Share your database with the integration (via the database Share menu).

You will also need the **database ID** (from the database URL).

## 3) Add GitHub secrets
In your GitHub repo: Settings → Secrets and variables → Actions → **New repository secret**.

### Required secrets
- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`
- `OPENAI_API_KEY`

### Recommended secrets
- `OPENAI_MODEL` (default in code is `gpt-4o-mini`)

### Optional secrets (only set if your Notion property names differ)
If you leave these empty, the script uses the defaults shown:
- `PROP_TITLE` (default: `Name`)
- `PROP_STATUS` (default: `Status`)
- `PROP_COMPONENT_TYPE` (default: `Component type`)
- `PROP_COMPONENT_SLOT` (default: `Component slot`)
- `PROP_APPROVED_COPY` (default: `Approved copy`)
- `PROP_MATCHED_APPROVED` (default: `Matched approved`)
- `PROP_AI_DRAFT` (default: `AI draft`)
- `PROP_AI_NOTES` (default: `AI notes`)

### Optional secrets (status values)
- `STATUS_NEEDS_DRAFT` (default: `Needs draft`)
- `STATUS_APPROVED` (default: `Approved`)
- `STATUS_AFTER` (default: `Needs review`)

### Optional secrets (behaviour)
- `MAX_ITEMS_PER_RUN` (default: `10`)
- `MAX_CANDIDATES` (default: `25`)
- `OVERWRITE_EXISTING` (default: `0`)
- `DRY_RUN` (default: `0`)

## 4) Enable the workflow
The workflow is in `.github/workflows/run.yml` and runs every 10 minutes, plus manual trigger.

## 5) First test
1. In Notion, set a row's **Status** to `Needs draft`.
2. In GitHub: Actions → "UX Content Bricks Automation" → Run workflow.
3. Confirm the row gets filled.

## Notes / guardrails
- This is intended to **assist drafting**, not to mark anything as Approved automatically.
- Keep your human review step.
- If you change property names in Notion, update secrets (or change defaults in `automation.py`).

## Troubleshooting
- 401/403 from Notion: integration token wrong, or database not shared with the integration.
- 400 from Notion query: property type mismatch; set the `PROP_*` secrets to the correct property names.
- OpenAI errors: check API key, model name, and account access.
