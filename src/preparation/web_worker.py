"""Guarded web worker and its fixed, credential-free MCP subprocess entry point."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from typing import Protocol

from pydantic import TypeAdapter

from src.preparation.native_plan import DraftPlan
from src.preparation.ownership import OwnerGrant, OwnershipStore
from src.preparation.sigma_web import CreatePermit, SearchResult, WebContext, WebDraft, WebProfile, WebSection
from src.preparation.tool_contract import JobScope
from src.preparation.tools import open_stdio, serve_stdio
from src.preparation.web_actions import WebActions, WebLayout
from src.preparation.worker import WorkItem


class WebAttestations(Protocol):
    async def checkout(self, grant: OwnerGrant, item: WorkItem, draft: str | None) -> str: ...
    async def absent(self, grant: OwnerGrant, result: SearchResult) -> str: ...
    async def existing(self, grant: OwnerGrant, result: SearchResult) -> str: ...
    async def created(self, grant: OwnerGrant, step_id: str, draft: str) -> str: ...
    async def closed(self, grant: OwnerGrant, draft: str | None) -> str: ...


class WebWorker:
    def __init__(self, profile: WebProfile, layout: WebLayout, attest: WebAttestations,
                 protected_directory: Path, *, chromium: str | None = None, headed_practice: bool = False):
        if (
            layout.profile_id != profile.profile_id or not protected_directory.is_absolute()
            or not protected_directory.is_dir() or protected_directory.is_symlink()
            or (sys.platform != "win32" and protected_directory.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
        ):
            raise ValueError("The web worker needs a matching profile and a private provisioned directory.")
        if headed_practice and not profile.origin.startswith("http://"):
            raise ValueError("Visible worker mode requires a loopback practice profile.")
        self.profile, self.layout, self.attest = profile, layout, attest
        self.directory, self.chromium = protected_directory, chromium
        self.headed_practice = headed_practice

    @asynccontextmanager
    async def session(self, grant: OwnerGrant, item: WorkItem):
        if item.profile_id != self.profile.profile_id:
            raise ValueError("This worker is not enrolled for the assigned profile.")
        yield _Session(self, grant, item)


class _Session:
    def __init__(self, worker: WebWorker, grant: OwnerGrant, item: WorkItem):
        self.worker, self.grant, self.item = worker, grant, item
        self.draft = item.draft_key

    async def checkout(self) -> str:
        return await self.worker.attest.checkout(self.grant, self.item, self.draft)

    async def establish_draft(self, store: OwnershipStore) -> None:
        if self.draft is not None:
            return  # The scoped child still verifies native lookup and exact identity before any mutation.
        identity = self.item.identity
        async with WebDraft.launch(self.worker.profile, executable_path=self.worker.chromium,
                                   headed_practice=self.worker.headed_practice) as browser:
            result = await browser.search(identity)
            if result.status == "found":
                proof = await self.worker.attest.existing(self.grant, result)
                self.draft = await asyncio.to_thread(store.bind_existing_draft, self.grant, proof)
                if self.draft != result.draft_key:
                    raise ValueError("The independently verified existing draft did not match search.")
            else:
                intent = None

                async def authorize(absent: SearchResult) -> CreatePermit:
                    nonlocal intent
                    proof = await self.worker.attest.absent(self.grant, absent)
                    intent = await asyncio.to_thread(store.begin_create, self.grant, proof)
                    return CreatePermit(self.item.profile_id, identity, absent.proof, intent)

                self.draft = await browser.create(identity, authorize)
                if intent is None:
                    raise RuntimeError("Creation failed to supply a journal checkpoint.")
                proof = await self.worker.attest.created(self.grant, intent, self.draft)
                await asyncio.to_thread(store.complete_create, self.grant, intent, proof)
        await asyncio.to_thread(store.activate, self.grant, await self.checkout())

    @asynccontextmanager
    async def tools(self, scope: JobScope, plan: DraftPlan):
        if scope.draft_key != self.draft:
            raise ValueError("The native session and ownership scope do not match.")
        profile = TypeAdapter(WebProfile).dump_python(self.worker.profile, mode="json", exclude={"_draft_re"})
        payload = {
            "scope": scope.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
            "profile": profile, "layout": TypeAdapter(WebLayout).dump_python(self.worker.layout, mode="json"),
            "chromium": self.worker.chromium,
            "headed_practice": self.worker.headed_practice,
        }
        with TemporaryDirectory(prefix="job-", dir=self.worker.directory) as directory:
            path = Path(directory) / "assignment.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            async with open_stdio(Path(__file__).resolve(), scope, assignment_path=path, timeout_seconds=60,
                                  display_for_practice=self.worker.headed_practice) as host:
                path.unlink()  # The child has bound the scope and no longer needs the serialized assignment.
                yield host

    async def close_proof(self) -> str:
        return await self.worker.attest.closed(self.grant, self.draft)


async def _host(path: Path) -> None:
    payload = json.loads(path.read_bytes())
    settings = payload["profile"]
    if settings["origin"].startswith(("http://127.0.0.1:", "http://[::1]:")):
        # Only a parent explicitly constructed with WebProfile.for_test can produce this assignment.
        settings["context"] = TypeAdapter(WebContext).validate_python(settings["context"])
        settings["sections"] = TypeAdapter(tuple[WebSection, ...]).validate_python(settings["sections"])
        settings["return_types"] = tuple(settings["return_types"])
        profile = WebProfile.for_test(**settings)
    else:
        profile = TypeAdapter(WebProfile).validate_python(settings)
    layout = TypeAdapter(WebLayout).validate_python(payload["layout"])
    plan = DraftPlan.model_validate(payload["plan"])
    scope = JobScope.model_validate(payload["scope"])
    async with WebDraft.launch(profile, executable_path=payload["chromium"],
                               headed_practice=payload["headed_practice"]) as browser:
        await browser.open_existing(plan.identity, scope.draft_key)
        await serve_stdio(WebActions(browser, scope, layout, plan))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("The tool host requires one protected assignment file.")
    asyncio.run(_host(Path(sys.argv[1])))
