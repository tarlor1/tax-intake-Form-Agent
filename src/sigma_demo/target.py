"""Independent synthetic draft website; never point it at real taxpayer data."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Iterator
from contextlib import closing, contextmanager
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sqlite3
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    client_reference TEXT NOT NULL,
    tax_year TEXT NOT NULL,
    return_type TEXT NOT NULL,
    gross_receipts TEXT NOT NULL DEFAULT '',
    platform_fees TEXT NOT NULL DEFAULT '',
    UNIQUE (client_reference, tax_year, return_type)
);
CREATE TABLE IF NOT EXISTS w2s (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    employer_reference TEXT NOT NULL,
    form_instance TEXT NOT NULL,
    wages TEXT NOT NULL,
    withholding TEXT NOT NULL,
    social_security_wages TEXT NOT NULL DEFAULT '',
    UNIQUE (draft_id, employer_reference, form_instance)
);
CREATE TABLE IF NOT EXISTS expenses (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    receipt_reference TEXT NOT NULL,
    description TEXT NOT NULL,
    amount TEXT NOT NULL,
    UNIQUE (draft_id, receipt_reference)
);
CREATE TABLE IF NOT EXISTS information_returns (
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    section TEXT NOT NULL CHECK (section IN ('1099-k', '1099-nec')),
    amount TEXT NOT NULL,
    withholding TEXT NOT NULL,
    PRIMARY KEY (draft_id, section)
);
"""
_INFORMATION_RETURNS = {
    "1099-k": ("1099-K", "gross", "Box 1a gross amount"),
    "1099-nec": ("1099-NEC", "compensation", "Box 1 nonemployee compensation"),
}
_INFORMATION_WITHHOLDING = ("withholding", "Box 4 federal withholding")


def _draft_pattern(include_information_returns: bool) -> str:
    sections = "|".join(("w2", r"rideshare(?:/expenses)?",
                         *(_INFORMATION_RETURNS if include_information_returns else ())))
    return (rf"/drafts/(?P<draft>[a-f0-9]{{32}})/(?P<section>{sections})"
            r"(?:/(?P<row>new|[a-f0-9]{32}))?")


_DRAFT_ROUTES = {enabled: re.compile(_draft_pattern(enabled)) for enabled in (False, True)}
_IDENTITY_FIELDS = ("client_reference", "tax_year", "return_type")
EXACT_SEARCH_COVERAGE = '<p data-search-coverage="exact-exhaustive">Exact search complete.</p>'
_W2_COLUMNS = (
    ("employer_reference", "Employer reference"),
    ("form_instance", "Form instance"),
    ("wages", "Box 1 wages"),
    ("withholding", "Box 2 federal withholding"),
)
_W2_BOX3 = ("social_security_wages", "Box 3 Social Security wages")
_W2_EXTENDED_COLUMNS = (*_W2_COLUMNS, _W2_BOX3)
_EXPENSE_COLUMNS = (
    ("receipt_reference", "Receipt reference"),
    ("description", "Description"),
    ("amount", "Amount"),
)


class _BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@contextmanager
def _connection(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            yield db


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title} | Practice Returns</title>
<style>
body {{ font: 16px system-ui, sans-serif; margin: 0; background: #f4f6fa; color: #142235; }}
.practice-banner {{ padding: 1rem 2rem; background: #173451; color: white; font-weight: 600; }}
main {{ padding: 1.5rem 2rem; }}
label {{ display: block; margin: .6rem 0; }}
input, select, button {{ font: inherit; padding: .35rem .6rem; }}
main[data-section] {{ display: grid; grid-template-columns: minmax(10rem, max-content) minmax(0, 1fr); gap: 1rem 2rem; }}
main[data-section] > header {{ grid-column: 1 / -1; }}
main[data-section] > section {{ padding: .5rem 1.5rem 1.5rem; background: white; border: 1px solid #cad4e0; }}
nav {{ display: grid; gap: .25rem; align-content: start; }}
nav a {{ padding: .55rem .7rem; }}
nav a[aria-current] {{ background: #deebfb; font-weight: 650; }}
th, td {{ text-align: left; padding: .4rem .9rem .4rem .3rem; border-bottom: 1px solid #e4e9ef; }}
dialog {{ position: static; margin: 0; border: 1px solid #91a7bd; }}
</style></head><body>{body}</body></html>"""


def _input(name: str, label: str, value: str, constraints: str = "") -> str:
    return f'<label>{label} <input name="{name}" value="{escape(value)}" {constraints}></label>'


def _money_input(name: str, label: str, value: str, *, required: bool = False) -> str:
    constraints = 'type="number" min="0" max="999999999.99" step="0.01"'
    return _input(name, label, value, constraints + (" required" if required else ""))


def _text_input(name: str, label: str, value: str) -> str:
    return _input(name, label, value, 'required maxlength="80"')


def _identity_attributes(draft: sqlite3.Row) -> str:
    return ' '.join(
        f'data-{key}="{escape(draft[column])}"'
        for key, column in (
            ("draft-id", "id"), ("client-reference", "client_reference"),
            ("tax-year", "tax_year"), ("return-type", "return_type"),
        )
    ) + ' data-status="unfiled"'


def _identity_line(draft: sqlite3.Row) -> str:
    return (
        f'<p>Client reference: <strong>{escape(draft["client_reference"])}</strong> · '
        f'Tax year: <strong>{draft["tax_year"]}</strong> · Individual · Unfiled</p>'
        f'<p>Draft key: <code>{draft["id"]}</code></p>'
    )


def _return_list(identity: dict[str, str], result: sqlite3.Row | None) -> str:
    reference = _text_input("client_reference", "Client reference", identity.get("client_reference", ""))
    year = _input("tax_year", "Tax year", identity.get("tax_year", ""), 'required pattern="[0-9]{4}"')
    if result is not None:
        matches = f'''<section aria-label="Exact search result" {_identity_attributes(result)}>
{_identity_line(result)}<a href="/drafts/{result['id']}/w2">Open draft</a></section>'''
    elif identity:
        hidden = ''.join(
            f'<input type="hidden" name="{name}" value="{escape(identity[name])}">'
            for name in _IDENTITY_FIELDS
        )
        matches = f'''<p role="status">No matching draft.</p>
<form action="/drafts" method="post">{hidden}<button>Create draft</button></form>'''
    else:
        matches = ""
    coverage = EXACT_SEARCH_COVERAGE if identity else ""
    return _page("Return list", f'''<main><h1>Return list</h1>
<form action="/" method="get" aria-label="Exact draft search">
{reference}{year}
<label>Return type <select name="return_type"><option value="individual">Individual</option></select></label>
<button>Search</button></form>{coverage}{matches}</main>''')


def _draft_page(draft: sqlite3.Row, section: str, title: str, body: str,
                include_information_returns: bool) -> str:
    root = f"/drafts/{draft['id']}"
    if section == "expenses":
        body = f'''<dialog open aria-labelledby="expense-title">
<h2 id="expense-title">{title}</h2>{body}
<p><a href="{root}/rideshare">Back to rideshare</a></p></dialog>'''
        title = "Rideshare Schedule C"

    def section_link(key: str, label: str) -> str:
        current = ' aria-current="page"' if section == key or (key == "rideshare" and section == "expenses") else ""
        return f'<a href="{root}/{key}"{current}>{label}</a>'

    information_links = (
        "".join(section_link(key, form[0]) for key, form in _INFORMATION_RETURNS.items())
        if include_information_returns else ""
    )
    return _page(title, f'''<main {_identity_attributes(draft)} data-section="{section}">
<header><p class="practice-banner">Synthetic tax preparation · practice only</p>{_identity_line(draft)}</header>
<nav aria-label="Return sections">
<a href="/">Return list</a>
{section_link("w2", "W-2s")}
{information_links}
{section_link("rideshare", "Rideshare")}
</nav><section aria-labelledby="section-title"><h1 id="section-title">{title}</h1>{body}</section></main>''')


def _record_table(label: str, columns: tuple[tuple[str, str], ...], records: list[sqlite3.Row],
                  route: str, edit_label: str) -> str:
    headings = ''.join(f'<th scope="col">{heading}</th>' for _, heading in columns)
    rows = []
    for record in records:
        cells = ''.join(f'<td>{escape(record[column])}</td>' for column, _ in columns)
        record_id = record['id']
        rows.append(f'''<tr data-record-id="{record_id}">{cells}
<td><a href="{route}/{record_id}">{edit_label}</a></td></tr>''')
    return f'''<table aria-label="{label}"><thead><tr>{headings}<th scope="col">Actions</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>'''


def _record_form(route: str, row: str, fields: str, save_label: str, back_label: str) -> str:
    record = f' data-record-id="{row}"' if row != "new" else ""
    return f'''<form action="{route}/{row}" method="post"{record}>
{fields}<button>{save_label}</button></form><p><a href="{route}">{back_label}</a></p>'''


def _w2_fields(record: sqlite3.Row | None, new_withholding: str, include_box3: bool) -> str:
    return ''.join(
        (_text_input if name in {"employer_reference", "form_instance"} else _money_input)(
            name, label, record[name] if record is not None else (new_withholding if name == "withholding" else "")
        )
        for name, label in (_W2_EXTENDED_COLUMNS if include_box3 else _W2_COLUMNS)
    )


def _rideshare_fields(gross: str, fees: str) -> str:
    return (_money_input("gross_receipts", "Gross receipts", gross)
            + _money_input("platform_fees", "Platform fees", fees))


def _information_fields(section: str, amount: str = "", withholding: str = "") -> str:
    _, name, label = _INFORMATION_RETURNS[section]
    return _money_input(name, label, amount) + _money_input(*_INFORMATION_WITHHOLDING, withholding)


def _information_page(db: sqlite3.Connection, draft: sqlite3.Row, section: str) -> str:
    route = f"/drafts/{draft['id']}/{section}"
    record = db.execute(
        "SELECT amount, withholding FROM information_returns WHERE draft_id = ? AND section = ?",
        (draft['id'], section),
    ).fetchone()
    label = _INFORMATION_RETURNS[section][0]
    fields = _information_fields(section, *record) if record is not None else _information_fields(section)
    return _draft_page(draft, section, f"Form {label}", f'''
<p>Information return only. Values are not carried to Schedule C.</p>
<form action="{route}" method="post">{fields}<button>Save {label}</button></form>''', True)


def _expense_fields(record: sqlite3.Row | None) -> str:
    return ''.join(
        _money_input(name, label, record[name] if record is not None else "", required=True)
        if name == "amount" else _text_input(name, label, record[name] if record is not None else "")
        for name, label in _EXPENSE_COLUMNS
    )


def _w2_page(db: sqlite3.Connection, draft: sqlite3.Row, row: str | None,
             new_withholding: str, include_box3: bool, include_information_returns: bool) -> str:
    route = f"/drafts/{draft['id']}/w2"
    if row is None:
        rows = db.execute(
            "SELECT * FROM w2s WHERE draft_id = ? ORDER BY employer_reference, form_instance",
            (draft['id'],),
        ).fetchall()
        columns = _W2_EXTENDED_COLUMNS if include_box3 else _W2_COLUMNS
        table = _record_table("W-2 records", columns, rows, route, "Edit W-2")
        return _draft_page(draft, "w2", "W-2s", f'<p><a href="{route}/new">Add W-2</a></p>{table}',
                           include_information_returns)

    record = None if row == "new" else _existing_row(db, "w2s", draft['id'], row)
    fields = _w2_fields(record, new_withholding, include_box3)
    body = _record_form(route, row, fields, "Save W-2", "Back to W-2s")
    return _draft_page(draft, "w2", "New W-2" if row == "new" else "Edit W-2", body,
                       include_information_returns)


def _rideshare_page(db: sqlite3.Connection, draft: sqlite3.Row, include_information_returns: bool) -> str:
    route = f"/drafts/{draft['id']}/rideshare"
    fields = _rideshare_fields(draft['gross_receipts'], draft['platform_fees'])
    count = db.execute("SELECT count(*) FROM expenses WHERE draft_id = ?", (draft['id'],)).fetchone()[0]
    return _draft_page(draft, "rideshare", "Rideshare Schedule C", f'''
<form action="{route}" method="post">{fields}<button>Save rideshare</button></form>
<p><a href="{route}/expenses">Expenses</a> · Saved expense count: {count}</p>''', include_information_returns)


def _expenses_page(db: sqlite3.Connection, draft: sqlite3.Row, row: str | None,
                   include_information_returns: bool) -> str:
    route = f"/drafts/{draft['id']}/rideshare/expenses"
    if row is None:
        rows = db.execute(
            "SELECT * FROM expenses WHERE draft_id = ? ORDER BY receipt_reference", (draft['id'],)
        ).fetchall()
        table = _record_table("Expense records", _EXPENSE_COLUMNS, rows, route, "Edit expense")
        return _draft_page(draft, "expenses", "Expenses", f'<p><a href="{route}/new">Add expense</a></p>{table}',
                           include_information_returns)

    record = None if row == "new" else _existing_row(db, "expenses", draft['id'], row)
    fields = _expense_fields(record)
    body = _record_form(route, row, fields, "Save expense", "Back to expenses")
    return _draft_page(draft, "expenses", "New expense" if row == "new" else "Edit expense", body,
                       include_information_returns)


def _form(encoded: str, names: tuple[str, ...]) -> dict[str, str]:
    try:
        data = parse_qs(encoded, keep_blank_values=True, strict_parsing=True, max_num_fields=len(names))
    except ValueError as exc:
        raise _BadRequest("Malformed form.") from exc
    if data.keys() != set(names) or any(len(values) != 1 for values in data.values()):
        raise _BadRequest("Use the supplied form fields.")
    return {name: data[name][0] for name in names}


def _required_text(value: str) -> str:
    if not 0 < len(value) <= 80 or not value.strip() or not value.isprintable():
        raise _BadRequest("Enter a nonempty text value of up to 80 characters.")
    return value


def _money(value: str, *, required: bool = False) -> str:
    if (required or value) and not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", value):
        raise _BadRequest("Enter a nonnegative amount with up to two decimal places.")
    return value


def _identity(fields: dict[str, str]) -> tuple[str, str, str]:
    year, kind = fields['tax_year'], fields['return_type']
    if not re.fullmatch(r"[0-9]{4}", year) or kind != "individual":
        raise _BadRequest("Choose a four-digit tax year and the Individual return type.")
    return _required_text(fields['client_reference']), year, kind


def _existing_row(db: sqlite3.Connection, table: str, draft_id: str, row_id: str) -> sqlite3.Row:
    # table is selected by the recognized route, never by a request parameter.
    record = db.execute(f"SELECT * FROM {table} WHERE draft_id = ? AND id = ?", (draft_id, row_id)).fetchone()
    if record is None:
        raise _BadRequest("Record not found in this draft.", 404)
    return record


def _draft_route(db: sqlite3.Connection, path: str,
                 include_information_returns: bool) -> tuple[sqlite3.Row, str, str | None]:
    match = _DRAFT_ROUTES[include_information_returns].fullmatch(path)
    if not match or (match['row'] and match['section'] not in {"w2", "rideshare/expenses"}):
        raise _BadRequest("Page not found.", 404)
    draft = db.execute("SELECT * FROM drafts WHERE id = ?", (match['draft'],)).fetchone()
    if draft is None:
        raise _BadRequest("Draft not found.", 404)
    return draft, match['section'], match['row']


def _get(db: sqlite3.Connection, path: str, query: str, new_w2_withholding: str,
         include_box3: bool, include_information_returns: bool) -> str:
    if path == "/":
        if not query:
            return _return_list({}, None)
        fields = _form(query, _IDENTITY_FIELDS)
        result = db.execute(
            "SELECT * FROM drafts WHERE client_reference = ? AND tax_year = ? AND return_type = ?",
            _identity(fields),
        ).fetchone()
        return _return_list(fields, result)

    draft, section, row = _draft_route(db, path, include_information_returns)
    if section == "w2":
        return _w2_page(db, draft, row, new_w2_withholding, include_box3, include_information_returns)
    if section == "rideshare/expenses":
        return _expenses_page(db, draft, row, include_information_returns)
    if section in _INFORMATION_RETURNS:
        return _information_page(db, draft, section)
    return _rideshare_page(db, draft, include_information_returns)


def _post(db: sqlite3.Connection, path: str, encoded: str,
          include_box3: bool, include_information_returns: bool) -> str:
    if path == "/drafts":
        draft_id = uuid4().hex
        db.execute(
            "INSERT INTO drafts (id, client_reference, tax_year, return_type) VALUES (?, ?, ?, ?)",
            (draft_id, *_identity(_form(encoded, _IDENTITY_FIELDS))),
        )
        return f"/drafts/{draft_id}/w2"

    draft, section, row = _draft_route(db, path, include_information_returns)
    draft_id = draft['id']
    if section == "rideshare":
        fields = _form(encoded, ("gross_receipts", "platform_fees"))
        db.execute("UPDATE drafts SET gross_receipts = ?, platform_fees = ? WHERE id = ?", (
            _money(fields['gross_receipts']), _money(fields['platform_fees']), draft_id,
        ))
        return path
    if section in _INFORMATION_RETURNS:
        _, amount, _ = _INFORMATION_RETURNS[section]
        withholding, _ = _INFORMATION_WITHHOLDING
        fields = _form(encoded, (amount, withholding))
        db.execute("""INSERT INTO information_returns (draft_id, section, amount, withholding)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(draft_id, section) DO UPDATE
            SET amount = excluded.amount, withholding = excluded.withholding""",
            (draft_id, section, _money(fields[amount]), _money(fields[withholding])))
        return path

    if row is None:
        raise _BadRequest("Save an individual record, not its list.", 405)
    if section == "w2":
        columns = _W2_EXTENDED_COLUMNS if include_box3 else _W2_COLUMNS
        fields = _form(encoded, tuple(name for name, _ in columns))
        values = (
            _required_text(fields['employer_reference']), _required_text(fields['form_instance']),
            _money(fields['wages']), _money(fields['withholding']),
        )
        box3 = _money(fields["social_security_wages"]) if include_box3 else None
        if row == "new":
            db.execute("""INSERT INTO w2s
                (id, draft_id, employer_reference, form_instance, wages, withholding, social_security_wages)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (uuid4().hex, draft_id, *values, box3 or ""))
        else:
            _existing_row(db, "w2s", draft_id, row)
            db.execute("""UPDATE w2s SET employer_reference = ?, form_instance = ?, wages = ?, withholding = ?,
                social_security_wages = COALESCE(?, social_security_wages)
                WHERE draft_id = ? AND id = ?""", (*values, box3, draft_id, row))
    else:
        fields = _form(encoded, tuple(name for name, _ in _EXPENSE_COLUMNS))
        values = (
            _required_text(fields['receipt_reference']), _required_text(fields['description']),
            _money(fields['amount'], required=True),
        )
        if row == "new":
            db.execute("INSERT INTO expenses VALUES (?, ?, ?, ?, ?)", (uuid4().hex, draft_id, *values))
        else:
            _existing_row(db, "expenses", draft_id, row)
            db.execute("""UPDATE expenses SET receipt_reference = ?, description = ?, amount = ?
                WHERE draft_id = ? AND id = ?""", (*values, draft_id, row))
    return f"/drafts/{draft_id}/{section}"


class _PracticeHandler(BaseHTTPRequestHandler):
    server: _PracticeServer

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self._serve(post=False)

    def do_POST(self) -> None:
        self._serve(post=True)

    def _serve(self, *, post: bool) -> None:
        host = f"127.0.0.1:{self.server.server_port}"
        origins = self.headers.get_all("Origin", [])
        if (self.headers.get_all("Host", []) != [host] or self.headers.get("Sec-Fetch-Site") == "cross-site"
                or (post and origins and origins != [f"http://{host}"])):
            self.send_error(403, "This practice target only accepts local same-origin requests.")
            return
        target = urlsplit(self.path)
        try:
            with _connection(self.server.database) as db:
                if post:
                    if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                        raise _BadRequest("Use the native HTML form.", 415)
                    length = self.headers.get("Content-Length", "")
                    if not length.isdecimal() or not 0 < int(length) <= 4096:
                        raise _BadRequest("Invalid form length.")
                    encoded = self.rfile.read(int(length)).decode("ascii")
                    result = _post(db, target.path, encoded, self.server.include_box3,
                                   self.server.include_information_returns)
                else:
                    result = _get(db, target.path, target.query, self.server.new_w2_withholding,
                                  self.server.include_box3, self.server.include_information_returns)
        except _BadRequest as exc:
            self.send_error(exc.status, str(exc))
            return
        except UnicodeDecodeError:
            self.send_error(400, "Malformed form encoding.")
            return
        except sqlite3.IntegrityError:
            self.send_error(409, "A record with this identity already exists.")
            return

        if post:
            self.send_response(303)
            self.send_header("Location", result)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            policy = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
            if self.server.focus_script and _DRAFT_ROUTES[True].fullmatch(target.path):
                result = result.removesuffix("</body></html>") + self.server.focus_script + "</body></html>"
                policy += self.server.focus_policy
            body = result.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", policy)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class _PracticeServer(ThreadingHTTPServer):
    def __init__(self, database: Path, new_w2_withholding: str, include_box3: bool,
                 include_information_returns: bool, companion_origin: str | None):
        self.database = database
        self.new_w2_withholding = new_w2_withholding
        self.include_box3 = include_box3
        self.include_information_returns = include_information_returns
        self.focus_script = self.focus_policy = ""
        if companion_origin is not None:
            script = f"""document.addEventListener("focusin", (event) => {{
    const input = event.target;
    if (input instanceof HTMLInputElement && input.type === "number" && input.name && window.opener) {{
        const root = input.closest("main[data-draft-id][data-section]");
        if (!root || !["w2", "1099-k", "1099-nec"].includes(root.dataset.section)) return;
        window.opener.postMessage({{
            type: "practice-field", field: input.name, draft: root.dataset.draftId, section: root.dataset.section,
            pathname: window.location.pathname,
        }}, "{companion_origin}");
    }}
}}, {{passive: true}});"""
            digest = b64encode(sha256(script.encode("utf-8")).digest()).decode("ascii")
            self.focus_script = f"<script>{script}</script>"
            self.focus_policy = f"; script-src 'sha256-{digest}'"
        super().__init__(("127.0.0.1", 0), _PracticeHandler)


@contextmanager
def sigma_practice_target(
    database: Path, *, new_w2_withholding: str = "", include_box3: bool = False,
    include_information_returns: bool = False, companion_origin: str | None = None,
) -> Iterator[str]:
    """Serve synthetic native draft forms at an ephemeral loopback URL.

    Reusing the file across launches preserves draft and record keys. There is no
    fixture setup or write interface beyond the site's own search/create/save forms.
    Enable Box 3 or information returns only with a matching practice layout.
    A companion origin opts into read-only focus notifications for paired review.
    """
    if companion_origin is not None:
        match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", companion_origin)
        if not include_information_returns or not match or not 1024 <= int(match[1]) <= 65535:
            raise ValueError("Paired practice review requires the extended target and an exact loopback origin.")
    with _connection(database) as db:
        db.executescript(_SCHEMA)
        if not any(column["name"] == "social_security_wages" for column in db.execute("PRAGMA table_info(w2s)")):
            db.execute("ALTER TABLE w2s ADD COLUMN social_security_wages TEXT NOT NULL DEFAULT ''")
    with _PracticeServer(database, new_w2_withholding, include_box3, include_information_returns,
                         companion_origin) as server:
        thread = Thread(target=server.serve_forever, name="sigma-practice-target", daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()
            thread.join()
