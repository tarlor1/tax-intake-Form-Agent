"""Portfolio CLI safety checks; all documents and provider responses are synthetic."""

from __future__ import annotations

import json
from hashlib import sha256
from shutil import copyfile

import pytest

import main as cli
from src import config
from src.drafts import ReviewTicket, save_review_ticket
from src.logging_setup import setup_logging


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    original_form = config.PROJECT_ROOT / "intake_form" / "index.html"
    form = tmp_path / "intake_form"
    form.mkdir()
    copyfile(original_form, form / "index.html")
    samples = tmp_path / "sample_inputs"
    samples.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "BUNDLED_INPUTS", samples)
    monkeypatch.setattr(cli, "setup_logging", lambda folder: folder / "run.log")
    return samples, tmp_path / "outputs"


def test_bundled_sample_manifest_matches_the_checked_in_files():
    samples = cli.list_intake_files(config.BUNDLED_INPUTS)
    with cli._approved_synthetic_inputs(samples) as approved:
        assert [path.name for path in approved] == [path.name for path in samples]
        assert all(
            original != copy and original.read_bytes() == copy.read_bytes()
            for original, copy in zip(samples, approved, strict=True)
        )


def test_default_stays_offline_with_both_keys_and_preserves_prior_runs(workspace, monkeypatch):
    inputs, outputs = workspace
    (inputs / "sample.txt").write_text("Subject: Synthetic request for tax preparation", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-unused-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-unused-key")
    monkeypatch.setenv("CLAUDE_EFFORT", "not-a-valid-effort")
    monkeypatch.setattr(config, "load_dotenv", lambda _: pytest.fail("offline mode must not load credentials"))

    assert cli.main(["--headless"]) == 0
    [first] = (outputs / "runs").glob("run-*/run_report.md")
    first_report = first.read_bytes()
    assert b"No external AI requests were made" in first_report

    assert cli.main(["--offline", "--headless", "--inputs", str(inputs)]) == 0
    [second] = set((outputs / "runs").glob("run-*/run_report.md")) - {first}
    assert "Custom input folder" in second.read_text(encoding="utf-8")
    assert first.read_bytes() == first_report


def test_bad_input_directory_preserves_existing_artifacts(workspace, monkeypatch):
    inputs, outputs = workspace
    old_report = outputs / "runs" / "run-earlier" / "run_report.md"
    old_report.parent.mkdir(parents=True)
    old_report.write_text("previous report", encoding="utf-8")
    monkeypatch.setattr(cli, "_make_ai", lambda _: pytest.fail("AI constructed before input validation"))

    assert cli.main(["--headless", "--inputs", str(inputs / "missing")]) == 2
    assert list((outputs / "runs").iterdir()) == [old_report.parent]
    assert old_report.read_text(encoding="utf-8") == "previous report"


def test_online_rejects_explicit_inputs_even_if_they_name_the_bundled_directory(workspace, monkeypatch, capsys):
    inputs, outputs = workspace
    monkeypatch.setattr(config, "load_dotenv", lambda _: pytest.fail("invalid invocation loaded credentials"))
    monkeypatch.setattr(cli, "_make_ai", lambda _: pytest.fail("invalid invocation constructed an AI"))

    assert cli.main(["--online-synthetic", "gemini", "--inputs", str(inputs)]) == 2
    assert "cannot be used with --inputs" in capsys.readouterr().err
    assert not outputs.exists()


def test_online_selects_only_the_named_provider_settings(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda _: None)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "test-gemini-model")
    monkeypatch.setenv("CLAUDE_MODEL", "test-claude-model")
    monkeypatch.setenv("CLAUDE_EFFORT", " high ")

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is required"):
        config.load_settings(online_provider="claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic-key")
    claude = config.load_settings(online_provider="claude")
    assert (claude.online_provider, claude.model, claude.effort) == ("claude", "test-claude-model", "high")
    monkeypatch.setenv("CLAUDE_EFFORT", "invalid-but-irrelevant")
    gemini = config.load_settings(online_provider="gemini", model="test-model-override")
    assert (gemini.online_provider, gemini.model) == ("gemini", "test-model-override")


@pytest.mark.parametrize("changed", ["extra", "missing", "replaced"])
def test_changed_sample_set_cannot_reach_provider(workspace, monkeypatch, changed):
    inputs, outputs = workspace
    approved = b"Approved synthetic email."
    sample = inputs / "sample.txt"
    sample.write_bytes(approved)
    if changed == "extra":
        (inputs / "extra.txt").write_text("Unapproved input fixture.", encoding="utf-8")
    elif changed == "missing":
        sample.unlink()
    else:
        sample.write_bytes(b"Unapproved replacement fixture.")
    monkeypatch.setattr(cli, "_SYNTHETIC_FIXTURES", {sample.name: sha256(approved).hexdigest()})
    monkeypatch.setattr(config, "load_dotenv", lambda _: None)
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    monkeypatch.setattr(cli, "_make_ai", lambda _: pytest.fail("nonapproved data could reach the provider"))

    assert cli.main(["--headless", "--online-synthetic", "gemini"]) == 2
    assert not outputs.exists()


def test_approved_online_run_uses_checked_snapshot_and_does_not_export_invalid_reply(workspace, monkeypatch, caplog):
    inputs, outputs = workspace
    approved = b"Approved synthetic email."
    sample = inputs / "sample.txt"
    sample.write_bytes(approved)
    monkeypatch.setattr(cli, "_SYNTHETIC_FIXTURES", {sample.name: sha256(approved).hexdigest()})
    monkeypatch.setattr(config, "load_dotenv", lambda _: None)
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    sensitive = "SYNTHETIC_INVALID_PROVIDER_RESPONSE"
    received = []

    class ScriptedAI:
        name = "synthetic test engine"

        def extract(self, text, previous_error=None):
            received.append(text)
            return sensitive

        def usage_summary(self):
            return None

    def make_ai(settings):
        assert settings.online_provider == "gemini"
        sample.write_bytes(b"Replacement after validation fixture.")
        return ScriptedAI()

    monkeypatch.setattr(cli, "_make_ai", make_ai)

    assert cli.main(["--headless", "--online-synthetic", "gemini"]) == 0
    assert received == [approved.decode(), approved.decode()]
    [report] = (outputs / "runs").glob("run-*/run_report.md")
    [ticket] = (report.parent / "needs_review").glob("*.json")
    ticket_text = ticket.read_text(encoding="utf-8")
    assert set(json.loads(ticket_text)) == {"source_file", "attempts", "errors"}
    assert sensitive not in ticket_text
    report_text = report.read_text(encoding="utf-8")
    assert sensitive not in report_text
    assert "Online AI processing via Gemini was authorized" in report_text
    assert sensitive not in caplog.text


def test_preexisting_logs_and_reports_survive_with_distinct_run_logs(workspace, monkeypatch):
    inputs, outputs = workspace
    (inputs / "sample.txt").write_text("Subject: Synthetic incomplete request", encoding="utf-8")
    monkeypatch.setattr(cli, "setup_logging", setup_logging)
    assert cli.main(["--headless"]) == 0
    [first] = (outputs / "runs").glob("*/logs/*.log")
    first_log = first.read_bytes()
    assert b"Offline mode" in first_log and b"Report:" in first_log
    assert cli.main(["--headless"]) == 0
    [second] = set((outputs / "runs").glob("*/logs/*.log")) - {first}
    assert b"Offline mode" in second.read_bytes()
    assert first.read_bytes() == first_log


def test_runtime_failure_does_not_print_exception_payload(workspace, monkeypatch, caplog):
    inputs, outputs = workspace
    (inputs / "sample.txt").write_text("Synthetic input", encoding="utf-8")
    payload = "SYNTHETIC_PRIVATE_EXCEPTION_BODY"

    def fail(*_):
        raise ValueError(payload)

    monkeypatch.setattr(cli, "_make_ai", fail)
    assert cli.main(["--headless"]) == 2
    assert "Could not complete the portfolio intake demo (ValueError)" in caplog.text
    assert payload not in caplog.text
    assert not list(outputs.rglob("run_report.*"))


def test_review_tickets_for_same_stem_do_not_replace_each_other(tmp_path):
    first = save_review_ticket(tmp_path, ReviewTicket("intake.txt", 2, ["first"]))
    second = save_review_ticket(tmp_path, ReviewTicket("intake.pdf", 2, ["second"]))
    assert first != second
    assert json.loads(first.read_text(encoding="utf-8"))["source_file"] == "intake.txt"
    assert json.loads(second.read_text(encoding="utf-8"))["source_file"] == "intake.pdf"
