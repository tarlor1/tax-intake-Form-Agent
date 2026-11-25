"""Synthetic dependency inputs for the real loopback API and evidence frontend."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncio
import os
import socket

import uvicorn
from playwright.async_api import Browser

from src.sigma_demo.pdf import source
from practice_inbox import PracticeInbox
from src.preparation.batches import IngestScope
from src.preparation.contracts import DocumentEvidence, LineEvidence, OriginalRef, PageEvidence, RegionEvidence
from src.preparation.review_events import NativeAddress, ObserverNotice, ReviewAccess, ReviewSelectionSession, ReviewUnavailable
from src.preparation.review_routes import (
    EvidenceCandidate, EvidenceCitation, EvidenceDetail, EvidenceDocument, EvidenceField,
    ReviewDecisionRecorded, ReviewOpened, RevisionLink,
)
from src.preparation.sigma_config import InspectionWorkspace
from src.preparation.web_observer import ReviewSection, WebSelectionObserver
from src.review_api import _companion_app
from src.review_store import ReviewError
from test_preparation_web_navigation import IDENTITY


ROOT = Path(__file__).resolve().parents[1]
RETURN = 'return-synthetic'
CLIENT = 'client-synthetic'
REVISION = 'revision-two'
PRIOR = 'revision-one'
TOKEN = 'test-loopback-token'


def _pdf(document_id, title, *rows, version_id='original-one', extra_pages=()):
    document, content, anchors = source(document_id, title, list(rows), extra_pages=extra_pages)
    original = OriginalRef(document_id=document_id, version_id=version_id)
    evidence = tuple(EvidenceCitation(
        id=citation['id'], label=citation['label'], evidence=RegionEvidence(
            precision='region', original=original, page=citation['page'], rect=citation['rect'], quote=citation['quote'],
        ),
    ) for citation in anchors)
    return EvidenceDocument(id=document_id, title=title, version_id=version_id, media_type='application/pdf',
                            page_count=document['pageCount']), content, evidence

class PracticeCompanion(InspectionWorkspace):
    def __init__(self, directory: Path, *, inbox: PracticeInbox | None = None):
        super().__init__(directory)
        self.inbox = inbox
        self.scope = IngestScope('practice-firm', 'practice-preparer', frozenset({RETURN}))
        self.active: ReviewSelectionSession | None = None
        self.browser: Browser | None = None
        self.native = None
        self.native_profile = None
        self.draft = None
        self.legal_addresses: dict[NativeAddress, str] = {}
        self.pipe_writer = None
        self.pipe_transport = None
        self.relay_task = None
        self.observer = None
        self.native_requests = []
        self.detail_requests = []
        self.stream_requests = 0
        self.field_version = 7
        self.decisions = 0
        a, a_bytes, (a_cite,) = _pdf('doc-a', 'Employer A original', 'Box 1 wages: $100.00')
        b, b_bytes, (b_cite, b_support) = _pdf(
            'doc-b', 'Employer B original', 'Box 1 wages: $100.00', extra_pages=(['Corroborating wages: $100.00'], []),
        )
        text = EvidenceDocument(id='doc-text', version_id='original-one', title='Employer B withholding note',
                                media_type='text/plain', page_count=None)
        text_cite = EvidenceCitation(id='text-line', label='Withholding line', evidence=LineEvidence(
            precision='lines', original=OriginalRef(document_id=text.id, version_id=text.version_id),
            line_start=2, line_end=2, quote='Withholding: $0.00',
        ))
        b_page = EvidenceCitation(id='b-page', label='Verified page only', evidence=PageEvidence(
            precision='page', original=b_cite.evidence.original, page=2,
        ))
        b_original = EvidenceCitation(id='b-original', label='No verified page', evidence=DocumentEvidence(
            precision='document', original=b_cite.evidence.original,
        ))
        self._documents = (a, b, text)
        self._originals = {(doc.id, doc.version_id): (doc.media_type, content) for doc, content in (
            (a, a_bytes), (b, b_bytes), (text, b'Employer B\nWithholding: $0.00\n'),
        )}
        self._fields = (
            EvidenceField(id='wages-a', label='Box 1 wages', record_label='Employer A', value='100.00',
                          provenance='source', used_citation_id=a_cite.id,
                          candidates=(EvidenceCandidate(id='candidate-a', label='Form W-2', value='100.00', citations=(a_cite,)),)),
            EvidenceField(id='wages-b', label='Box 1 wages', record_label='Employer B', value='100.00',
                          provenance='source', used_citation_id=b_cite.id,
                          candidates=(EvidenceCandidate(id='candidate-b', label='Form W-2', value='100.00',
                                                        citations=(b_cite, b_support, b_page, b_original)),)),
            EvidenceField(id='tax-b', label='Withholding', record_label='Employer B', value='0.00',
                          provenance='source', used_citation_id=text_cite.id,
                          candidates=(EvidenceCandidate(id='tax-candidate', label='Note', value='0.00', citations=(text_cite,)),)),
        )

    async def intake_settings(self):
        return await (self.inbox or super()).intake_settings()

    async def batches(self, *, return_id=None):
        return await (self.inbox or super()).batches(return_id=return_id)

    async def declare(self, manifest, *, return_id=None):
        return await (self.inbox or super()).declare(manifest, return_id=return_id)

    async def upload(self, batch_id, slot, media_type, content, *, return_id=None):
        return await (self.inbox or super()).upload(batch_id, slot, media_type, content, return_id=return_id)

    async def exclude(self, batch_id, slot):
        return await (self.inbox or super()).exclude(batch_id, slot)

    async def returns(self):
        return [{
            'id': RETURN, 'clientId': CLIENT, 'clientName': 'Synthetic Avery Morgan', 'taxYear': 2025,
            'returnType': 'individual', 'destination': 'Sigma Web', 'totalFields': 3, 'acceptedFields': 3,
            'missingFields': 0, 'conflictFields': 0, 'updatedAt': '2025-12-25T00:00:00Z', 'reviewRequired': True,
        }]

    async def jobs(self, return_id):
        await self.access(return_id)
        return [{'id': 'job', 'returnId': RETURN, 'inputRevision': REVISION, 'status': 'ready_for_review', 'issues': []}]

    async def access(self, return_id):
        if return_id != RETURN:
            raise ReviewError(404, 'Return is outside the synthetic assignment.')
        return ReviewAccess(self.scope.firm_id, self.scope.actor_id, return_id)

    async def evidence(self, access, revision):
        if revision is None:
            revision = REVISION
        if revision not in {PRIOR, REVISION}:
            raise ReviewError(404, 'Input revision unavailable.')
        self.detail_requests.append(revision)
        return EvidenceDetail(
            id=access.return_id, client_id=CLIENT, client_name='Synthetic Avery Morgan', tax_year=2025,
            return_type='individual', input_revision=revision, latest_revision=REVISION,
            revisions=(RevisionLink(id=REVISION, label='Revision 2'), RevisionLink(id=PRIOR, label='Revision 1')),
            documents=self._documents if revision == REVISION else self._documents[:1],
            fields=self._fields if revision == REVISION else self._fields[:1], review_available=self.browser is not None,
            decision_version=self.decisions, field_version=self.field_version,
        )

    async def original(self, access, revision, document_id, version_id):
        key = document_id, version_id
        if revision not in {PRIOR, REVISION} or key not in self._originals or (revision == PRIOR and document_id != 'doc-a'):
            raise ReviewError(404, 'Document version is outside this revision.')
        return self._originals[key]

    async def open_review(self, access, revision):
        if self.active is not None:
            if self.active.access != access:
                raise ReviewError(404, 'No practice reservation in this scope.')
            if not self.active.active:
                return ReviewOpened(status='recovery_required', review_session_id=self.active.id, return_id=RETURN,
                                    input_revision=self.active.input_revision)
        if revision != REVISION or not self.browser or not self.native_profile or not self.draft:
            raise ReviewError(409, 'The practice native draft is unavailable for that revision.')
        if self.active is not None and self.active.active:
            return ReviewOpened(status='open', review_session_id=self.active.id, return_id=RETURN, input_revision=revision)
        self.active = ReviewSelectionSession(
            access, input_revision=revision, draft_key=self.draft,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5), bindings=self.legal_addresses,
        )
        self.native = await self.browser.new_page()
        self.native.on('request', lambda request: self.native_requests.append((request.method, request.url)))
        await self.native.goto(f'{self.native_profile.origin}/drafts/{self.draft}/w2')
        read_fd, write_fd = os.pipe()
        self.pipe_writer = os.fdopen(write_fd, 'wb', buffering=0)
        reader = asyncio.StreamReader(limit=2048)
        self.pipe_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, 'rb', buffering=0),
        )
        self.relay_task = asyncio.create_task(self.active.relay(reader))
        sequence = 0

        def forward(selection):
            nonlocal sequence
            sequence += 1
            draft, section, row, field = selection or (None, None, None, None)
            notice = ObserverNotice(sequence=sequence, draft_key=draft, section=section, record=row, field=field)
            if not self.pipe_writer.closed:
                self.pipe_writer.write(notice.model_dump_json(by_alias=True).encode() + b'\n')

        section = ReviewSection('w2', (('input[name="wages"]', 'wages'), ('input[name="withholding"]', 'withholding')),
                                active_record='form[data-record-id]')
        self.observer = WebSelectionObserver(self.native, self.native_profile, IDENTITY, self.draft, (section,), r'[a-f0-9]{32}', forward)
        await self.observer.__aenter__()
        return ReviewOpened(status='open', review_session_id=self.active.id, return_id=RETURN, input_revision=revision)

    def session(self, access, session_id):
        if self.active is None or self.active.id != session_id:
            raise ReviewUnavailable('No practice reservation.')
        self.active.require_access(access, session_id)
        return self.active

    async def decision(self, access, command):
        if (command.expected_decision_version != self.decisions
                or command.expected_field_version != self.field_version or command.kind != 'mark_reviewed'):
            raise ReviewError(409, 'Stale or unsupported practice decision.')
        self.decisions += 1
        return ReviewDecisionRecorded(decision_version=self.decisions)

    async def close_review(self, access, session_id):
        if self.active is None or self.active.id != session_id or self.active.access != access:
            raise ReviewError(404, 'No practice reservation in this scope.')
        if self.native is None or not self.native.is_closed():
            raise ReviewError(409, 'Close the native return before releasing the reservation.')
        await self.dispose()
        self.active = None

    async def dispose(self):
        if self.native is not None and not self.native.is_closed():
            await self.native.close()
        if self.observer is not None:
            await self.observer.close()
        if self.pipe_writer is not None:
            self.pipe_writer.close()
        if self.relay_task is not None:
            await self.relay_task
        if self.pipe_transport is not None:
            self.pipe_transport.close()
        if self.active is not None:
            self.active.close()


@asynccontextmanager
async def run_companion(backend: PracticeCompanion | PracticeInbox):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        app = _companion_app(ROOT / 'review_ui' / 'dist', port=port, token=TOKEN, backend=backend)

        @app.middleware('http')
        async def track_stream(request, call_next):
            if isinstance(backend, PracticeCompanion) and request.url.path.endswith('/review/events'):
                backend.stream_requests += 1
            return await call_next(request)

        ready = asyncio.Event()

        class Server(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                ready.set()

        server = Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', proxy_headers=False))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
            assert server.started
            yield f'http://127.0.0.1:{port}'
        finally:
            if isinstance(backend, PracticeCompanion):
                await backend.dispose()
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)
