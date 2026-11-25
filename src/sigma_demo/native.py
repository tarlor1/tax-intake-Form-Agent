"""Native web profile and authority for the isolated synthetic Sigma demo."""

from src.preparation.sigma_web import WebContext, WebDraft, WebProfile, WebSection as DraftSection
from src.preparation.web_actions import WebField, WebLayout, WebLink, WebRecords, WebSection

from .target import _INFORMATION_RETURNS, _INFORMATION_WITHHOLDING, _draft_pattern


_PRACTICE_PROFILE_ID = "synthetic-draft-v1"
_BOX3_KEY = "social_security_wages"
_SECTIONS = (
    DraftSection("w2", "w2", "Save W-2", rows=True),
    DraftSection("rideshare", "rideshare", "Save rideshare"),
    DraftSection("rideshare/expenses", "expenses", "Save expense", rows=True, dialog=True),
)
_INFORMATION_SECTIONS = tuple(
    DraftSection(key, key, f"Save {label}") for key, (label, _, _) in _INFORMATION_RETURNS.items()
)
_CONTEXT = WebContext(
    list_heading="Return list",
    search_form="Exact draft search",
    search_button="Search",
    search_labels=("Client reference", "Tax year", "Return type"),
    field_names=("client_reference", "tax_year", "return_type"),
    result_region="Exact search result",
    open_link="Open draft",
    absent_status="No matching draft.",
    completion_attribute="data-search-coverage",
    completion_value="exact-exhaustive",
    completion_text="Exact search complete.",
    create_button="Create draft",
    draft_root="main[data-section]",
    identity_attributes=("data-draft-id", "data-client-reference", "data-tax-year", "data-return-type", "data-status"),
    unfiled_value="unfiled",
    section_attribute="data-section",
    row_attribute="data-record-id",
)


def practice_web_settings(origin: str, *, include_information_returns: bool = False):
    return dict(
        profile_id=_PRACTICE_PROFILE_ID, origin=origin, list_path="/", create_path="/drafts",
        draft_pattern=_draft_pattern(include_information_returns),
        context=_CONTEXT, sections=_SECTIONS + (_INFORMATION_SECTIONS if include_information_returns else ()),
        landing_section="w2", return_types=("individual",),
    )


def practice_web_profile(origin: str, *, include_information_returns: bool = False) -> WebProfile:
    return WebProfile.for_test(**practice_web_settings(
        origin.removesuffix("/"), include_information_returns=include_information_returns,
    ))


def layout(profile_id: str, *, include_box3: bool = False, include_information_returns: bool = False) -> WebLayout:
    box3 = (WebField(_BOX3_KEY, "Box 3 Social Security wages", _BOX3_KEY, "decimal"),) if include_box3 else ()
    info_forms = _INFORMATION_RETURNS if include_information_returns else {}
    info_links = tuple(WebLink(label, key, "open_section") for key, (label, _, _) in info_forms.items())
    withholding, withholding_label = _INFORMATION_WITHHOLDING
    info_sections = tuple(
        WebSection(key, f"/drafts/{{draft_key}}/{key}", (
            WebField(amount, amount_label, amount, "decimal"),
            WebField(withholding, withholding_label, withholding, "decimal"),
        ), f"Save {label}", links=(WebLink("W-2s", "w2", "open_section"),))
        for key, (label, amount, amount_label) in info_forms.items()
    )
    return WebLayout(profile_id, (
        WebSection("w2", "/drafts/{draft_key}/w2", (
            WebField("employer", "Employer reference", "employer_reference", required=True),
            WebField("instance", "Form instance", "form_instance", required=True),
            WebField("wages", "Box 1 wages", "wages", "decimal"),
            WebField("withholding", "Box 2 federal withholding", "withholding", "decimal"),
            *box3,
        ), "Save W-2", links=(WebLink("Rideshare", "rideshare", "open_section"), *info_links),
            records=WebRecords("W-2 records", "data-record-id", ("employer", "instance"),
                               "Add W-2", "Edit W-2", "Back to W-2s", "Actions", 12,
                               navigation_labels=("Return list", "W-2s", "Rideshare",
                                                  *(link.label for link in info_links)))),
        WebSection("rideshare", "/drafts/{draft_key}/rideshare", (
            WebField("gross", "Gross receipts", "gross_receipts", "decimal"),
            WebField("fees", "Platform fees", "platform_fees", "decimal"),
        ), "Save rideshare", links=(WebLink("W-2s", "w2", "open_section"),
                                    WebLink("Expenses", "expenses", "open_detail"))),
        WebSection("expenses", "/drafts/{draft_key}/rideshare/expenses", (
            WebField("receipt", "Receipt reference", "receipt_reference", required=True),
            WebField("description", "Description", "description", required=True),
            WebField("expense", "Amount", "amount", "decimal", required=True),
        ), "Save expense", links=(WebLink("Back to rideshare", "rideshare", "open_detail"),), dialog="Expenses",
            records=WebRecords("Expense records", "data-record-id", ("receipt",), "Add expense", "Edit expense",
                               "Back to expenses", "Actions", 12, navigation_labels=("Back to rideshare",),
                               new_dialog="New expense", edit_dialog="Edit expense")),
        *info_sections,
    ))


async def rows(session: WebDraft, label: str) -> dict[tuple[str, ...], str]:
    """Read displayed row values and native IDs, omitting a blank optional Box 3."""
    table = session.page.get_by_role("table", name=label, exact=True)
    result: dict[tuple[str, ...], str] = {}
    ids: set[str] = set()
    for row in await table.locator("tbody tr").all():
        values = tuple((await row.get_by_role("cell").all_text_contents())[:-1])
        if label == "W-2 records" and len(values) == 5 and not values[-1]:
            values = values[:-1]
        native_id = await row.get_attribute("data-record-id")
        if not native_id or native_id in ids or values in result:
            raise ValueError(f"Missing or ambiguous native row identity in {label}")
        result[values] = native_id
        ids.add(native_id)
    return result


class PracticeAuthority:
    """Synthetic, generation-scoped authority; application inputs cannot register evidence."""

    def __init__(self):
        self.checkouts = set()
        self.closes = set()
        self.absent = set()
        self.existing = {}
        self.created = {}
        self.reopens = {}
        self.restorations = set()
        self.inspections = []

    def holds_checkout(self, grant, draft_key, proof_id):
        return (grant, draft_key, proof_id) in self.checkouts

    def closed(self, grant, draft_key, proof_id):
        return (grant, draft_key, proof_id) in self.closes

    def existing_draft(self, grant, proof_id):
        return self.existing.get((grant, proof_id))

    def creation_allowed(self, grant, proof_id):
        return (grant, proof_id) in self.absent

    def created_draft(self, grant, step_id, proof_id):
        return self.created.get((grant, step_id, proof_id))

    def reopened(self, grant, draft_key, intents, proof_id):
        self.inspections.append((grant, draft_key, intents))
        return self.reopens.get((grant, draft_key, proof_id))

    def restored_session(self, interrupted_grant, proof_id):
        return (interrupted_grant, proof_id) in self.restorations
