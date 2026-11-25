"""Exercise the meeting demo through its real browser upload and review controls."""

import asyncio
import os
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from src.preparation.batches import BatchIntake, ClassifiedUpload, CommittedBatchSource
from src.preparation.coordinator_db import migrate
from src.preparation.dispatch import ExtractionPolicy, UploadCommitted, UploadDispatch
from src.preparation.firm_api import Principal, create_firm_app
from src.preparation.originals import FilesystemOriginals
from src.preparation.ownership import OwnershipStore
from src.preparation.tax_store import PostgresTaxStore
from src.sigma_demo.app import _live_settings, run_interactive
from src.sigma_demo.documents import SUBJECT_NAME, TAX_YEAR
from src.sigma_demo.native import PracticeAuthority


def test_firm_intake_roster_scopes_storage_without_granting_disclosure_or_attribution(tmp_path, coordinator_db):
    db = coordinator_db
    migrate(db)
    with db.transaction() as connection:
        for return_id, client_id in (('allowed', 'client-a'), ('other', 'client-b')):
            connection.execute(
                """INSERT INTO prep_returns (firm_id,return_id,profile_id,store_id,tax_year)
                   VALUES ('firm',%s,'profile','store',%s)""", (return_id, TAX_YEAR),
            )
            connection.execute(
                """INSERT INTO prep_return_directory
                   (firm_id,return_id,client_id,client_name,client_reference,return_type,product)
                   VALUES ('firm',%s,%s,%s,%s,'individual','web')""", (return_id, client_id, SUBJECT_NAME, client_id),
            )
    originals = tmp_path / 'originals'
    originals.mkdir(mode=0o700)
    objects = FilesystemOriginals(originals)
    intake = BatchIntake(db, objects)
    principal = Principal('firm', 'actor', frozenset({'allowed'}), 'seat', 'profile')
    access_token = 'synthetic-practice-token'
    app = create_firm_app(
        authenticate=lambda token, seat, profile: principal if token == access_token else None,
        db=db, tax_store=PostgresTaxStore(db), intake=intake, objects=objects,
        ownership=OwnershipStore(db, native_authority=PracticeAuthority()),
        extraction_policy=ExtractionPolicy('gemini', 'practice', 'local-deterministic', 'synthetic',
                                           'https://network-denied.invalid'),
    )
    prefix = '/api/sigma/v1'
    endpoint = f'{prefix}/intake/batches'
    manifest = {
        'batchId': 'synthetic-upload',
        'approvedForAI': False,
        'files': [{'slot': 'txt', 'filename': 'generated.txt', 'mediaType': 'text/plain'}],
    }
    assignment = {'clientId': 'client-a', 'taxYear': TAX_YEAR}
    with TestClient(app, headers={'Authorization': f'Bearer {access_token}', 'X-Sigma-Machine': 'seat',
                                  'X-Sigma-Profile': 'profile'}) as client:
        assert client.get(f'{prefix}/intake/clients').json() == [{'name': SUBJECT_NAME, 'taxYears': [TAX_YEAR],
                                                                 'clientId': 'client-a'}]
        assert client.post(endpoint, json=manifest).status_code == 422
        assert client.post(endpoint, json={**manifest, 'assignment': {**assignment, 'clientId': 'client-b'}}).status_code == 404
        assert client.post(endpoint, json={**manifest, 'assignment': {**assignment, 'taxYear': TAX_YEAR - 1}}).status_code == 404
        assert client.post(endpoint, json={**manifest, 'assignment': assignment, 'returnHint': 'other'}).status_code == 404
        declared = client.post(endpoint, json={**manifest, 'assignment': assignment})
        assert declared.status_code == 201
        assert declared.json()['files'][0]['assignment'] == assignment
        uploaded = client.put(f'{endpoint}/synthetic-upload/files/txt', content=b'Synthetic practice text.\n',
                              headers={'Content-Type': 'text/plain'})
        assert uploaded.status_code == 200
        assert uploaded.json()['state'] == 'sealed'
        slot = uploaded.json()['files'][0]
        event = UploadCommitted('firm', slot['uploadId'], slot['versionId'])
        assert intake.committed_events(firm_id='firm') == (event,)
        source = CommittedBatchSource(intake, lambda _event, _hint: ClassifiedUpload())
        verified = source.verify(event)
        assert verified is not None and verified.return_id is None and verified.data_class is None
        assert source.grant_id(event) is None
        assert UploadDispatch(db, source).consume(event) == verified.receipt_id
        assert client.get(f'{prefix}/returns/allowed/intake/batches').json() == []
        assert client.get(f'{prefix}/returns/allowed/jobs').json() == []
        assert client.get(f'{prefix}/returns/other/access').status_code == 404


@pytest.mark.skipif(not os.environ.get('SIGMA_TEST_DISPLAY'), reason='Requires a headed demo display')
def test_interactive_upload_queues_visible_draft_and_links_real_evidence(tmp_path, coordinator_db, monkeypatch):
    monkeypatch.setenv('DISPLAY', os.environ['SIGMA_TEST_DISPLAY'])
    live = _live_settings('gemini') if os.environ.get('SIGMA_TEST_LIVE_GEMINI') == '1' else None
    approval = ('Authorize sending these synthetic documents to gemini' if live
                else 'Authorize local processing of this batch of synthetic documents')

    async def upload(page, paths):
        inbox = page.get_by_role('region', name='Document inbox')
        picker = inbox.get_by_role('group', name='Assign this upload')
        await picker.get_by_label('Client', exact=True).select_option(label=SUBJECT_NAME)
        await picker.get_by_label('Tax year', exact=True).select_option(str(TAX_YEAR))
        await inbox.get_by_role('checkbox', name=approval).check()
        await inbox.get_by_label('Select PDF or TXT files').set_input_files(
            [str(path) for path in paths if 'w2-' in path.name],
        )

    async def check(evidence, native):
        await evidence.get_by_text(
            'Needs review: W-2 Box 3 for TEST-EMP-B / B-1 has no verified value.', exact=True,
        ).wait_for()
        documents = []
        for employer in ('TEST-EMP-A', 'TEST-EMP-B'):
            row = native.get_by_role('row').filter(has_text=employer)
            await row.get_by_role('link', name='Edit W-2').click()
            await native.get_by_label('Box 1 wages', exact=True).focus()
            await evidence.get_by_role('region', name='Field provenance').get_by_role('heading').filter(
                has_text=employer,
            ).wait_for()
            documents.append(parse_qs(urlsplit(evidence.url).query)['document'][0])
            box_3 = native.get_by_label('Box 3 Social Security wages', exact=True)
            if employer == 'TEST-EMP-A':
                assert Decimal(await box_3.input_value()) == Decimal('30000')
                await box_3.focus()
                await evidence.get_by_text('Box 3 Social Security wages: $30,000.00', exact=False).first.wait_for()
            else:
                assert await box_3.input_value() == ''
            await native.get_by_role('link', name='Back to W-2s').click()
        assert documents[0] != documents[1]

    result = asyncio.run(run_interactive(coordinator_db, tmp_path, upload=upload, check=check, live=live))
    assert result.saved.box_3 == {('TEST-EMP-A', 'A-1', Decimal('30000'))}
    assert dict(result.decisions)['rideshare'] == dict(result.decisions)['expenses'] == 'skipped'
