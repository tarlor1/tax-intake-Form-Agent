# Tax Document Intake and Review

The review companion keeps a PDF/TXT inbox and a separate evidence window for the Sigma Tax Pro draft workflow. The former intake-form preview, mirrored editors, and submit controls are gone. The email-intake demo in `main.py` is independent.

> Use synthetic documents for local testing. This repository does not authorize live taxpayer data or unattended access to Sigma Tax Pro.

## Local review workspace

A drop groups upload progress and retries, not preparation. Each committed file can proceed independently. Work and ownership are scoped to the verified return, so a failed upload for one client does not hold up another. A new document for a return already open for review creates a successor revision; it does not take over the open return.

From the project root, with an activated Python environment and Bun 1.4.4:

Install the PostgreSQL server tools (`sudo pacman -S postgresql` on Arch Linux). You do not need to enable a system database service.

```bash
pip install -r review-requirements.txt
python -m playwright install chromium
(cd review_ui && bun install --frozen-lockfile && bun run build)
python review.py
```

`review.py` starts the backend and opens the built frontend at `http://127.0.0.1:8765`. In **Document inbox**, add or select a client and enter the tax year, then drop selectable-text PDF or UTF-8 TXT files up to 10 MiB. Files in the same drop must belong to the same client and year. `sample_inputs/` works without an API key or tax application. Originals and client assignments survive restarts. Use `--data-dir` to select a persistent workspace; the default is `outputs/review_workspace/`.

The local inbox processes each committed original independently and files it by category. It lists forms mentioned in intake correspondence and extracts amounts only when it can establish the box and cite the original. Click **Open evidence** to inspect the values and their source in the workspace. Only the practice tax form opens in a separate window. Uncertain boxes stay flagged for manual review. Previously uploaded originals are analyzed again when the extraction version changes. OCR scans are not supported.

When extraction finishes, each document moves to its client and tax year under **Saved workspaces → Saved documents**. Search by client name, filename, form type, or document category, or filter by tax year. Earlier uploads appear under **Unassigned documents**; assign them there without re-uploading. The inbox retains pending uploads and files still being extracted.

Files `01`–`10` are intake documents. The [public W-2 and 1099 examples](sample_inputs/SOURCES.md) in `11`–`13` have selectable text and ruled boxes.

The local inbox never opens the old `review.sqlite3`. To inspect that archive separately, run `python review.py --inspect-archive` (with `--data-dir` if needed). If SQLite reports that it needs rollback recovery, keep the archive and its journal together.

To let Gemini prepare separate drafts for those published samples in the existing synthetic tax form, configure `GEMINI_API_KEY` in your shell or `.env` and run:

```bash
python review.py --practice-gemini
```

Check the Gemini approval before upload, or click **Approve Gemini and prepare** in the evidence window for an existing upload. The worker opens a headed browser and writes only supported verified boxes. It saves and independently reopens the draft before **Open practice draft** becomes available. During review, focusing a bound draft field selects its source citation. The original PDFs never go to Gemini; its tool requests contain guarded choices. Other uploads stay local, and no tax return is transmitted or filed.

The web worker offers a `set_fields` action for the writable, verified fields on the current page or record. One model call selects the group; the MCP host enters and checks the values locally. Navigation and Save remain separate actions. The licensed Sigma entry command below is an inspection tool, not an enrolled worker.

For custom dropdowns, the browser helper matches the option inside the active popup, scrolls it into view, and checks that the browser would deliver the click to that option even when the sidebar covers part of it. It verifies the selected value afterward.

If Gemini returns HTTP 503 during preparation, the worker retries the failed model request up to twice within the same authorization and request budget. It does not replay form edits. An exhausted retry leaves the evidence available and does not expose an unverified draft.


### Firm practice integration

Set `SIGMA_COORDINATOR_DSN` from the firm secret store and pre-provision a dedicated PostgreSQL schema. Run the coordinator roles as separate processes:

```bash
python sigma_service.py --schema "$SIGMA_SCHEMA" migrate
python sigma_service.py --schema "$SIGMA_SCHEMA" --site "$SIGMA_SITE_FACTORY" serve
python sigma_service.py --schema "$SIGMA_SCHEMA" --site "$SIGMA_SITE_FACTORY" dispatch
python sigma_service.py --schema "$SIGMA_SCHEMA" --site "$SIGMA_SITE_FACTORY" worker
```

`serve` listens on loopback for a firm-controlled HTTPS reverse proxy and accepts `--port` (default `8766`). `dispatch` processes committed uploads; `worker` runs eligible jobs on an enrolled interactive host. Both accept `--once` for one bounded pass. The trusted, installed `MODULE:FACTORY` named by `--site` accepts the coordinator database and returns a [`FirmSite`](src/preparation/site.py). The firm supplies SSO and assignments, protected originals, independent source verification, native proof, platform profiles, and any upstream portal integration.

Intake, client assignment, and citation extraction do not open the tax application. The worker uses the trusted site's `WorkerEnrollment.native` and field plan to open the exact draft, enter supported values, and verify a reopen. A new Web, desktop, or MCP integration belongs in that adapter layer; it does not change source extraction or the upload contract. Selecting a client on an upload records where it belongs in the workspace. Firm-side attribution and permission to prepare the return still require independent verification.

To check the licensed Sigma Web 2025 entry point without creating a return, run `python -m src.preparation.sigma_licensed_web --profile-directory /absolute/path/to/dedicated-chromium-profile`. Use an existing private profile directory, sign into Sigma if prompted, and select tax year 2025. The check opens the SSN dialog, verifies its field layout, and exits without entering values or pressing **Start Return**. It does not enroll the worker or prepare a draft.

In the authorized practice workspace, add `--check-mock-ssn` to verify keyboard autofocus across the six segments. The command prompts for a reserved mock SSN and never presses **Start Return**.

If Sigma shows its duplicate-return warning, the entry check stops. Do not retry **Start Return** or switch to another identifier to bypass the warning. Reopen the confirmed existing practice draft instead.

The licensed 2025 adapter also has a guarded `exit_summary` operation: open **Exit Return**, confirm it in Sigma's **Ready to go?** prompt, and verify Sigma returned to its client list. This is an exit check, not proof that entries saved. Reopen the same draft and compare values to a source-backed expected result; a summary file checksum only verifies that the captured artifact did not change.

Compare two complete, independently reopened native field inventories with `python -m src.preparation.sigma_saved_compare manual.json gemini.json`. The command reports missing, unexpected, or different fields without printing their values. It rejects a partial capture rather than reporting equality for the subset.

The companion receives an individual SSO token through `SIGMA_REVIEW_ACCESS_TOKEN`. The enrollment file supplies the HTTPS service and profile, without credentials. To enable native review, supply the separate trusted local `MODULE:FACTORY`:

```bash
python review.py --sigma-config "$SIGMA_PRACTICE_CONFIG" \
  --native-review-factory "$SIGMA_NATIVE_REVIEW_FACTORY" --data-dir "$SIGMA_ACCEPTANCE_DIR"
```

These commands do not provision firm integrations or qualify a licensed Sigma profile. Use synthetic data only.

In the synthetic Web review, a separate headed Chromium window acts as the native tax form. Its focused field selects versioned source evidence in the companion. Browsing sources or using evidence Back/Forward leaves the native form alone. The companion does not sign, file, transmit, or amend returns. See the [design and acceptance boundaries](docs/LOCAL_COMPANION_DESIGN.md) and the [local API security model](docs/LOCAL_API_AUTHENTICATION.md).

### Verification

```bash
pip install -r requirements.txt -r review-requirements.txt
SIGMA_RELEASE_TESTS=1 python -m pytest -q
```

Run the synthetic release check on Linux after building the frontend and installing Chromium. Point `SIGMA_TEST_POSTGRES_SOCKET` at the absolute socket directory of a disposable local PostgreSQL instance and set `SIGMA_TEST_POSTGRES_PORT` if it is not `5432`. Set `SIGMA_TEST_DISPLAY` to a test X11 display to exercise separate headed windows. `SIGMA_RELEASE_TESTS=1` makes reported skips fail an otherwise successful pytest run. These are synthetic checks; they require neither licensed Sigma nor model credentials.

### Frontend structure

The frontend follows the [shadcn/ui Vite setup](https://ui.shadcn.com/docs/installation/vite). Reusable UI primitives live separately from feature components; routes load data and compose features, hooks manage stateful workflows, and `lib` contains API contracts and utilities.

```text
review_ui/src/
  components/
    ui/           Shared shadcn buttons, inputs, alerts, progress, and other primitives
    layout/       Application shell
    clients/      Saved client queue
    intake/       Document drop, approval, and per-file status
    review/       Source-first review and return status
    evidence/     PDF and text evidence viewers
    routing/      Route loading and error feedback
  hooks/          Reusable workflow hooks
  lib/            API client, schemas, and shared utilities
  routes/         TanStack file routes
  index.css       Tailwind entry, shared theme tokens, and progress keyframes
```

Use Tailwind utilities and existing shared primitives rather than restoring a component stylesheet. `components.json` configures shadcn, and `@/` resolves to `src/`. Before adding a feature, inspect the package manifest and related components, check installed libraries, and consider an established dependency before writing custom behavior.

## Offline portfolio demo

Adapted from [immigration-intake-Form-Agent](https://github.com/AshwinSanthanakrishnan/immigration-intake-Form-Agent). A tax practice has no "case types", so the intake is built around the taxpayer instead: identity and photo ID, home address, tax year, filing status, dependents, income sources and deductions.

---

## The problem

A tax preparation firm gets a steady stream of inquiries, and many arrive in a rush around filing and extension deadlines. For each one, someone has to:

1. Read the email and work out what the client needs (a late return, an amended return, an IRS notice, next season's return...).
2. Retype the client's details, including their driver's license, into the firm's intake system.
3. Judge how urgent it is. An IRS notice with a response date next month can't wait in a queue.
4. Write back if anything is missing.
5. Prepare a list of documents the client should gather (W-2s, 1099s, 1098s...).

Clients also tend to paste their Social Security number or bank details straight into the email. This project automates the clerical work, keeps those numbers away from the AI, and keeps people in charge of every decision that reaches a client.

## What it does

The demo uses local rules by default to process the fictional inquiries in `sample_inputs/`. Optional online mode uses an explicitly selected provider for the exact shipped fixtures only.

| # | Step | What happens |
|---|---|---|
| 1 | **Read** | Loads the email from a `.txt`, `.pdf`, `.eml` or `.md` file. |
| 2 | **Redact** | Masks Social Security numbers, ITINs, and bank account or routing numbers **in code, before any AI call**. The report notes which emails contained them so staff can steer those clients to a secure channel. |
| 3 | **Extract** | Extracts the contact, identity and tax-year fields required by the local form, along with urgency and available optional tax-profile details. |
| 4 | **Decide** | If any required detail is missing, the record is **not submitted**. The demo saves a draft follow-up for staff review. |
| 5 | **Fill the form** | For complete records, a browser (Playwright) opens the intake form, finds each field by its label, types or checks every value, and clicks Submit. You can watch it happen. |
| 6 | **Verify** | The program checks that a confirmation and intake ID appeared **and** that the intake system stored the same details. It marks the record PASS or FAIL and saves a screenshot. |
| 7 | **Checklist** | For each accepted demo client, drafts a document checklist from their tax profile for preparer review. |
| 8 | **Report** | Writes a Markdown report and, when PDF rendering succeeds, a PDF copy in the run directory. |

One bad file never stops the run. The problem is recorded in the report and the next file is processed.

## Quick start

You need Python 3.10 or newer.

```bash
git clone https://github.com/AshwinSanthanakrishnan/tax-intake-Form-Agent.git
cd tax-intake-Form-Agent

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python main.py
```

A browser window opens the local demo form. Results go to a fresh `outputs/runs/run-*` directory. No API key is needed, and an API key in the environment does not enable online mode.

### Optional online synthetic run

Set `GEMINI_API_KEY` for Gemini or `ANTHROPIC_API_KEY` for Claude in your environment or local `.env` (see `.env.example`). Explicitly select one provider:

```bash
python main.py --online-synthetic gemini
# Or:
python main.py --online-synthetic claude
```

The CLI checks the filenames and SHA-256 digests of the exact ten shipped fixtures listed below, then processes a private copy of those verified bytes. Missing, changed or additional supported inputs fail validation. `--inputs` cannot be combined with `--online-synthetic`; the CLI does not fall back to another provider.

### Other options

| Command | What it does |
|---|---|
| `python main.py --headless` | Runs the browser invisibly. |
| `python main.py --slow-mo 800` | Slows browser actions, useful for screen recordings. |
| `python main.py --inputs my_folder` | Reads fictional emails from a different folder in offline mode. |
| `python main.py --offline` | Explicitly selects the default local rule-based mode. |
| `python main.py --online-synthetic gemini --model <name>` | Overrides the selected provider's model; `--model` requires online mode. |
| `./run.sh` | Sets up the environment if needed, then runs the project. |
| `python -m pytest` | Runs the tests (no internet or API key needed; the end-to-end test uses Playwright's Chromium). |

The demo exits `0` when there is no technical processing failure, `1` for a processing, verification or checklist failure, and `2` for setup or command-line errors. An expected follow-up for missing information does not itself fail the run.

## Sample data

`sample_inputs/` contains 10 fictional inquiries; four are PDF intake questionnaires.

| File | Situation | Expected path |
|---|---|---|
| `01_w2_extension_priya_nair.txt` | W-2 employee, extended 2025 return due this week | Submitted, high urgency |
| `02_joint_return_planning_daniel_okafor.txt` | Married couple, two kids, new preparer for next season | Submitted, low urgency |
| `03_freelancer_unfiled_messy.txt` | Messy phone email, freelancer with crypto sales, no license or contact details | **Follow-up drafted** |
| `04_rideshare_estimated_tax_luis_ortega.pdf` | Rideshare driver setting up estimated payments | Submitted |
| `05_widowed_retiree_harold_jensen.txt` | Retiree with pension, Social Security and a rental; **expired license** | Submitted, flagged for a current ID |
| `06_irs_notice_cp2000_angela_brooks.pdf` | IRS CP2000 notice; the form includes an **SSN** | Submitted, SSN redacted before AI |
| `07_amend_head_of_household_jasmine_reed.txt` | Single parent wanting to amend a self-prepared return | Submitted |
| `08_relocation_rsu_planning_elena_vasquez.pdf` | Moved states mid-year, RSUs and HSA | Submitted |
| `09_rental_condo_sanjay_patel.txt` | New rental condo; emailed **bank routing and account numbers** | Submitted, bank numbers redacted |
| `10_unfiled_years_levy_notice_terrence_walker.pdf` | Two unfiled years and an IRS levy notice; license "at appointment" | **Follow-up drafted** |

## What you get

Each `outputs/runs/run-*` directory holds the reports and artifacts from one run:

| Path | Contents |
|---|---|
| `run_report.md` | Summary of the whole run. |
| `run_report.pdf` | PDF copy, when rendering succeeds. |
| `screenshots/` | A screenshot of each confirmation page. |
| `followups/` | Draft replies to clients with missing information. |
| `checklists/` | Draft document checklists, one per accepted client. |
| `needs_review/` | Records the AI couldn't process reliably, for a person to handle. |
| `logs/` | A detailed log of each run. |

| The form, filled in by the browser | The verified confirmation |
|---|---|
| ![Filled intake form](docs/intake_form_filled.png) | ![Confirmation with intake ID](docs/confirmation.png) |

## How the code is organized

| File | Job |
|---|---|
| `main.py` | Runs the independent local portfolio demo. |
| `src/input_reader.py` | Finds and reads the email files, including PDFs. |
| `src/redaction.py` | Masks SSNs, ITINs and bank numbers before anything reaches the AI. |
| `src/ai_client.py` | Talks to the AI and holds the instructions it is given. |
| `src/extraction.py` | Checks the AI's answer and retries once if it's invalid. |
| `src/models.py` | Defines what a valid intake record looks like, and the form's field labels. |
| `src/form_server.py` | The local stand-in for the firm's intake system. |
| `src/form_filler.py` | The browser automation that fills in and submits the form. |
| `src/verification.py` | Confirms each submission landed and takes the screenshot. |
| `src/drafts.py` | Saves follow-up emails and checklists with review banners. |
| `src/reporting.py` | Builds the run report (Markdown). |
| `src/report_pdf.py` | Renders the run report as a PDF. |
| `src/pipeline.py` | Runs the steps above for each file. |
| `src/offline_ai.py` | The default local keyword-rule engine. |
| `intake_form/index.html` | The demo intake form. |
| `tests/` | Automated tests, including an end-to-end browser test. |

## Privacy and safety

- **Fictional data only.** Every person, address, email, phone number and ID number in the project is made up, and so is the firm named on the form.
- **Local practice form only.** The browser visits the local demo server. The default mode makes no external AI requests; explicit online mode uses the selected provider for verified synthetic fixtures.
- **Redaction before extraction.** The demo masks recognized Social Security, ITIN and bank-account or routing-number patterns in code. This does not authorize using real data.
- **Driver's license fields.** The local form requires a driver's license number, which may be included in synthetic online extraction. The confirmation page and reports mask the number.
- **Humans stay in control.** Nothing is ever sent to a client. Follow-ups are saved as drafts marked `DRAFT - NOT SENT`, and checklists are marked `DRAFT FOR PREPARER REVIEW. NOT TAX ADVICE.` The program adds these labels itself, so the AI can't leave them out.
- **Incomplete means not submitted.** A record with missing details is never entered into the system. The program, not the AI, decides what counts as missing.
- **Share as little as possible.** The checklist step sends the AI only the tax profile (tax year, state, filing status, dependents count, income and deduction categories), never the client's name, contact details, address, date of birth or ID numbers.
- **Keep credentials and artifacts private.** `.env` and `outputs/` are ignored by git. Neither the portfolio demo nor the companion is authorized for live taxpayer data.
