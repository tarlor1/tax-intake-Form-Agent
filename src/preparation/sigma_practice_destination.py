"""Loopback-only destination adapter for the synthetic Sigma practice form."""

from collections.abc import Iterator, Mapping, Set
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
from shutil import which
from typing import Literal

from ..sigma_demo.native import PracticeAuthority, layout, practice_web_profile
from ..sigma_demo.target import sigma_practice_target
from .contracts import Form
from .native_plan import DraftIdentity, DraftPlan, FieldPlan, RecordPlan, SectionPlan
from .ownership import OwnerGrant, OwnershipBlocked
from .practice_destination import PracticeDestination, PracticeField, PracticeForm
from .sigma_web import SearchResult
from .tax_store import TaxRecord
from .web_worker import WebWorker
from .worker import FieldSource, WorkerPlan, WorkItem, binding_reference

_FORMS: Mapping[Form, PracticeForm] = {
    "w2": PracticeForm("w2", "wages", {"1": "wages", "2": "withholding", "3": "social_security_wages"}),
    "1099k": PracticeForm("1099-k", "gross", {"1a": "gross", "4": "withholding"}),
    "1099nec": PracticeForm("1099-nec", "compensation", {"1": "compensation", "4": "withholding", "1a": "compensation"}),
}
_SECTIONS = frozenset(form.section_key for form in _FORMS.values())
_DRAFT_KEY = re.compile(r"[a-f0-9]{32}")
_MONEY = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,2})?")


@dataclass(frozen=True, slots=True)
class _SigmaPracticeDestination:
    profile_id: str
    store_id: str
    product: Literal["web"]
    lock_path: Path
    authority: PracticeAuthority
    native: WebWorker

    def form(self, form: Form) -> PracticeForm | None:
        return _FORMS.get(form)

    def accepts_amount(self, value: str) -> bool:
        return _MONEY.fullmatch(value) is not None

    def plan(self, identity: DraftIdentity, record: TaxRecord, fields: Mapping[str, PracticeField],
             review_issues: tuple[str, ...]) -> WorkerPlan:
        section = _FORMS[record.identity.form].section_key
        row_id = record.record_id if section == "w2" else None
        field_plans, sources = [], {}
        for field, fact in fields.items():
            binding = binding_reference(section, row_id, field)
            field_plans.append(FieldPlan(field_key=field, value=format(fact.amount.value, "f"), binding_id=binding,
                                         fact_ids=(fact.fact_id,), kind="decimal"))
            sources[binding] = FieldSource((fact.source,), fact.amount)
        if row_id is not None:
            profile_section = SectionPlan(section_key=section, records=(RecordPlan(
                source_record_id=row_id,
                identity={"employer": record.identity.issuer_reference, "instance": record.identity.instance_reference},
                fields=tuple(field_plans), allow_create=True,
            ),))
        else:
            profile_section = SectionPlan(section_key=section, fields=tuple(field_plans))
        return WorkerPlan(DraftPlan(identity=identity, sections=(profile_section,)), sources,
                          transit_contexts=frozenset({"w2"}), review_issues=review_issues)

    def draft_url(self, draft_key: str, section_key: str, row_keys: Set[str | None]) -> str | None:
        if section_key not in _SECTIONS or not _DRAFT_KEY.fullmatch(draft_key):
            return None
        section = next((section for section in self.native.layout.sections if section.key == section_key), None)
        if section is None:
            return None
        path = self.native.profile.origin + section.path.format(draft_key=draft_key)
        if section.records is not None and len(row_keys) == 1:
            row_key, = row_keys
            if row_key is not None and _DRAFT_KEY.fullmatch(row_key):
                return f"{path}/{row_key}"
        elif section.records is None and row_keys == {None}:
            return path
        return None


class _PracticeAttestations:
    """Adapt proof from the guarded browser while the local seat mutex is held."""

    def __init__(self, authority: PracticeAuthority):
        self._authority = authority
        self._grant: OwnerGrant | None = None
        self._identity: DraftIdentity | None = None

    async def checkout(self, grant: OwnerGrant, item: WorkItem, draft: str | None) -> str:
        if draft is not None and not _DRAFT_KEY.fullmatch(draft):
            raise OwnershipBlocked("practice-checkout-mismatch")
        if self._grant != grant:
            self._authority.checkouts.clear()
            self._authority.closes.clear()
            self._authority.absent.clear()
            self._authority.existing.clear()
            self._authority.created.clear()
            self._grant, self._identity = grant, item.identity
        self._authority.checkouts.add((grant, draft, "practice-seat-checkout"))
        return "practice-seat-checkout"

    async def absent(self, grant: OwnerGrant, result: SearchResult) -> str:
        if grant != self._grant or result.identity != self._identity or result.status != "absent" or result.draft_key is not None:
            raise OwnershipBlocked("practice-absence-unverified")
        self._authority.absent.add((grant, result.proof))
        return result.proof

    async def existing(self, grant: OwnerGrant, result: SearchResult) -> str:
        if (grant != self._grant or result.identity != self._identity or result.status != "found"
                or result.draft_key is None or not _DRAFT_KEY.fullmatch(result.draft_key)):
            raise OwnershipBlocked("practice-draft-unverified")
        self._authority.existing[(grant, result.proof)] = result.draft_key
        return result.proof

    async def created(self, grant: OwnerGrant, step_id: str, draft: str) -> str:
        if grant != self._grant or not _DRAFT_KEY.fullmatch(draft):
            raise OwnershipBlocked("practice-draft-unverified")
        self._authority.created[(grant, step_id, "practice-created")] = draft
        return "practice-created"

    async def closed(self, grant: OwnerGrant, draft: str | None) -> str:
        # WebWorker calls this after its browser and the tool host have exited.
        if grant != self._grant:
            raise OwnershipBlocked("practice-close-unverified")
        self._authority.checkouts.clear()
        self._authority.closes.add((grant, draft, "practice-seat-closed"))
        return "practice-seat-closed"


@contextmanager
def sigma_practice_destination(data_dir: Path, port: int) -> Iterator[PracticeDestination]:
    directory = data_dir.expanduser().absolute() / "practice-drafts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or (os.name == "posix" and (directory.stat().st_uid != os.getuid()
                                                           or directory.stat().st_mode & 0o077)):
        raise ValueError("The practice draft directory must be private to this OS user.")
    chromium = next((path for name in ("chromium", "chromium-browser", "google-chrome") if (path := which(name))), None)
    with sigma_practice_target(directory / "drafts.sqlite3", include_box3=True, include_information_returns=True,
                               companion_origin=f"http://127.0.0.1:{port}") as origin:
        profile = practice_web_profile(origin, include_information_returns=True)
        private = directory / "private-worker"
        private.mkdir(mode=0o700, exist_ok=True)
        authority = PracticeAuthority()
        native = WebWorker(profile, layout(profile.profile_id, include_box3=True, include_information_returns=True),
                           _PracticeAttestations(authority), private, chromium=chromium, headed_practice=True)
        yield _SigmaPracticeDestination(profile.profile_id, "local-practice-drafts", "web", directory / "seat.lock",
                                        authority, native)
