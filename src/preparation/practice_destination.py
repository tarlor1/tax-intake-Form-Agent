"""Outbound contract for the local synthetic practice workflow."""

from collections.abc import Mapping, Set
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .contracts import Amount, Form
from .native_plan import DraftIdentity
from .ownership import NativeAuthority
from .tax_store import SourceField, TaxRecord
from .worker import WorkerNative, WorkerPlan


@dataclass(frozen=True, slots=True)
class PracticeForm:
    section_key: str
    primary_field: str
    fields_by_box: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PracticeField:
    source: SourceField
    fact_id: str
    amount: Amount


class PracticeDestination(Protocol):
    """Provision one enrolled seat and bind grounded facts to its allowed controls."""

    @property
    def profile_id(self) -> str: ...
    @property
    def store_id(self) -> str: ...
    @property
    def product(self) -> Literal["web", "desktop"]: ...
    @property
    def lock_path(self) -> Path: ...
    @property
    def authority(self) -> NativeAuthority: ...
    @property
    def native(self) -> WorkerNative: ...

    def form(self, form: Form) -> PracticeForm | None: ...
    def accepts_amount(self, value: str) -> bool: ...

    def plan(self, identity: DraftIdentity, record: TaxRecord, fields: Mapping[str, PracticeField],
             review_issues: tuple[str, ...]) -> WorkerPlan: ...

    def draft_url(self, draft_key: str, section_key: str, row_keys: Set[str | None]) -> str | None:
        """Resolve the exact location of a released draft from verified native keys."""
        ...
