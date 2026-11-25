"""Committed originals through the shipped coordinator, worker and MCP child."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import httpx

from test_preparation_web_actions import MODEL, PERSON, ScriptedProposals, field_plan, w2
from test_preparation_web_navigation import chromium_executable
from src.sigma_demo.native import PracticeAuthority, layout, practice_web_profile, rows
from src.sigma_demo.target import sigma_practice_target
from src.preparation.agent import Authorization, StagePolicy
from src.preparation.batches import BatchFile, BatchManifest, ClassifiedUpload, IngestScope
from src.preparation.contracts import Amount, Candidate, ExactClaim, OriginalRef, RecordIdentity, SourceOccurrence, TaxExtraction
from src.preparation.coordinator_db import migrate
from src.preparation.evidence import OriginalScope, VerifiedFact
from src.preparation.firm_api import Principal
from src.preparation.native_plan import DraftPlan, SectionPlan
from src.preparation.originals import FilesystemOriginals
from src.preparation.providers import Usage
from src.preparation.sigma_web import WebDraft
from src.preparation.site import ExtractionEnrollment, FirmRuntime, FirmSite
from src.preparation.tax_store import SourceField, TaxAccess
from src.preparation.web_worker import WebWorker
from src.preparation.worker import FieldSource, WorkerEnrollment, WorkerPlan, binding_reference


FIRM, RETURN, STORE, SEAT, WORKER = 'practice-firm', 'return-one', 'store-one', 'seat-one', 'worker-one'
CLASSES = frozenset({'synthetic'})
WAGES = '30000.00'
QUOTE = 'Box 1 wages: $30,000.00'


def _policy(stage):
    return StagePolicy(MODEL, stage, CLASSES, 12, 65_536, 60, 1024, 256, Decimal(1), Decimal(1), Decimal(1))


def _fact(scope, employer):
    identity = RecordIdentity(tax_year=PERSON.tax_year, form='w2', subject_reference=PERSON.client_reference,
                              issuer_reference=employer, activity_reference=None, period=None, instance_reference='ORIGINAL')
    anchor = ExactClaim(precision='exact', original=scope.original, page=None, line_start=2, line_end=2, quote=QUOTE)
    return VerifiedFact(scope=scope, identity=identity, fact_key='box-1', column='reported',
                        raw_value='$30,000.00', normalized=Amount(kind='amount', value=WAGES), anchors=(anchor,))


def test_two_equal_w2s_retain_distinct_citations_after_worker_exit(tmp_path, coordinator_db):
    async def exercise(origin):
        db = coordinator_db
        assert migrate(db) == migrate(db)
        profile = practice_web_profile(origin)
        binary = chromium_executable()
        authority = PracticeAuthority()
        assignment = {}
        proposals = []
        original_ids = {}
        same_return_uploads = asyncio.Barrier(2)
        storage = tmp_path / 'originals'
        storage.mkdir(mode=0o700)

        class Attestations:
            async def checkout(self, grant, item, draft):
                authority.checkouts.add((grant, draft, 'checkout'))
                return 'checkout'

            async def absent(self, grant, result):
                assert result.status == 'absent' and result.identity == PERSON
                authority.absent.add((grant, result.proof))
                return result.proof

            async def created(self, grant, step_id, draft):
                authority.created[(grant, step_id, 'created')] = draft
                authority.checkouts.add((grant, draft, 'checkout'))
                return 'created'

            async def closed(self, grant, draft):
                authority.checkouts = {entry for entry in authority.checkouts if entry[0] != grant}
                authority.closes.add((grant, draft, 'closed'))
                return 'closed'

        class Extraction:
            identity = MODEL

            async def extract(self, instructions, source_text, schema, *, max_output_tokens, max_request_bytes,
                              before_request):
                await before_request()
                source = json.loads(source_text)
                original = OriginalRef.model_validate(source['original'])
                employer = source['pages'][0]['lines'][0]['text']
                original_ids[employer] = original
                await asyncio.wait_for(same_return_uploads.wait(), timeout=5)
                fact = _fact(OriginalScope(firm_id=FIRM, return_id=RETURN, original=original), employer)
                candidate = Candidate(fact_key=fact.fact_key, column=fact.column, raw_value=fact.raw_value,
                                      normalized=fact.normalized, citations=fact.anchors)
                extraction = TaxExtraction(schema_version=1, issues=(), records=(
                    SourceOccurrence(identity=fact.identity, original=original, candidates=(candidate,)),
                ))
                return extraction.model_dump(mode='json'), Usage(1, 1)

        @asynccontextmanager
        async def extraction():
            yield Extraction()

        @asynccontextmanager
        async def reasoning():
            class RequiredToolScript(ScriptedProposals):
                async def propose(self, *args, **kwargs):
                    assert self.script, 'Controller requested another tool after work completed'
                    return await super().propose(*args, **kwargs)

            yield RequiredToolScript(proposals, (PERSON.client_reference, 'EMPLOYER-A', 'EMPLOYER-B', WAGES))

        def plan(item, view):
            records = []
            mapping = {}
            for record in sorted(view.records.values(), key=lambda row: row.identity.issuer_reference):
                field = SourceField(record_id=record.record_id, fact_key='box-1')
                (fact,) = view.effective_facts[field]
                binding = binding_reference('w2', record.record_id, 'wages')
                records.append(w2(record.record_id, record.identity.issuer_reference,
                                 (field_plan('wages', WAGES, binding, fact.fact_id),), allow_create=True))
                mapping[binding] = FieldSource((field,), fact.normalized)
                proposals.extend((('w2', 'ensure_record', {'sourceRecordId': record.record_id}),
                                  ('w2', 'set_field', {'bindingId': binding}), ('w2', 'save_draft', {})))
            return WorkerPlan(
                DraftPlan(identity=PERSON, sections=(SectionPlan(section_key='w2', records=tuple(records)),)),
                mapping, transit_contexts=frozenset({'rideshare', 'expenses'}),
            )

        def authorize(item):
            return assignment.setdefault(item.input_revision, Authorization(
                item.input_revision, 'reasoning', MODEL, CLASSES, datetime.now(timezone.utc) + timedelta(minutes=5),
            ))

        token = 'synthetic-review-token'

        def authenticate(candidate, machine, profile_id):
            if (candidate, machine, profile_id) == (token, SEAT, profile.profile_id):
                return Principal(FIRM, 'preparer', frozenset({RETURN}), machine, profile_id)
            return None

        site = FirmSite(
            FIRM, authenticate, authority, FilesystemOriginals(storage),
            lambda event, hint: ClassifiedUpload(RETURN, 'synthetic'),
            lambda doc: (_fact(doc.scope, doc.original.content.decode().splitlines()[0]),),
            extraction=ExtractionEnrollment(_policy('extraction'), 'synthetic', extraction),
            reasoning_policy=_policy('reasoning'),
            worker=WorkerEnrollment(SEAT, WORKER, _policy('reasoning'),
                WebWorker(profile, layout(profile.profile_id), Attestations(), storage, chromium=binary),
                tmp_path / 'seat.lock', plan, authorize, reasoning),
        )
        runtime = FirmRuntime(db, site)
        runtime.ownership.register_machine(FIRM, SEAT, WORKER)
        runtime.ownership.qualify_machine(FIRM, SEAT, profile.profile_id, PERSON.tax_year, STORE,
                                          shared_store=True, checkout_mode='firm')
        runtime.ownership.set_available(FIRM, SEAT, True)
        with db.transaction() as connection:
            connection.execute('INSERT INTO prep_returns (firm_id,return_id,profile_id,store_id,tax_year) VALUES (%s,%s,%s,%s,%s)',
                               (FIRM, RETURN, profile.profile_id, STORE, PERSON.tax_year))
            connection.execute('''INSERT INTO prep_return_directory
                (firm_id,return_id,client_id,client_name,client_reference,return_type,product)
                VALUES (%s,%s,'synthetic-client','Synthetic Client',%s,'individual','web')''',
                (FIRM, RETURN, PERSON.client_reference))
        scope = IngestScope(FIRM, 'practice-intake', frozenset({RETURN}))
        runtime.intake.declare(scope, BatchManifest(batch_id='mixed-drop', approved_for_ai=True, files=tuple(
            BatchFile(slot=slot, filename=f'{slot}.txt', media_type='text/plain') for slot in ('a', 'b', 'unrelated')
        )), policy=site.extraction.recipient)
        for slot, employer in (('a', 'EMPLOYER-A'), ('b', 'EMPLOYER-B')):
            assert runtime.intake.put(scope, 'mixed-drop', slot, 'text/plain', f'{employer}\n{QUOTE}\n'.encode()).state == 'incomplete'
        assert await runtime.dispatch_once() == 2
        assert await runtime.dispatch_once() == 0
        job = await runtime.worker().run_once()
        assert job is not None and await runtime.worker().run_once() is None
        with db.transaction() as connection:
            result = connection.execute('SELECT state, input_revision FROM prep_jobs WHERE job_id=%s', (job,)).fetchone()
            journal = connection.execute('SELECT event FROM prep_owner_events ORDER BY event_order').fetchall()
            assert result['state'] == 'ready_for_review', journal
            draft = connection.execute('SELECT draft_key FROM prep_returns WHERE return_id=%s', (RETURN,)).fetchone()['draft_key']
        async with WebDraft.launch(profile, executable_path=binary) as reviewer:
            await reviewer.open_existing(PERSON, draft)
            assert list(await rows(reviewer, 'W-2 records')) == [
                ('EMPLOYER-A', 'ORIGINAL', WAGES, ''), ('EMPLOYER-B', 'ORIGINAL', WAGES, ''),
            ]
        access = TaxAccess(firm_id=FIRM, return_id=RETURN, actor_id='preparer', scopes={'read'})
        view = FirmRuntime(db, site).tax.read_revision(access, 2)
        assert len({field.binding.record_key for field in view.fields.values()}) == 2
        for employer in ('EMPLOYER-A', 'EMPLOYER-B'):
            record_id = next(record.record_id for record in view.records.values() if record.identity.issuer_reference == employer)
            state = view.fields[binding_reference('w2', record_id, 'wages')].state
            (fact_id,) = state.used_candidate_ids
            assert view.facts[fact_id].original == original_ids[employer]
            assert state.reopened.value.value == Decimal(WAGES)

        prefix = f'/api/sigma/v1/returns/{RETURN}'
        headers = {'Authorization': f'Bearer {token}', 'X-Sigma-Machine': SEAT, 'X-Sigma-Profile': profile.profile_id}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.app()),
                                     base_url='https://firm.invalid') as client:
            assert (await client.get(f'{prefix}/evidence')).status_code == 401
            assert (await client.get('/api/sigma/v1/returns/other-return/evidence', headers=headers)).status_code == 404
            response = await client.get(f'{prefix}/evidence', headers=headers)
            assert response.status_code == 200
            evidence = response.json()
            assert evidence['inputRevision'] == result['input_revision']
            fields = evidence['fields']
            for employer, original in original_ids.items():
                field = next(field for field in fields if employer in field['recordLabel'])
                assert field['status'] == 'verified' and Decimal(field['savedValue']) == Decimal(WAGES)
                citations = [citation for candidate in field['candidates'] for citation in candidate['citations']]
                used = next(citation for citation in citations if citation['id'] == field['usedCitationId'])
                assert used['evidence']['original'] == original.model_dump(by_alias=True)
                path = f'{prefix}/originals/{original.document_id}/{original.version_id}'
                download = await client.get(path, params={'revision': evidence['inputRevision']}, headers=headers)
                assert download.status_code == 200 and download.content == f'{employer}\n{QUOTE}\n'.encode()

    with sigma_practice_target(tmp_path / 'vendor.sqlite3') as origin:
        asyncio.run(exercise(origin))
