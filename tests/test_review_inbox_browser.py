"""Exercise native focus → observer → private pipe → authenticated SSE → evidence UI."""

import asyncio
from datetime import datetime, timezone
import os
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright, expect

from practice_inbox import PracticeInbox
from src.sigma_demo.pdf import source as synthetic_pdf
from review_companion_practice import CLIENT, PRIOR, RETURN, REVISION, ROOT, TOKEN, PracticeCompanion, run_companion
from sigma_practice_uploads import PracticeObjects
from src.sigma_demo.native import practice_web_profile
from src.sigma_demo.target import sigma_practice_target
from src.preparation.batches import BatchIntake, ClassifiedUpload, CommittedBatchSource, IngestScope
from src.preparation.dispatch import ExtractionTicket, PreparedExtraction, UploadDispatch
from src.preparation.review_events import NativeAddress
from test_preparation_batches import EXTRACTION
from test_preparation_web_navigation import IDENTITY, chromium_executable
from test_preparation_web_observer import _add_w2, _create_draft

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='Synthetic observer transport uses a Unix private pipe')


def _browser_options():
    options = {'executable_path': chromium_executable()}
    display = os.environ.get('SIGMA_TEST_DISPLAY')
    if display:
        options['headless'] = False
        options['env'] = {**os.environ, 'DISPLAY': display, 'WAYLAND_DISPLAY': ''}
        options['args'] = ['--ozone-platform=x11']
    return options


@pytest.fixture(autouse=True)
def built_frontend():
    if not (ROOT / 'review_ui' / 'dist' / 'index.html').is_file():
        pytest.skip('Build review_ui before the companion browser regression')


async def assert_view(page, field, document, citation, page_number):
    await page.wait_for_url(lambda url: all(
        parse_qs(urlsplit(url).query).get(key, [None])[0] == expected
        for key, expected in {'field': field, 'document': document, 'citation': citation, 'page': page_number}.items()
    ))


def test_source_history_keeps_native_focus_and_skips_duplicate_notifications(tmp_path):
    async def exercise(native_origin):
        backend = PracticeCompanion(tmp_path)
        backend.native_profile = practice_web_profile(native_origin)
        options = _browser_options()
        async with async_playwright() as playwright:
            seed = await playwright.chromium.launch(executable_path=options['executable_path'])
            try:
                page = await seed.new_page()
                backend.draft = await _create_draft(page, native_origin, IDENTITY.client_reference)
                row_a = await _add_w2(page, 'Employer A')
                row_b = await _add_w2(page, 'Employer B')
            finally:
                await seed.close()

            backend.legal_addresses = {
                NativeAddress('w2', row_a, 'wages'): 'wages-a',
                NativeAddress('w2', row_b, 'wages'): 'wages-b',
                NativeAddress('w2', row_b, 'withholding'): 'tax-b',
            }
            backend.browser = await playwright.chromium.launch(**options)
            evidence_browser = await playwright.chromium.launch(**options)
            try:
                async with run_companion(backend) as companion:
                    evidence = await evidence_browser.new_page()
                    await evidence.goto(f'{companion}/clients/{CLIENT}/returns/2025?revision={REVISION}')
                    await evidence.get_by_role('button', name='Open review').click()
                    await expect(evidence.get_by_role('button', name='Mark reviewed')).to_be_enabled()
                    competing = await evidence_browser.new_page()
                    await competing.goto(f'{companion}/clients/{CLIENT}/returns/2025?revision={REVISION}')
                    async with competing.expect_response(
                        lambda response: response.url.endswith('/review/events') and response.status == 409,
                    ):
                        await competing.get_by_role('button', name='Open review').click()
                    await expect(competing.get_by_text('Evidence is already following this review in another window.')).to_be_visible()
                    await expect(competing.get_by_role('button', name='Mark reviewed')).to_be_disabled()
                    assert backend.stream_requests == 2
                    native = backend.native
                    assert native is not None and native.context.browser is backend.browser
                    assert evidence.context.browser is not native.context.browser
                    assert await evidence.locator('iframe').count() == 0
                    for employer, field, doc in (('Employer A', 'wages-a', 'doc-a'), ('Employer B', 'wages-b', 'doc-b')):
                        await native.get_by_role('row').filter(has_text=employer).get_by_role('link').click()
                        await native.get_by_label('Box 1 wages').click()
                        await assert_view(evidence, field, doc, f'{doc}-citation-1', '1')
                        await expect(evidence.get_by_role('img', name=f'{employer} original, page 1. Highlighted evidence: Box 1 wages: $100.00')).to_be_visible()
                        if employer == 'Employer A':
                            await native.get_by_role('link', name='Back to W-2s').click()
                    assert backend.stream_requests == 2 and backend.detail_requests == [REVISION, REVISION]
                    await expect(evidence.get_by_text('Source evidence only', exact=True)).to_be_visible()
                    await expect(evidence.get_by_text('Verified after reopening', exact=True)).to_have_count(0)
                    requests = list(backend.native_requests)
                    await evidence.get_by_role('button', name='Employer B original, page 2, line 1').click()
                    await assert_view(evidence, 'wages-b', 'doc-b', 'doc-b-citation-2', '2')
                    await expect(evidence.get_by_role('img', name='Employer B original, page 2. Highlighted evidence: Corroborating wages: $100.00')).to_be_visible()
                    await evidence.get_by_role('button', name='Next PDF page').click()
                    await assert_view(evidence, 'wages-b', 'doc-b', 'doc-b-citation-2', '3')
                    await expect(evidence.get_by_role('img', name='Employer B original, page 3. No evidence highlighted on this page.')).to_be_visible()
                    await native.get_by_label('Box 1 wages').click()  # Same native field; browsing stays put.
                    await assert_view(evidence, 'wages-b', 'doc-b', 'doc-b-citation-2', '3')

                    for field, doc, cite, number in (
                        ('wages-b', 'doc-b', 'doc-b-citation-2', '2'),
                        ('wages-b', 'doc-b', 'doc-b-citation-1', '1'),
                        ('wages-a', 'doc-a', 'doc-a-citation-1', '1'),
                    ):
                        await evidence.go_back()
                        await assert_view(evidence, field, doc, cite, number)
                        await expect(evidence.get_by_text('Active in Sigma: Employer B · Box 1 wages')).to_be_visible()
                    for cite, number in (('doc-b-citation-1', '1'), ('doc-b-citation-2', '2'), ('doc-b-citation-2', '3')):
                        await evidence.go_forward()
                        await assert_view(evidence, 'wages-b', 'doc-b', cite, number)
                    assert backend.native_requests == requests
                    assert backend.stream_requests == 2 and backend.detail_requests == [REVISION, REVISION]
                    await competing.close()
                    assert await native.get_by_label('Box 1 wages').evaluate('(input) => input === document.activeElement')

                    await evidence.get_by_role('button', name='Previous evidence').click()
                    await assert_view(evidence, 'wages-b', 'doc-b', 'doc-b-citation-2', '2')
                    await native.keyboard.press('Tab')
                    await assert_view(evidence, 'tax-b', 'doc-text', 'text-line', None)
                    await expect(evidence.get_by_label('Employer B withholding note, selectable original text with line numbers')).to_be_visible()
                    await expect(evidence.get_by_role('button', name='Next evidence')).to_be_disabled()
                    await native.get_by_label('Employer reference').click()
                    await expect(evidence.get_by_text('Active in Sigma: No linked citation · choose evidence manually')).to_be_visible()
                    await expect(evidence.get_by_role('region', name='Source document')).to_have_count(0)

                    await evidence.get_by_role('combobox', name='Input revision', exact=True).select_option(PRIOR)
                    await expect(evidence.get_by_role('option', name='Employer A · Box 1 wages')).to_have_count(1)
                    await expect(evidence.get_by_role('option', name='Employer B · Box 1 wages')).to_have_count(0)
                    await native.get_by_label('Box 1 wages').click()
                    await assert_view(evidence, 'wages-b', 'doc-b', 'doc-b-citation-1', '1')
                    await evidence.get_by_role('button', name='Verified page only').click()
                    await expect(evidence.get_by_text('Page 2 verified; there is no exact highlight.')).to_be_visible()
                    await evidence.get_by_role('button', name='No verified page').click()
                    await expect(evidence.get_by_text('Page not verified.')).to_be_visible()
                    backend.field_version += 1
                    await evidence.get_by_role('button', name='Mark reviewed').click()
                    await expect(evidence.get_by_role('alert')).to_have_text(
                        'The evidence changed. Refresh and review the current field snapshot.'
                    )
                    assert backend.decisions == 0
                    async with evidence.expect_response(
                        lambda response: '/evidence?' in response.url and response.status == 200,
                    ):
                        await evidence.get_by_role('button', name='Refresh status').click()
                    await expect(evidence.get_by_role('button', name='Mark reviewed')).to_be_enabled()
                    await evidence.get_by_role('button', name='Mark reviewed').click()
                    await expect(evidence.get_by_text('Marked reviewed for this input revision. Nothing was filed.')).to_be_visible()
                    await evidence.get_by_role('button', name='Release review').click()
                    await expect(evidence.get_by_role('alert')).to_have_text('Close the native return before releasing the reservation.')
                    reservation = backend.active
                    assert reservation is not None
                    reservation.expires_at = datetime.now(timezone.utc)
                    await evidence.reload()
                    await evidence.get_by_role('button', name='Open review').click()
                    await expect(evidence.get_by_text('The evidence session expired.', exact=False)).to_be_visible()
                    await expect(evidence.get_by_role('button', name='Mark reviewed')).to_be_disabled()
                    assert backend.active is reservation and backend.native is native and not native.is_closed()
                    await native.close()
                    await evidence.get_by_role('button', name='Release review').click()
                    await expect(evidence.get_by_text('Native review released.')).to_be_visible()
                    assert backend.stream_requests == 2
                    assert all(method == 'GET' for method, _ in backend.native_requests)

                    await evidence.reload()
                    await expect(evidence.get_by_text('Page not verified.')).to_be_visible()
                    assert backend.stream_requests == 2 and backend.native.is_closed()
                    await evidence.goto(f'{companion}/clients/{CLIENT}/returns/2025?revision={REVISION}&field=wages-b&document=doc-a&version=original-one&citation=doc-b-citation-1')
                    await expect(evidence.get_by_role('alert')).to_have_text('This evidence location is unavailable for the selected return revision.')
                    await expect(evidence.get_by_role('region', name='Source document')).to_have_count(0)
            finally:
                await backend.browser.close()
                await evidence_browser.close()

    with sigma_practice_target(tmp_path / 'drafts.sqlite3') as origin:
        asyncio.run(exercise(origin))


def test_drop_keeps_committed_slot_and_allows_independent_retry(tmp_path, coordinator_db):
    async def exercise():
        other_return = 'practice-other-return'
        with coordinator_db.transaction() as connection:
            connection.execute("""INSERT INTO prep_returns (firm_id,return_id,profile_id,store_id,tax_year)
                                  VALUES ('practice-firm',%s,'practice','store',2025),
                                         ('practice-firm',%s,'practice','store',2025)""", (RETURN, other_return))
        intake = BatchIntake(coordinator_db, PracticeObjects(tmp_path))
        intake.install()
        classified = {}
        source = CommittedBatchSource(intake, lambda event, _hint: ClassifiedUpload(classified.get(event, RETURN), 'synthetic'))
        dispatch = UploadDispatch(coordinator_db, source)
        dispatch.install()
        scope = IngestScope('practice-firm', 'practice-preparer', frozenset({RETURN, other_return}))
        inbox = PracticeInbox(tmp_path, intake, scope, policy=EXTRACTION)
        backend = PracticeCompanion(tmp_path, inbox=inbox)
        async with run_companion(backend) as origin, async_playwright() as playwright:
            browser = await playwright.chromium.launch(**_browser_options())
            try:
                page = await browser.new_page()
                await page.goto(f'{origin}/clients')
                await expect(page.get_by_role('button', name='Select files')).to_be_enabled()
                slow_upload = asyncio.Event()
                parallel_put = asyncio.Event()
                first_put = True

                async def hold_first_put(route):
                    nonlocal first_put
                    if first_put:
                        first_put = False
                        await slow_upload.wait()
                    else:
                        parallel_put.set()
                    await route.continue_()

                await page.route('**/api/review/intake/batches/*/files/*', hold_first_put)
                await page.get_by_role('checkbox', name='Approve synthetic files for practice extraction.').check()
                await page.get_by_label('Select PDF or TXT files').set_input_files([
                    {'name': 'good.txt', 'mimeType': 'text/plain', 'buffer': b'Synthetic W-2\nBox 1: 100\n'},
                    {'name': 'broken.pdf', 'mimeType': 'application/pdf', 'buffer': b'bad PDF'},
                    {'name': 'missing.pdf', 'mimeType': 'application/pdf', 'buffer': b'bad PDF'},
                ])
                await expect(page.get_by_text('good.txt', exact=True)).to_be_visible()
                try:
                    await asyncio.wait_for(parallel_put.wait(), timeout=5)
                finally:
                    slow_upload.set()
                await expect(page.get_by_text('good.txt', exact=True).locator('..')).to_contain_text('Committed')
                await expect(page.get_by_text('broken.pdf', exact=True).locator('..')).to_contain_text('complete PDF')
                (event,) = intake.committed_events()
                assert dispatch.consume(event)
                ticket = dispatch.begin_extraction(event, source.grant_id(event), EXTRACTION)
                assert isinstance(ticket, ExtractionTicket)
                assert dispatch.authorize_disclosure(ticket) == 'authorized'
                assert dispatch.stage_extraction(ticket, PreparedExtraction(RETURN, lambda _conn: 'fixture-validated-facts'))
                assert dispatch.settle_staged(event)
                await expect(page.get_by_role('button', name='Extract uploaded documents')).to_have_count(0)
                with coordinator_db.transaction() as connection:
                    assert connection.execute(
                        'SELECT review_required FROM prep_returns WHERE return_id=%s', (RETURN,),
                    ).fetchone()['review_required']
                    assert connection.execute('SELECT count(*) AS n FROM prep_jobs').fetchone()['n'] == 1

                scoped = await browser.new_page()
                await scoped.goto(f'{origin}/clients/{CLIENT}/returns/2025?revision={REVISION}')
                await expect(scoped.get_by_text('good.txt', exact=True)).to_be_visible()
                await expect(scoped.get_by_text('broken.pdf', exact=True)).to_have_count(0)
                await expect(scoped.get_by_text('missing.pdf', exact=True)).to_have_count(0)
                await expect(scoped.get_by_role('button', name='Exclude file')).to_have_count(0)
                original_batch, = intake.list_batches(scope)
                pending_slot = next(slot for slot in original_batch.files if slot.filename == 'broken.pdf')
                denied = await page.request.put(
                    f'{origin}/api/review/returns/{RETURN}/intake/batches/{original_batch.batch_id}/files/{pending_slot.slot}',
                    data='Not independently attributed',
                    headers={'X-Review-Token': TOKEN, 'Content-Type': 'text/plain'},
                )
                assert denied.status == 404
                await scoped.get_by_label('Select PDF or TXT files').set_input_files([
                    {'name': 'other-return.txt', 'mimeType': 'text/plain', 'buffer': b'Synthetic other client'},
                ])
                await expect(scoped.get_by_text('Files appear here only after the firm verifies', exact=False)).to_be_visible()
                other_event, = set(intake.committed_events()) - {event}
                classified[other_event] = other_return
                assert dispatch.consume(other_event)
                async with scoped.expect_response(lambda response: response.url.endswith(f'{RETURN}/intake/batches')):
                    await scoped.get_by_role('button', name='Refresh verified uploads').click()
                await expect(scoped.get_by_text('other-return.txt', exact=True)).to_have_count(0)
                await scoped.close()

                await page.reload()
                await expect(page.get_by_text('other-return.txt', exact=True)).to_be_visible()
                _, corrected_pdf, _ = synthetic_pdf('broken', 'Corrected synthetic PDF', ['Box 1: $100.00'])
                broken_file = page.get_by_text('broken.pdf', exact=True).locator('..')
                await broken_file.get_by_label('Reselect file').set_input_files({
                    'name': 'broken.pdf', 'mimeType': 'application/pdf', 'buffer': corrected_pdf,
                })
                await expect(broken_file).to_contain_text('Committed')
                await page.get_by_text('missing.pdf', exact=True).locator('..').get_by_role('button', name='Exclude file').click()
                await expect(page.get_by_text('Upload complete', exact=True)).to_have_count(2)
                committed = intake.committed_events()
                assert len(committed) == 3 and event in committed
                with coordinator_db.transaction() as connection:
                    assert connection.execute('SELECT count(*) AS n FROM prep_jobs').fetchone()['n'] == 1
                response = await page.request.get(f'{origin}/api/review/returns/{RETURN}/evidence')
                assert response.status == 401
                response = await page.request.get(f'{origin}/api/review/returns/{RETURN}/review/originals/doc-b/original-one?revision={PRIOR}',
                                                  headers={'X-Review-Token': TOKEN})
                assert response.status == 404
            finally:
                await browser.close()

    asyncio.run(exercise())
