# Local Tax Review Workspace

**Status: local prototype implemented; production design remains for discussion.** Unchecked decisions remain open. Prototype authorization does not authorize live taxpayer-data processing or commit every feature to the six-week engagement.

**Current slice:** document-first mixed-client/year intake for selectable-text PDFs and UTF-8 TXT, consent-gated Gemini extraction using the original normalized intake model, AI drafts with locally matched source locations, durable automatic preparation jobs, and UI-only Playwright control of the unchanged `intake_form/index.html`. Literal-answer and non-overlapping source-line gates are not part of extraction. Grouped editable fields, a view-only actual-browser preview, and document review share one page; preparer reasons are optional. TaxSlayer, OCR/photos, adaptive form discovery, and Pro Desktop automation remain unavailable. Worker supervision currently requires Linux.

## 1. Goal and boundaries

Extract tax-document information and automatically fill as much of a supported TaxSlayer return as possible. The tax preparer reviews the saved draft afterward, with citations and conflicts alongside it. The agent continues through supported pages without waiting for the preparer after each page.

The controller, storage, and browser sessions run on the preparer's machine. Gemini receives document text externally only after explicit approved-data consent. The companion retains evidence, provenance, conflicts, history, and local browser observations. The current stand-in has no draft-saving capability; only its native human submission produces a receipt, held in memory by that website. The companion never signs, files, transmits, or amends a tax return.

> **Known routing:** individual returns use **TaxSlayer ProWeb**; business returns use **Pro Desktop**. They need separate integration adapters. Authorized access, supported forms, and the unified application-review experience remain open; this slice connects to neither product.

## 2. Architecture and proposed stack

One launcher starts the companion and independently serves the original intake directory on a separate HTTP origin. The companion receives only its URL. The review page combines grouped editors, source evidence, and a view-only full-page image of the actual Chromium browser. Editors are local drafts, not a second destination form: Apply uses ordinary Playwright controls and saves locally only after matching readback. Original target HTML, validation, and submission code remain unchanged. No injected bridge, target helper calls, shared credential, or direct target API write is used.

| Component | Proposed technology | Responsibility |
|---|---|---|
| Dashboard | React + TypeScript + Vite + TanStack Router; Bun | Generated file routes, registered router types, typed navigation/search, runtime validation, client queue and review. |
| Local controller/API | Python + FastAPI + Pydantic | Authenticate commands, manage workflow, and validate model output. |
| Workers | Supervised Python processes | Durable SQLite queue; two concurrent processes by default. Current supervision is Linux-specific. |
| Browser control | Playwright Python + Chromium | Native input/select/checkbox interaction, readback, and human handoff on the original intake stand-in. Real ProWeb needs an authorized adapter. |
| Workflow storage | SQLite; controller manages writes | Persist client/year associations, AI/source/preparer provenance, originals, observations, receipts, and job ownership. |
| Documents | Local SQLite blobs + pdfplumber / UTF-8 text; OCR deferred | Preserve originals and extract readable text; AI drafts need no manufactured literal citations. |
| Evidence viewer | PDF.js + line-based text viewer | Highlight original PDF regions or TXT lines beside the actual browser. |
| Prototype model | Google Gen AI SDK; `GEMINI_MODEL` | Default `gemini-3.5-flash-lite`; original intake normalization for multiple records, one validation-feedback retry, and preparer review. |
| Credentials | Per-launch API token; gitignored `.env` | `GEMINI_API_KEY` stays server-side; OS credential storage remains proposed. |

**TanStack Router, Bun, and Playwright for ProWeb are selected.** Route generation precedes strict TypeScript checking; invalid paths, missing/wrong params, and wrong search types fail compilation. The remaining production stack and packaging need review. Redis, Celery, PostgreSQL, Docker, and a general-purpose agent framework are unnecessary for this workstation baseline.

### Communication

The dashboard calls an authenticated FastAPI service bound to `127.0.0.1`. Commands enqueue/cancel work, upload/extract documents, apply whole-field values, choose a source, or explicitly submit. GET requests poll status, observations, and bounded in-memory full-page JPEGs. Preview scrolling is native client-side scrolling; it sends no wheel commands and is independent of image polling. The target is at least 30 FPS while scrolling, not a guarantee of a 34 ms maximum for rendering, image refresh, or command completion. Processing and retained browsers continue when the dashboard closes. Service shutdown closes browsers; saved observations survive, but unsent browser drafts do not. Restart requires explicit reopening and never replays a submission.

The controller owns SQLite writes and coordinates processes through narrow duplex messages. Whole-field edits and source decisions are revision-checked and recorded only after matching native readback. Failed managed changes cannot later become implicit preparer edits through background observation. Explicit submission clicks the real website's existing button and preserves native validation. No JavaScript or arbitrary URL is accepted. The retained low-level input endpoint also permits only fixed bounded browser actions.

## 3. Intended end-to-end workflow

1. **Upload:** Drag/drop mixed-client PDF/TXT files without creating clients first. Reject unsupported scans/photos and detect duplicate bytes.
2. **Extract/group:** After approved-data consent, Gemini returns separate normalized records for every detected client/entity and requested tax year. Reuse a unique normalized client name when identifying details do not conflict. Ask on ambiguity or conflicting details; missing names/years/types can be assigned. No mandatory SSN, literal-answer matching, or non-overlapping line ownership.
3. **Fill:** After the extraction batch commits, automatically queue a separate original-form session per assigned client/year using its latest revision. Fill owned-document AI drafts, preparer entries, and workspace identity. Preserve conflicting values for review and stop before submission. The original intake controls remain the current fixed mapping; no general adaptive form discovery or TaxSlayer adapter is implemented.
4. **Observe:** Read native control values without changing the target's code. Preserve native constraints and show missing information or conflicting evidence. Observations confirm what the browser contains, not that an external save occurred.
5. **Hand off:** Release the automation slot but retain the actual browser before submission. Show a full-page view-only preview that scrolls locally; `--show-workers` also permits interaction with a native window.
6. **Review:** Edit supported fields in grouped local controls beside source evidence. Apply a complete value or choose a source candidate through Playwright; confirm matching readback before recording the decision. Background polling preserves unsaved local edits. Reasons are optional. Selecting a field/evidence never accepts a value.
7. **Submit:** After explicit preparer confirmation, Playwright activates the original **Submit intake** button. Its own JavaScript performs the normal request and native validation still applies. Observe its confirmation and intake ID. The runtime never queries the stand-in's internal submission store.

Keep a return's source mutations locked while its browser is owned. Explicitly close the browser before attaching/extracting new sources or opening another session for that return. Closing warns about unsent work. Valid committed preparer edits remain locally recorded; incomplete native edits may exist only in that browser.

## 4. Agent, models, and browser actions

The “agent” is the Python workflow plus a model adapter. The model returns typed, normalized intake drafts; application code applies the original model validation and associates clients/years without claiming semantic answers are independently verified quotations. The original documents remain available for review. Playwright performs ordinary UI actions and native readback. DOM evaluation observes controls, focus, validity, and confirmation; it is not a target integration API.

Use explicit, tax-year-specific page adapters: page recognition, field locators, allowed values, save/navigation controls, and verification checks. Stop on unfamiliar pages, ambiguous fields, or wrong-client identity. Existing demo selectors are not a working TaxSlayer integration.

The review app uses Gemini for identity/grouping and the actual intake field contract. Categorical choices require explicit evidence; checkbox values use the form's allowed codes. Dates and numeric bounds follow the original website. Missing ID, DOB, urgency, or other answers are never invented. The original intake CLI remains a separate workflow with Gemini/Claude adapters and an offline substitute; that substitute is not document extraction.

Gemini is external despite local browsers/storage. Live taxpayer data remains prohibited under the current agreement. The free tier may use submitted content for product improvement; use only approved synthetic/redacted documents. Production inference/provider approval remains open.

## 5. Citations, conflicts, and saved state

Every populated input needs support:

- **Document value:** immutable document/version, page and highlighted region; text sources may use line/span references.
- **Workspace identity:** only exact known name/year metadata, labelled separately from source evidence.
- **Preparer entry:** explicit preparer provenance; reason and documentary references are optional. References provide context, not proof of the edited value.

A matching quote does not prove the correct taxpayer, year, or tax box was selected. Verify both source location and meaning. Keep missing, zero, false, and not applicable distinct.

For conflicts, retain every candidate/citation, including original and corrected W-2s. The preparer can choose an intake candidate or apply a custom field value. Conflicting sources remain issues until resolved; the newest upload is not automatically authoritative.

Local source matching enriches detached presentation data without changing saved values, candidate identity, review provenance, revisions, or owned-browser signatures. It uses actual source text and locations, including normalized dates, and leaves ambiguous/inferred answers without exact highlights. Existing stored drafts can gain locations without a new model request or renewed consent. A matching location does not promote an AI draft to independently verified evidence.

Record explicit field edits only after successful matching browser readback, with preparer provenance and an optional reason. Valid edits made directly in a native worker window may also be recorded on commit/blur; partial/invalid native edits remain browser-only observations. Local recording does not establish external persistence.

Persist client/year identity, originals, document versions, proposals/citations, human decisions, observations, receipt IDs, revisions, worker ownership, and history. Browser snapshots are bounded observations, not a replacement destination database.

A new database starts empty and is never reseeded. Legacy worksheets are archived intact as read-only facts, including values, citations, corrections, and events. Do not split an old mailing-address string or map wages into unrelated controls. Compatible phone facts can be reused. Old extraction groups are preserved and require renewed consent before extraction against the new contract.

Before real TaxSlayer integration, design reconciliation against actual saved destination values. The stand-in has no Save Draft and cannot prove a ProWeb save or restore unsent drafts after shutdown. Test code may inspect its stored receipt after real UI submission; runtime code receives only the target URL and uses the visible browser.

## 6. Concurrency and recovery

Process clients concurrently with separate limits for model requests and active browser sessions. Each worker has an isolated assignment/profile; only one writer may edit a return. Approximately ten concurrent clients is a capacity objective to evaluate, not proven capacity or vendor permission.

Handoff releases an automation slot while retaining its browser. Default automation concurrency is two; total retained/active browser capacity is `max(4, workers)`. Additional jobs wait. Sessions are not silently evicted or expired; explicit close/cancel must finish process cleanup before replacement. Confirm TaxSlayer account/session restrictions before real integration.

On failure, record the stage and recovery action while other clients continue. Before restarting a job, confirm the old worker has stopped and reconcile actual saved TaxSlayer values. A timed-out Save may have succeeded; blindly retrying can duplicate or overwrite data. Display technical errors separately from missing information and authentication requests.

## 7. Essential security and acceptance

Local execution reduces hosted infrastructure but does not establish compliance. Follow the company's security checklist/WISP and obtain appropriate review. Protect the loopback API with authentication, Host/Origin validation, and CSRF controls. Keep secrets out of URLs and logs.

Use approved storage, permissions, encryption, backups, retention, and credential handling. SQLite is not encrypted by default. Browser monitoring retains bounded local in-memory JPEGs, not external traces; screenshots can still contain sensitive data. Never bypass MFA or vendor restrictions.

The companion restricts its own actions; it cannot guarantee that a human cannot file directly in TaxSlayer. Final filing authorization remains separate from implicit draft review.

**Verification:** focused regressions cover mixed-client/year grounding, consent/key failures, optional reasons, legacy preservation/re-extraction, source isolation, queue ownership, cancellation/restart, and real Chromium interaction with the original form. Native validation prevents incomplete submission; human browser activation produces a real stand-in receipt whose fields tests inspect. Browser UI checks exercise the combined surface and source selection. No live Gemini call, extraction-quality evaluation, or TaxSlayer connection is claimed. See README for commands.

A demonstration uses synthetic/approved redacted data. A pilot needs approved accounts, storage, providers, bounded coverage, and measured quality. Live use additionally needs company security/production authorization, support ownership, and documented review controls. Handoff includes company-controlled source, pinned dependencies, setup/deployment instructions, test results, and open issues.

## 8. Decisions to make

Check an item only after recording the chosen outcome; a recommendation is not a decision.

- [x] **ProWeb automation:** Use Playwright. This does not select a Pro Desktop automation tool.
- [x] **Prototype model:** Gemini via local `.env`; default `gemini-3.5-flash-lite`. This does not approve live taxpayer processing or select a production provider.
- [x] **Capacity approach:** Measure capacity on target hardware; do not promise ten sessions without evidence.
- [x] **Dashboard routing:** TanStack Router generated file routes and registered router types; strict TypeScript, runtime URL/API validation, and Bun builds.
- [x] **Intake UX:** Drag/drop batches may contain multiple clients/years; selectable-text PDFs and UTF-8 TXT first. Scans/photos/OCR deferred.
- [x] **Review UX:** Grouped editable fields, view-only unchanged intake website, and evidence on one page; whole-field Playwright application, optional reasons, and explicitly confirmed native submission.
- [ ] **Destination access:** TaxSlayer permission is expected but not verified. Obtain written authorization and authorized practice access for the intended reading, filling, field monitoring, and combined view.
- [ ] **Coverage:** Assigned document types, formats, tax years, fields/pages, exclusions, and six-week responsibilities.
- [ ] **Platform/stack:** Target OS/hardware, proposed components, installer, signing, and updates.
- [ ] **Production inference:** Provider/account ownership, approved data handling, local versus external requirements, exact model evaluation, licensing, and fallback policy.
- [ ] **Review authorization:** Final acceptance boundaries, real destination takeover, and acceptable preparer attestations.
- [ ] **Conflicts:** Qualified-reviewer rules for provisional values, corrected-document precedence, and readiness blockers.
- [ ] **Concurrency limits:** Choose operational limits after measurement and vendor approval; the current bounded, explicit-close browser retention policy is a prototype decision.
- [ ] **Security/operations:** Company checklist, API session bootstrap, storage/encryption, credentials, retention, backups, logs, repository, and support owner.
- [ ] **Acceptance/budget:** Representative examples, qualified reviewer, quality thresholds, volume, latency, costs, and pilot/live authorization.

### Key references

- [TaxSlayer ProWeb saving](https://support.taxslayerpro.com/hc/en-us/articles/6792410315162-ProWeb-To-ensure-that-your-data-is-being-saved-properly-warning): generally requires Save/Continue; persistence must be verified.
- [ProWeb best practices](https://support.taxslayerpro.com/hc/en-us/articles/5412206343578-ProWeb-Best-Practices): one working tab per browser; separate sessions still need authorization.
- [IRS Publication 4557](https://www.irs.gov/pub/irs-pdf/p4557.pdf) and [WISP guidance, Publication 5708](https://www.irs.gov/pub/irs-pdf/p5708.pdf).
- [TaxSlayer Pro Terms of Service](https://www.taxslayerpro.com/company/terms-of-service/), Section 9, “Unauthorized Integrations”: written authorization is a prerequisite for the intended integration.
