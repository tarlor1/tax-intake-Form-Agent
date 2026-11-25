# Sigma preparation and evidence companion

**Status:** `review.py` serves the evidence companion. The firm-local coordinator provides durable upload, preparation and review services with synthetic regression coverage. Firm integrations and licensed Sigma acceptance have not run. The independent `main.py` portfolio demo defaults to offline processing.

## Coordinator deployment

Pre-provision a dedicated PostgreSQL schema and inject its connection string through `SIGMA_COORDINATOR_DSN`. The command is `python sigma_service.py --schema SCHEMA [--site MODULE:FACTORY] COMMAND`. Run `migrate` first to apply numbered migrations; it needs neither a site factory nor native or model access. All runtime roles require `--site`:

| Command | Responsibility |
| --- | --- |
| `serve [--port PORT]` | Serve the firm API on loopback (default `8766`) behind a firm-controlled HTTPS reverse proxy. |
| `dispatch [--once]` | Replay the local upload outbox and settle eligible extraction attempts. |
| `worker [--once]` | Claim eligible work using this installation's qualified interactive enrollment. |

`--site` selects a trusted installed callable. It receives the coordinator database and returns a [`FirmSite`](../src/preparation/site.py). The site provides firm authentication and scoped assignments, an `ImmutableObjects` adapter, independent upload classification and document verification, and a `NativeAuthority` for checkout and lifecycle proof. Preparation also needs the applicable extraction and reasoning enrollment, pinned provider policies, an enrolled worker, and approved native profiles and mappings. The repository does not provision an SSO issuer or integrate a firm's upstream portal. The portal's durable upload feed, assignment and authorization must be integrated by the site.

The companion's `--sigma-config` points to a credential-free enrollment file; the firm's SSO integration supplies a short-lived individual token through `SIGMA_REVIEW_ACCESS_TOKEN`. The companion forwards that identity to the HTTPS service, not to browser pages or native subprocesses. `review.py --sigma-config PATH --native-review-factory MODULE:FACTORY` enables native review through a separate trusted local factory. It receives the enrollment and a scoped firm JSON client and returns the native review adapter. Without that adapter the enrolled companion cannot open a native review session.

## Uploads and ownership

The browser declares a drop to track its file slots. Each successful file write stores the original in protected, firm-scoped object storage and commits the version and outbox event in PostgreSQL. The storage adapter writes immutable bytes, returns an opaque object key and supplies a verified checksum. PostgreSQL holds the upload metadata and evidence references. A missing, invalid, or excluded sibling cannot block processing of a committed file. Retry or exclude an individual slot; do not replace bytes in a committed slot. Scans without usable text are not supported.

Each uploaded original records an explicit client and tax year. The standalone app stores its own client directory; the enrolled API scopes selections to the firm's authorized roster. Both use the same upload contract. A selection groups documents in the workspace and may suggest a return for independent verification. It cannot authorize a native write or a model disclosure.

`UploadDispatch` verifies the committed version and marks the independently assigned return for review. External extraction requires a current grant for the exact original, provider, account, model, endpoint, and data class. An absent grant blocks the model call, not the return's review notice. An approved upload's durable fact reference can contribute to a job when the known inputs for that return settle. A later attributable file can create a successor revision without disturbing a review already open. Replay does not create another provider attempt or another job for the same revision.

The PostgreSQL reference coordinator reserves one writer per return, shared by workers and preparers. An OS-held session lock separately prevents competing automation in one native login/session. Expiry alone cannot release ownership after an uncertain native write. Reconcile the exact draft after proving the former writer stopped and obtaining an authorized native lock or firm checkout.

## Tax evidence and native work

The enrolled evidence path uses `PostgresTaxStore` to keep document versions, verified facts and citations, revisioned bindings and native observations, and review decisions in the coordinator. The firm API scopes retrieval to the assigned return; original downloads resolve through the protected object store. Keep source values distinct from values proposed, entered, seen, or verified after native reopen. A citation identifies a PDF region, TXT line range, verified page, or only the original document. Matching text alone is not proof of the correct client, form instance, box, or row. Document-only or unresolved data does not authorize entry. A W-2c changes only supported corrected boxes on the identified original; it does not create another W-2 or overwrite a conflicting preparer edit.

The application validates one proposed action and records mutation intent before dispatch. The private MCP child exposes scoped draft operations over stdio, not arbitrary browser/desktop controls or a network listener. Playwright provides the Web connector boundary; Windows UI Automation provides the desktop boundary. Profiles must describe verified return identity, allowed fields, native locking, and applicable save/readback signals. Neither connector qualifies an unidentified screen, process, or draft. No signing, filing, transmitting, or amending capability.

`LocalDocumentIndex` commits cited analysis without a tax application or preparation worker. An enabled `InboxPreparation` adapter consumes authorized analyzed originals separately. The worker builds a field plan from frozen semantic facts; destination-specific mappings, checkout, tool transport, save, and reopen are supplied through `WorkerEnrollment` and the `WorkerNative`/`NativeAuthority` ports. The local practice deployment wires its loopback destination separately through `PracticeDestination`. A destination may have fewer supported fields than the source contains; unsupported facts still appear in evidence.

`tests/test_preparation_web_handoff.py` covers two synthetic W-2s with equal wages and distinct sources. It exercises PostgreSQL, the private Web MCP child and Chromium against a persistent test site, with scripted model responses. It checks reopened values and source identity after the worker exits. It does not qualify a licensed profile or the Windows UI Automation path.

The synthetic desktop regression checks that 1120 expense rows with the same amount remain distinct and that a native “Saved” label cannot acknowledge a reverted child value. It exercises the desktop controller against a simulated UI Automation tree on Linux. Independent reopen in the licensed Windows application remains required.

## Review windows

Edits belong in the actual tax application. The companion displays originals, candidate values, and revisions without embedding or mirroring the form. The Web observer reports opaque field and record selections through a private channel; a single authenticated SSE stream pushes those selections to the evidence window. The native focus path does not call a model or reload the return. Evidence navigation never edits, focuses, or navigates the native form. History identifies what the user is viewing separately from the active native field. A focus event is not evidence that the preparer accepted or saved an edit.

The regression uses a synthetic, persistent tax UI in a separate Chromium window. It exercises native focus, exact record context, cited PDF/TXT viewing, manual browsing, and the browser history API. It does not establish toolbar history or normal field-commit behavior on the firm's browser, Windows accessibility support, or licensed Sigma Save/Reopen.

## Enablement and data boundaries

- Without enrollment, `review.py` runs the persistent document inbox, client directory, extraction, and evidence review. Add `--inspect-archive` to read the legacy SQLite archive without changing it. No archive record automatically becomes an active draft.
- `tests/practice_inbox.py` provides an isolated manual upload test backed by temporary PostgreSQL and original files. It has no provider or native software connection. See the [README](../README.md) for the command.
- Site adapters must supply the firm's identity, protected storage, upstream portal integration, and independent proof of native authority. The repository does not establish a vendor checkout, validate a firm's profiles, or qualify a licensed Web or Windows host.
- Before enabling a licensed profile, obtain written vendor and firm authorization, prove exact draft identity and save/reopen on that build and host, qualify native locking and human review, and verify failure/recovery on synthetic returns. Keep unattended operation off where those checks cannot pass. See the [Sigma plan](../SIGMA_TAX_AGENT_PLAN.md).
- Use synthetic data for verification and acceptance. No live taxpayer data is authorized; in particular, do not disclose live taxpayer documents, application contents, prompts, screenshots or traces to an external AI provider. Approved endpoint, account, model, disclosure policy, security controls, and field coverage are separate decisions. Follow the firm's written security plan and [IRS Publication 4557](https://www.irs.gov/pub/irs-pdf/p4557.pdf).

## Synthetic release checks

Follow the [README verification setup](../README.md#verification) and run `SIGMA_RELEASE_TESTS=1 python -m pytest -q` against a disposable local PostgreSQL instance with the frontend built and Chromium installed. Use a test X11 display for the separate headed-window path. The release setting fails an otherwise successful run if pytest reports any skipped scenarios. It does not replace authorized acceptance on the firm's licensed products, platforms and profiles.
