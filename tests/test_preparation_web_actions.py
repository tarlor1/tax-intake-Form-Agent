"""Exercise the web tool backend against an independent persistent native target."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from test_preparation_web_navigation import chromium_executable
from src.preparation.agent import Authorization, DraftAssignment, StagePolicy, prepare
from src.preparation.native_plan import DraftIdentity, DraftPlan, FieldPlan, RecordPlan, SectionPlan
from src.preparation.providers import ProviderIdentity, ProviderTurn, ToolCall, Usage
from src.preparation.sigma_web import CreatePermit, WebDraft
from src.preparation.tool_contract import Action, JobScope, MutationPermit, Verification, VerifiedReadback
from src.preparation.tools import ToolHostError
from src.preparation.web_actions import WebActions
from src.sigma_demo.native import layout, practice_web_profile, rows
from src.sigma_demo.target import sigma_practice_target


PERSON = DraftIdentity(client_reference="PRACTICE-CLIENT", tax_year=2025, return_type="individual")
MODEL = ProviderIdentity("gemini", "scripted", "https://network-denied.invalid", "synthetic-proof")
CLASSES = frozenset({"synthetic"})


def field_plan(key, value, binding, *facts, expected=None, owned=False, kind="decimal"):
    return FieldPlan(field_key=key, value=value, binding_id=binding, fact_ids=facts or (f"fact-{binding}",),
                     kind=kind, expected_saved=expected, preparer_owned=owned)


def w2(source, employer, fields, *, external=None, allow_create=False, instance="ORIGINAL"):
    return RecordPlan(source_record_id=source, identity={"employer": employer, "instance": instance}, fields=fields,
                      external_key=external, allow_create=allow_create)


def expense(source, receipt):
    return RecordPlan(source_record_id=source, identity={"receipt": receipt}, allow_create=True, fields=(
        field_plan("description", f"Expense for {receipt}", f"description-{source}", kind="text"),
        field_plan("expense", "42.00", f"amount-{source}"),
    ))


async def create(session):
    async def authorize(absent):
        assert absent.status == "absent" and absent.identity == PERSON
        return CreatePermit(session.profile.profile_id, PERSON, absent.proof, "practice-create-intent")

    return await session.create(PERSON, authorize)


def scope_for(session, key):
    return JobScope(job_id="sample-job", owner_generation=1, profile_id=session.profile.profile_id, draft_key=key)


def choose(observation, name, **match):
    choices = [choice for choice in observation.choices if choice.action.name == name
               and all(getattr(choice.action.arguments, key, None) == value for key, value in match.items())]
    assert len(choices) == 1
    return choices[0]


class Journal:
    def __init__(self, scope):
        self.scope = scope
        self.permits = []
        self.readbacks = []
        self.verifying = False
        self.verified = None

    async def require_owner(self, scope):
        assert scope == self.scope

    async def begin(self, scope, choice):
        assert scope == self.scope and choice.mutation is not None
        permit = MutationPermit(scope=scope, step_id=f"step-{len(self.permits)}", action=choice.action,
                                details=choice.mutation)
        self.permits.append(permit)
        return permit

    async def readback(self, permit, outcome, observed):
        assert permit == self.permits[-1] and outcome.status == "applied"
        assert observed.scope == self.scope and observed.observation_id != permit.action.observation_id
        self.readbacks.append(permit)

    async def start_verification(self):
        assert self.permits == self.readbacks
        self.verifying = True

    async def record_verification(self, result):
        assert self.verifying and result.scope == self.scope
        self.verified = result


class ScriptedProposals:
    """Pure local proposals: there is no SDK client, key, or network transport."""

    identity = MODEL

    def __init__(self, script, forbidden):
        self.script = list(script)
        self.forbidden = forbidden
        self.turn = 0

    async def propose(self, observation, tools, previous, *, max_output_tokens, max_request_bytes):
        for local in self.forbidden:
            assert local not in str(observation)
        if not self.script:
            return ProviderTurn((), Usage(1, 1))
        context, name, keys = self.script.pop(0)
        assert observation["context"] == context
        assert name in {tool.name for tool in tools}
        choices = [choice for choice in observation["choices"] if choice["name"] == name and all(
            choice["arguments"].get(key) == value for key, value in keys.items()
        )]
        assert len(choices) == 1
        self.turn += 1
        return ProviderTurn((ToolCall(f"proposal-{self.turn}", name, choices[0]["arguments"]),), Usage(1, 1))


async def run_plan(session, key, plan, script):
    scope = scope_for(session, key)
    fields = [field for section in plan.sections for field in section.fields]
    fields.extend(field for section in plan.sections for row in section.records for field in row.fields)
    assignment = DraftAssignment(
        scope, "revision", CLASSES, frozenset({"w2", "rideshare", "expenses"}),
        frozenset({"w2", "rideshare", "expenses", *(field.field_key for field in fields)}),
        tuple((f.binding_id, f.fact_ids) for f in fields),
        frozenset(row.source_record_id for section in plan.sections for row in section.records), 48, 5,
    )
    policy = StagePolicy(MODEL, "reasoning", CLASSES, 48, 65_536, 120, 1024, 128,
                         Decimal(1), Decimal(1), Decimal(1))
    grant = Authorization(assignment.revision, "reasoning", MODEL, CLASSES,
                          datetime.now(timezone.utc) + timedelta(minutes=3))
    provider = ScriptedProposals(script, (key, PERSON.client_reference, "6200.00", "EMPLOYER-SECRET"))
    journal = Journal(scope)
    result = await prepare(assignment, provider=provider, policy=policy, grant=grant,
                           host=WebActions(session, scope, layout(scope.profile_id), plan), journal=journal)
    assert not provider.script
    assert journal.verified == result
    return result, journal


async def follow(session, key, label, suffix):
    await session.follow(PERSON, key, session.page.get_by_role("link", name=label, exact=True),
                         path=f"/drafts/{key}/{suffix}")


async def manual_w2(session, key, employer, wages, withholding, instance="ORIGINAL"):
    await follow(session, key, "Add W-2", "w2/new")
    form = session.page.locator("form")
    fields = (
        ("Employer reference", "employer_reference", employer), ("Form instance", "form_instance", instance),
        ("Box 1 wages", "wages", wages), ("Box 2 federal withholding", "withholding", withholding),
    )
    for label, _, value in fields:
        await form.get_by_label(label, exact=True).fill(value)
    path = f"/drafts/{key}/w2"
    await session.submit(PERSON, key, form, form.get_by_role("button", name="Save W-2", exact=True),
                         path=f"{path}/new", result_path=path,
                         expected_fields=tuple((name, value) for _, name, value in fields))
    return (await rows(session, "W-2 records"))[(employer, instance, wages, withholding)]


def test_chromium_prepares_and_independently_reopens_wages_rideshare_and_nested_expenses(tmp_path):
    async def check(profile, binary):
        employer_a = w2("w2-a", "EMPLOYER-SECRET-A", (
            field_plan("wages", "12345.67", "wages-a"), field_plan("withholding", "1250.00", "tax-a"),
        ), allow_create=True)
        employer_b = w2("w2-b", "EMPLOYER-SECRET-B", (field_plan("wages", "8025.40", "wages-b"),),
                        allow_create=True)
        plan = DraftPlan(identity=PERSON, sections=(
            SectionPlan(section_key="w2", records=(employer_a, employer_b)),
            SectionPlan(section_key="rideshare", fields=(
                field_plan("gross", "6200.00", "gross", "gross-1099k", "gross-platform"),
                field_plan("fees", "600.00", "fees"),
            )),
            SectionPlan(section_key="expenses", records=(expense("expense-a", "RECEIPT-A"),
                                                         expense("expense-z", "RECEIPT-Z"))),
        ))
        script = [
            ("w2", "list_records", {}),
            ("w2", "ensure_record", {"sourceRecordId": "w2-b"}),
            ("w2", "set_field", {"bindingId": "wages-b"}),
            ("w2", "save_draft", {}),
            ("w2", "ensure_record", {"sourceRecordId": "w2-a"}),
            ("w2", "set_fields", {}),
            ("w2", "save_draft", {}),
            ("w2", "open_section", {}),
            ("rideshare", "set_fields", {}),
            ("rideshare", "save_draft", {}),
            ("rideshare", "open_detail", {}),
            ("expenses", "list_records", {}),
            ("expenses", "ensure_record", {"sourceRecordId": "expense-z"}),
            ("expenses", "set_fields", {}),
            ("expenses", "save_draft", {}),
            ("expenses", "ensure_record", {"sourceRecordId": "expense-a"}),
            ("expenses", "set_fields", {}),
            ("expenses", "save_draft", {}),
        ]
        async with WebDraft.launch(profile, executable_path=binary) as session:
            key = await create(session)
            verified, journal = await run_plan(session, key, plan, script)
            assert not verified.issues
            assert set(verified.verified_binding_ids) == {
                "wages-a", "tax-a", "wages-b", "gross", "fees", "description-expense-a", "amount-expense-a",
                "description-expense-z", "amount-expense-z",
            }
            by_binding = {item.binding_id: item for item in verified.readbacks}
            assert set(by_binding) == set(verified.verified_binding_ids)
            assert all(item.scope == journal.scope and item.matches_intended for item in by_binding.values())
            assert by_binding["wages-a"].source_record_id == "w2-a"
            assert by_binding["wages-b"].source_record_id == "w2-b"
            assert by_binding["wages-a"].native_row_key != by_binding["wages-b"].native_row_key
            assert by_binding["wages-a"].normalized_value == "12345.67"
            assert by_binding["gross"].source_record_id is by_binding["gross"].native_row_key is None
            assert by_binding["gross"].normalized_value == "6200"
            assert by_binding["gross"].fact_ids == ("gross-1099k", "gross-platform")
            gross, = [permit for permit in journal.permits if permit.action.name == "set_fields"
                      and any(field.binding_id == "gross" for field in permit.action.arguments.fields)]
            assert {field.binding_id: field.fact_ids for field in gross.action.arguments.fields} == {
                "gross": ("gross-1099k", "gross-platform"), "fees": ("fact-fees",),
            }
            assert gross.details.before == {"gross": "", "fees": ""}
            assert gross.details.intended == {"gross": "6200.00", "fees": "600.00"}
        # Use a fresh browser and the site's own return list; the previous DOM and cookies are gone.
        async with WebDraft.launch(profile, executable_path=binary) as reviewer:
            assert await reviewer.open_existing(PERSON, key) == key
            wages = await rows(reviewer, "W-2 records")
            assert set(wages) == {("EMPLOYER-SECRET-A", "ORIGINAL", "12345.67", "1250.00"),
                                  ("EMPLOYER-SECRET-B", "ORIGINAL", "8025.40", "")}
            assert len(set(wages.values())) == 2
            await follow(reviewer, key, "Rideshare", "rideshare")
            assert await reviewer.page.get_by_label("Gross receipts", exact=True).input_value() == "6200.00"
            assert await reviewer.page.get_by_label("Platform fees", exact=True).input_value() == "600.00"
            await follow(reviewer, key, "Expenses", "rideshare/expenses")
            expenses = await rows(reviewer, "Expense records")
            assert set(expenses) == {("RECEIPT-A", "Expense for RECEIPT-A", "42.00"),
                                     ("RECEIPT-Z", "Expense for RECEIPT-Z", "42.00")}
            assert len(set(expenses.values())) == 2

    binary = chromium_executable()
    with sigma_practice_target(tmp_path / "practice.sqlite") as origin:
        asyncio.run(check(practice_web_profile(origin), binary))


def test_correction_reuses_verified_row_and_preserves_unaddressed_preparer_values(tmp_path):
    async def check(profile, binary):
        async with WebDraft.launch(profile, executable_path=binary) as session:
            key = await create(session)
            record = await manual_w2(session, key, "EMPLOYER-SECRET-A", "10000.75", "570.00")
            other = await manual_w2(session, key, "EMPLOYER-SECRET-A", "2000.25", "95.20", "SECOND")
            correct = w2("w2-a", "EMPLOYER-SECRET-A", (
                field_plan("withholding", "0", "correct-withholding", expected="570"),
            ), external=record)
            plan = DraftPlan(identity=PERSON, sections=(SectionPlan(section_key="w2", records=(correct,)),))
            result, journal = await run_plan(session, key, plan, [
                ("w2", "ensure_record", {"sourceRecordId": "w2-a"}),
                ("w2", "set_field", {"bindingId": "correct-withholding"}),
                ("w2", "save_draft", {}),
            ])
            assert result.verified_binding_ids == ("correct-withholding",) and not result.issues
            assert journal.permits[1].details.before == "570.00"
            assert await rows(session, "W-2 records") == {
                ("EMPLOYER-SECRET-A", "ORIGINAL", "10000.75", "0"): record,
                ("EMPLOYER-SECRET-A", "SECOND", "2000.25", "95.20"): other,
            }
            # Replaying an already saved correction matches the row and verifies without another write.
            replay, journal = await run_plan(session, key, plan, [])
            assert replay == result and journal.permits == []

    binary = chromium_executable()
    with sigma_practice_target(tmp_path / "practice.sqlite") as origin:
        asyncio.run(check(practice_web_profile(origin), binary))


def test_wrong_native_key_and_preparer_conflict_are_not_permission_to_mutate(tmp_path):
    async def check(profile, binary):
        async with WebDraft.launch(profile, executable_path=binary) as session:
            key = await create(session)
            record = await manual_w2(session, key, "EMPLOYER-SECRET-A", "10000.75", "800.00")
            other = await manual_w2(session, key, "EMPLOYER-SECRET-B", "21000.24", "89.20")
            correction = w2("correct-a", "EMPLOYER-SECRET-A", (
                field_plan("withholding", "0", "withholding", expected="570.00"),
                field_plan("wages", "5000.25", "wages", expected="10000.75", owned=True),
            ), external=record)
            wrong = w2("wrong", "GHOST", (field_plan("wages", "107.40", "unsupported"),), external=other)
            plan = DraftPlan(identity=PERSON, sections=(SectionPlan(section_key="w2", records=(correction, wrong)),))
            scope = scope_for(session, key)
            reviewed = layout(scope.profile_id)
            host = WebActions(session, scope, reviewed, plan)
            journal = Journal(scope)
            seen = await host.observe()
            assert seen.blocked_binding_ids == ("unsupported", "wages", "withholding")
            assert seen.completed_binding_ids == () and not seen.pending_save
            assert {c.action.arguments.source_record_id for c in seen.choices if c.action.name == "ensure_record"} == {
                "correct-a",
            }
            assert not any(choice.action.name == "open_section" for choice in seen.choices)
            select = choose(seen, "ensure_record", source_record_id="correct-a")
            permit = await journal.begin(scope, select)
            wrong_owner = permit.model_copy(update={"scope": scope.model_copy(update={"owner_generation": 2})})
            with pytest.raises(ToolHostError, match="scope-mismatch"):
                await host.execute(select.action, wrong_owner)
            assert (await host.execute(select.action, permit)).status == "applied"
            active = await host.observe()
            assert not any(c.action.mutates for c in active.choices)
            fabricated = Action(name="set_field", arguments={"observationId": active.observation_id,
                                "controlRef": "control-0", "bindingId": "withholding", "factIds": ["fact-withholding"]})
            with pytest.raises(ToolHostError, match="unauthorized-action"):
                await host.execute(fabricated, None)
            back = choose(active, "open_detail")
            # Native same-return reopen without the backend invalidates the prior field observation.
            await session.reopen(PERSON, key)
            with pytest.raises(ToolHostError, match="stale-observation"):
                await host.execute(back.action, None)
            result = await host.verify()
            assert result.verified_binding_ids == ()
            assert set(result.issues) == {"field-conflict", "record-identity-conflict"}
            assert {item.binding_id: Decimal(item.normalized_value) for item in result.readbacks} == {
                "withholding": Decimal("800"), "wages": Decimal("10000.75"),
            }
            assert all(item.native_row_key == record and not item.matches_intended for item in result.readbacks)
            assert await rows(session, "W-2 records") == {
                ("EMPLOYER-SECRET-A", "ORIGINAL", "10000.75", "800.00"): record,
                ("EMPLOYER-SECRET-B", "ORIGINAL", "21000.24", "89.20"): other,
            }
            # Exceeding the reviewed complete list bound never proves absence.
            bounded = replace(reviewed, sections=(replace(reviewed.sections[0],
                records=replace(reviewed.sections[0].records, row_limit=1)), *reviewed.sections[1:]))
            with pytest.raises(ToolHostError, match="stale-observation"):
                await WebActions(session, scope, bounded, plan).observe()
            wrong_identity = plan.model_copy(update={"identity": PERSON.model_copy(update={"tax_year": 2024})})
            with pytest.raises(ToolHostError, match="stale-observation"):
                await WebActions(session, scope, reviewed, wrong_identity).observe()

    binary = chromium_executable()
    with sigma_practice_target(tmp_path / "practice.sqlite") as origin:
        asyncio.run(check(practice_web_profile(origin), binary))


def test_save_with_unexpected_row_multiplicity_requires_reconciliation(tmp_path):
    async def check(profile, binary):
        async with WebDraft.launch(profile, executable_path=binary) as session:
            key = await create(session)
            plan = DraftPlan(identity=PERSON, sections=(SectionPlan(section_key="w2", records=(
                w2("w2-a", "EMPLOYER-SECRET-A", (field_plan("wages", "8601.40", "wages-a"),), allow_create=True),
            )),))
            scope = scope_for(session, key)
            host = WebActions(session, scope, layout(scope.profile_id), plan)
            journal = Journal(scope)
            for action in ("ensure_record", "set_field"):
                choice = choose(await host.observe(), action)
                assert (await host.execute(choice.action, await journal.begin(scope, choice))).status == "applied"
            save = choose(await host.observe(), "save_draft")
            # Deliberately model a broken exclusivity guarantee. The independent site lets a
            # second actor add a row; successful native Save must not hide this change.
            async with WebDraft.launch(profile, executable_path=binary) as actor:
                await actor.open_existing(PERSON, key)
                await manual_w2(actor, key, "EXTERNAL-ACTOR", "5.10", "")
            permit = await journal.begin(scope, save)
            assert (await host.execute(save.action, permit)).status == "uncertain"
            with pytest.raises(ToolHostError, match="reconciliation-required"):
                await host.observe()
            assert (await host.verify()).issues == ("reconciliation-required",)
            assert set(await rows(session, "W-2 records")) == {
                ("EXTERNAL-ACTOR", "ORIGINAL", "5.10", ""), ("EMPLOYER-SECRET-A", "ORIGINAL", "8601.40", ""),
            }

    binary = chromium_executable()
    with sigma_practice_target(tmp_path / "practice.sqlite") as origin:
        asyncio.run(check(practice_web_profile(origin), binary))


@pytest.mark.parametrize("approved", [False, True], ids=["unexplained-default", "explicit-default"])
def test_new_row_preflights_all_native_defaults_before_filling(tmp_path, monkeypatch, approved):
    native_default = "7.25"

    async def check(profile, binary):
        async with WebDraft.launch(profile, executable_path=binary) as session:
            key = await create(session)
            posts = []
            session.page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
            plan = DraftPlan(identity=PERSON, sections=(SectionPlan(section_key="w2", records=(
                w2("w2-a", "EMPLOYER-SECRET-A", (field_plan("wages", "17.00", "wages-a"),), allow_create=True),
            )),))
            scope = scope_for(session, key)
            reviewed = layout(scope.profile_id)
            if approved:
                section = reviewed.sections[0]
                reviewed = replace(reviewed, sections=(
                    replace(section, records=replace(section.records, approved_defaults=(("withholding", native_default),))),
                    *reviewed.sections[1:],
                ))
            else:
                async def must_not_fill(*_args):
                    pytest.fail("Unexpected defaults must block before the first field fill.")

                monkeypatch.setattr(WebActions, "_fill", must_not_fill)
            host = WebActions(session, scope, reviewed, plan)
            journal = Journal(scope)

            async def perform(name):
                choice = choose(await host.observe(), name)
                return (await host.execute(choice.action, await journal.begin(scope, choice))).status

            assert (await host.observe()).completed_binding_ids == ()
            if not approved:
                assert await perform("ensure_record") == "unchanged"
                seen = await host.observe()
                assert seen.blocked_binding_ids == ("wages-a",) and not seen.pending_save
                assert not any(choice.action.mutates for choice in seen.choices)
                assert (await host.verify()).issues == ("unexplained-record-defaults",)
                assert posts == [] and await rows(session, "W-2 records") == {}
                return

            assert await perform("ensure_record") == "applied"
            assert (await host.observe()).pending_save
            assert await perform("set_field") == "applied"
            seen = await host.observe()
            assert seen.completed_binding_ids == seen.blocked_binding_ids == () and seen.pending_save and posts == []
            assert await perform("save_draft") == "applied"
            seen = await host.observe()
            assert seen.completed_binding_ids == ("wages-a",) and not seen.pending_save
            assert len(posts) == 1
            assert set(await rows(session, "W-2 records")) == {("EMPLOYER-SECRET-A", "ORIGINAL", "17.00", native_default)}
            fresh = WebActions(session, scope, reviewed, plan)
            assert (await fresh.observe()).completed_binding_ids == ("wages-a",)
            assert (await fresh.verify()).verified_binding_ids == ("wages-a",)

    binary = chromium_executable()
    with sigma_practice_target(tmp_path / "practice.sqlite", new_w2_withholding=native_default) as origin:
        asyncio.run(check(practice_web_profile(origin), binary))


def test_verification_requires_exact_scope_and_matching_binding_projection():
    scope = JobScope(job_id="job-a", owner_generation=1, profile_id="profile", draft_key="draft-a")
    observed = VerifiedReadback(
        scope=scope, section_key="w2", field_key="wages", binding_id="wages-a", fact_ids=("fact-a",),
        source_record_id="record-a", native_row_key="row-a", value_kind="decimal",
        normalized_value="1500.75", matches_intended=True,
    )
    data = dict(verified_binding_ids=("wages-a",), readbacks=(observed,))
    result = Verification(scope=scope, **data)
    assert Verification.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError, match="uniquely match the scoped verification"):
        Verification(scope=scope.model_copy(update={"draft_key": "draft-b"}), **data)
    with pytest.raises(ValidationError, match="uniquely match the scoped verification"):
        Verification(scope=scope, verified_binding_ids=("wages-b",), readbacks=(observed,))
