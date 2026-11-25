"""Classify inbox originals locally and quote only unambiguous, labeled tax amounts."""

import re
from collections import Counter
from decimal import Decimal
from unicodedata import normalize

from ..review_documents import MAX_QUOTE_CHARS, DocumentError, source_pages
from .local_pdf import PDFCell, ruled_tables

_DASHES = str.maketrans("‐‑‒–—−", "------")
_FORM_REFERENCE = re.compile(
    r"(?<!\w)(?:w\s*-?\s*(?:2[cg]?|[479])|1099(?:\s*-?\s*(?:misc|nec|int|div|oid|patr|"
    r"b|c|g|k|q|r|s|sa))?|1098(?:\s*-?\s*[et])?|1095(?:\s*-?\s*[abc])?|"
    r"1040(?:\s*-?\s*(?:sr|nr|x))?|1065|1120(?:\s*-?\s*s)?)(?![\w-])"
)
_FORM_TITLES = {
    "W-2": "wage and tax statement",
    "W-2c": "corrected wage and tax statement",
    "W-2G": "certain gambling winnings",
    "1099-K": "payment card and third party network transactions",
    "1099-NEC": "nonemployee compensation",
    "1099-MISC": "miscellaneous information",
}
_OMB_FORMS = {"0008": "W-2", "0116": "1099-NEC", "2205": "1099-K"}
_OMB = re.compile(r"\bomb\s*(?:no\.?\s*)?1545\s*-\s*([0-9]{4})\b")
_W2_EARNINGS = re.compile(r"^(?:20[0-9]{2} )?w\s*-?\s*2 (?:and|&) earnings summary\b")
_NOTICE = re.compile(
    r"^(?:(?:irs|internal revenue service)\s*[:|-]?\s*)?"
    r"(?:(?:final )?notice(?:$|[:\s]+(?:cp\s*[0-9]|of\b|number\b|from\b))|"
    r"(?:notice\s*[:|-]?\s*)?(?:cp|lt)\s*[0-9]{2,5}[a-z]?\b|letter\s+[0-9]{2,5}[a-z]?\b)"
)
_CORRESPONDENCE = re.compile(
    r"^(?:subject\s*:|dear\s|to whom it may concern\b|"
    r"(?:(?:new |tax )?client |tax(?: return)? )?(?:intake\b|checklist\b|questionnaire\b|correspondence\b)|"
    r"client (?:email|letter|note)\b|email (?:from|to)\b|(?:tax )?documents (?:needed|required|request)\b|"
    r"request for (?:tax |supporting )?(?:documents|information)\b|"
    r"please\s+(?:send|upload|provide|submit|attach|include)\b)"
)
_NON_SOURCE = re.compile(
    r"^(?:instructions for\b|(?:example|sample|blank)\s+(?:form\b|w\s*-?\s*[0-9]|109[589]\b)|"
    r"form\s+.*\b(?:example|sample|template)$|void(?:\s*[:=]\s*(?:yes|true))?$)"
)
_IDENTITY = re.compile(
    r"^(?:employer|employee|payer|filer|recipient)'?s?\b(?:\s*:|\s+(?:name|identification|address|.*\btin\b))|"
    r"^copy\s+(?:[a-d]|[12])\b"
)
_BOX_LINE = re.compile(r"(?:(box)\s+)?([0-9]{1,2}[a-z]?)(?=$|[\s.:=|])\s*[.:=|]?\s*(.*)")
_MONEY_NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3}){1,5}|[0-9]{1,18})(?:\.[0-9]{1,2})?"
_MONEY = re.compile(rf"(?<![\w$.,(\-])(?:-?\$?\s?{_MONEY_NUMBER}|\(\$?\s?{_MONEY_NUMBER}\))$")

# The first label in each tuple is the display label. Short aliases still have to
# agree with the printed box number; captions on their own never produce facts.
_FEDERAL_WITHHOLDING = ("Federal income tax withheld", "Income tax withheld")
_BOX_LABELS = {
    "W-2": {
        "1": ("Wages, tips, other compensation", "Wages", "Wages, tips and other compensation",
              "Wages, tips, other comp."),
        "2": _FEDERAL_WITHHOLDING,
        "3": ("Social Security wages",),
        "4": ("Social Security tax withheld",),
        "5": ("Medicare wages and tips", "Medicare wages"),
        "6": ("Medicare tax withheld",),
        "7": ("Social Security tips",),
        "8": ("Allocated tips",),
        "10": ("Dependent care benefits",),
        "11": ("Nonqualified plans",),
        "16": ("State wages, tips, etc.", "State wages"),
        "17": ("State income tax", "State income tax withheld"),
        "18": ("Local wages, tips, etc.", "Local wages"),
        "19": ("Local income tax", "Local income tax withheld"),
    },
    "1099-K": {
        "1a": (
            "Gross amount of payment card/third party network transactions",
            "Gross amount of payment card and third party network transactions",
            "Gross payment card and third party network transactions",
            "Gross amount of reportable payment transactions",
            "Gross amount",
        ),
        "1b": ("Card not present transactions",),
        "4": _FEDERAL_WITHHOLDING,
        "8": ("State income tax withheld", "State tax withheld"),
        **{f"5{letter}": (month,) for letter, month in zip(
            "abcdefghijkl", ("January", "February", "March", "April", "May", "June",
                             "July", "August", "September", "October", "November", "December"),
        )},
    },
    "1099-NEC": {
        "1": ("Nonemployee compensation", "Non-employee compensation"),
        "1a": ("Nonemployee compensation", "Non-employee compensation"),
        "4": _FEDERAL_WITHHOLDING,
        "5": ("State tax withheld", "State income tax withheld"),
        "7": ("State income",),
    },
}


def _matching_text(text: str) -> str:
    return " ".join(normalize("NFKC", text).translate(_DASHES).replace("’", "'").casefold().split())


def _label_key(label: str) -> str:
    return " ".join(re.sub(r"[.,/:=|()\-]", " ", label).split())


_LABEL_KEYS = {
    form: {box: tuple(_label_key(label.casefold()) for label in labels) for box, labels in boxes.items()}
    for form, boxes in _BOX_LABELS.items()
}


def _compact(text: str) -> str:
    # Small payroll PDFs sometimes encode captions without any space glyphs.
    return re.sub(r"[\W_]+", "", _matching_text(text))


_CELL_CAPTIONS = {
    form: {f"{prefix}{box}{_compact(label)}": box
           for box, labels in boxes.items() for label in labels for prefix in ("", "box")}
    for form, boxes in _BOX_LABELS.items()
}

_PDF_IDENTITIES = {
    "W-2": (
        ("d Control number",), ("b Employer's FED ID number", "b Employer identification number (EIN)"),
        ("a Employee's SSA number", "a Employee's social security number"),
        ("c Employer's name, address, and ZIP code",),
        ("e/f Employee's name, address, and ZIP code",),
    ),
    "1099-NEC": (
        ("For calendar year",), ("Payer's TIN",), ("Recipient's TIN",),
        ("Payer's name",), ("Recipient's name",), ("Account number (see instructions)",),
    ),
    "1099-K": (
        ("For calendar year",), ("Filer's TIN",), ("Payee's TIN",),
        ("Filer's name",), ("Payee's name",), ("Account number (see instructions)",),
    ),
}


def _canonical_form(reference: str) -> str:
    form = re.sub(r"[\s-]", "", reference).upper()
    if form.startswith("W"):
        return "W-2c" if form == "W2C" else f"W-{form[1:]}"
    return f"{form[:4]}-{form[4:]}" if len(form) > 4 else form


def _is_heading(text: str, reference: re.Match[str], form: str) -> bool:
    title = _FORM_TITLES.get(form)
    prefix = text[:reference.start()].strip(" :|")
    if prefix not in ("", "form") and not (title and prefix in (title, f"{title} form")):
        return False
    suffix = text[reference.end():].strip()
    if title and suffix.startswith(title):
        suffix = suffix[len(title):].strip()
    if not suffix or re.fullmatch(r"\(rev\.? [^()]+\)|20[0-9]{2}", suffix):
        return True
    # A colon can identify the issuer on a text copy. Request phrases are not issuer names.
    issuer = suffix[1:].strip() if suffix.startswith(":") else ""
    return suffix == ":" or bool(
        issuer and issuer[0].isalpha() and len(issuer) <= 120
        and not _CORRESPONDENCE.match(issuer)
        and not re.search(r"\b(?:required|attach(?:ed|ment)?|missing|needed|requested|example|sample|instructions)\b", issuer)
    )


def _has_form_detail(lines: list[str], form: str, heading: int) -> bool:
    title = _FORM_TITLES.get(form)
    if title and title in _matching_text(lines[heading - 1]):
        return True
    for number, raw in enumerate(lines, 1):
        if number != heading:
            text = _matching_text(raw)
            if _IDENTITY.match(text) or (text.startswith("box ") and _BOX_LINE.fullmatch(text)):
                return True
    return False


def _result(status: str, category: str | None, forms: list[str], message: str, facts: list[dict] | None = None) -> dict:
    return {"status": status, "category": category, "forms": forms, "facts": [] if facts is None else facts,
            "message": message}


def _normalize_amount(amount: str) -> str:
    value = amount.replace("$", "").replace(",", "").replace(" ", "")
    if value.startswith("("):
        value = f"-{value[1:-1]}"
    return format(Decimal(value), ".2f")


def _amount_fact(form: str, box: str, amount: str, page: int | None, line: int, quote: str) -> dict:
    return {
        "label": f"Box {box} — {_BOX_LABELS[form][box][0]}",
        "value": _normalize_amount(amount),
        "page": page,
        "line": line,
        "quote": quote,
    }


def _source_facts(lines: list[str], form: str, page: int | None) -> tuple[list[dict], bool, bool]:
    labels_by_box = _LABEL_KEYS[form]
    seen: Counter[str] = Counter()
    candidates = {}
    ambiguous = False
    for number, text in enumerate(lines, 1):
        row = _BOX_LINE.fullmatch(_matching_text(text))
        if not row:
            continue
        explicit, box, body = row.groups()
        labels = labels_by_box.get(box)
        if not labels:
            continue
        body_key = _label_key(body)
        if not explicit and not any(body_key == label or body_key.startswith(f"{label} ") for label in labels):
            continue
        seen[box] += 1
        if not body_key or body_key in labels or body.strip() == "$":
            continue  # A blank printed caption has no documentary amount.
        amount = _MONEY.search(body)
        label = _label_key(body[:amount.start()]) if amount else ""
        if not amount or (label not in labels and (label or not explicit)) or len(text) > MAX_QUOTE_CHARS:
            ambiguous = True
            continue
        candidates[box] = _amount_fact(form, box, amount.group(), page, number, text)
    return [fact for box, fact in candidates.items() if seen[box] == 1], ambiguous, any(n > 1 for n in seen.values())


def _marks_form(cell: PDFCell, form: str) -> bool:
    text = _matching_text(cell.text)
    return (any(_OMB_FORMS.get(code) == form for code in _OMB.findall(text))
            and any(_canonical_form(match.group()) == form for match in _FORM_REFERENCE.finditer(text)))


def _copy_identity(cells: tuple[PDFCell, ...], marker: PDFCell, form: str) -> tuple[str, ...] | None:
    # Require identifiers and the form instance, not display labels or amounts.
    fields: dict[str, list[str]] = {}
    for cell in cells:
        first, _, rest = cell.text.partition("\n")
        if rest:
            fields.setdefault(_compact(first), []).append(_matching_text(rest))
    values = []
    for labels in _PDF_IDENTITIES[form]:
        matches = [value for label in labels for value in fields.get(_compact(label), ())]
        if len(matches) != 1 or not matches[0]:
            return None
        values.append(matches[0])
    if form == "W-2":
        # The year can wrap within the W-2 title cell.
        years = set(re.findall(r"(?<!\d)(20\d)\s*(\d)(?!\d)", _matching_text(marker.text)))
        if len(years) != 1:
            return None
        values.append("".join(years.pop()))
    elif not re.fullmatch(r"20\d{2}", values[0]):
        return None
    if any(not re.fullmatch(r"[0-9x*]{5}\d{4}", value.replace("-", "").replace(" ", ""))
           for value in values[1:3]):
        return None
    return tuple(values)


def _amount_line(lines: list[dict], rect: tuple[float, float, float, float], amount: str) -> int | None:
    left, top, width, height = rect
    number = re.search(r"[0-9][0-9,.]*", amount)
    assert number is not None
    literal = re.compile(rf"(?<![\d.,]){re.escape(number.group())}(?![\d.,])")
    matches = []
    for index, line in enumerate(lines, 1):
        x, y, w, h = line["rect"]
        if x < left + width and x + w > left and y < top + height and y + h > top:
            if literal.search(_matching_text(line["text"])):
                matches.append(index)
    return matches[0] if len(matches) == 1 else None


def _copy_facts(form: str, page: dict, cells: tuple[PDFCell, ...]) -> tuple[dict[str, dict], set[str], dict[str, str]]:
    """Identify numbered cells, allowing each box at most one literal amount."""
    captions = _CELL_CAPTIONS[form]
    seen: set[str] = set()
    facts: dict[str, dict] = {}
    ambiguous = set()
    observed: dict[str, str] = {}
    for cell in cells:
        text = _matching_text(cell.text)
        amount = _MONEY.search(text)
        caption = _compact(text[:amount.start()] if amount else text)
        box = captions.get(caption)
        if box is None:
            # An otherwise recognized caption followed by an unparseable value
            # must not silently count as a blank or confirm a different copy.
            box = next((box for label, box in captions.items() if caption.startswith(label)), None)
            if box is not None:
                ambiguous.add(box)
            continue
        if box in seen:
            ambiguous.add(box)
            continue
        seen.add(box)
        if not amount:
            continue
        amount_text = amount.group()
        observed[box] = _normalize_amount(amount_text)
        quote, rect = cell.quote, cell.rect
        if rect is None or quote is None or len(quote) > MAX_QUOTE_CHARS:
            continue
        if quote != cell.text:
            quoted_amount = _MONEY.search(_matching_text(quote))
            if not quoted_amount or quoted_amount.group().replace("$", "").replace(" ", "") != (
                amount_text.replace("$", "").replace(" ", "")
            ):
                ambiguous.add(box)
                continue
        line = _amount_line(page["lines"], rect, amount_text)
        if line is None:
            continue
        facts[box] = _amount_fact(form, box, amount_text, page["page"], line, quote)
        facts[box]["rect"] = rect
    return facts, ambiguous, observed


def _pdf_source_facts(content: bytes, pages: list[dict], form: str,
                      form_pages: dict[int, int]) -> tuple[list[dict], str | None]:
    copies = []
    grounded_copies: Counter[int] = Counter()
    for page, cells in ruled_tables(content, set(form_pages)):
        markers = [cell for cell in cells if _marks_form(cell, form)]
        if not markers:
            continue
        if len(markers) != 1:
            return [], "The original has overlapping or inseparable form copies. Review the original."
        grounded_copies[page] += 1
        facts, ambiguous, observed = _copy_facts(form, pages[page - 1], cells)
        copies.append((_copy_identity(cells, markers[0], form), facts, ambiguous, observed))
    if dict(grounded_copies) != form_pages:
        return [], "The form was recognized, but its boxes could not be located reliably. Review the original."
    if len(copies) > 1:
        identity = copies[0][0]
        if identity is None or any(other != identity for other, _, _, _ in copies[1:]):
            return [], "The form copies could not be identified as the same source record. Review the original."

    facts = []
    disputed = []
    for box in _BOX_LABELS[form]:
        amounts = [observed.get(box) for _, _, _, observed in copies]
        if any(box in ambiguous for _, _, ambiguous, _ in copies):
            disputed.append(box)
        elif any(amount is not None for amount in amounts):
            citation = next((grounded[box] for _, grounded, _, _ in copies if box in grounded), None)
            if citation is None or any(amount != citation["value"] for amount in amounts):
                disputed.append(box)
            else:
                facts.append(citation)
    if disputed:
        boxes = ", ".join(disputed)
        return facts, f"Ambiguous or disagreeing amounts in boxes {boxes}. Review the original."
    if not facts:
        return [], "No supported box had a clearly grounded amount. Review the original."
    return facts, None


def analyze_document(content: bytes, media_type: str) -> dict:
    """Return local filing status and documentary amounts; never assign or update a return."""
    try:
        pages = source_pages(content, media_type)
    except DocumentError as error:
        if error.detail.startswith("This PDF has no selectable text."):
            return _result("needs_review", "Uncategorized", [], error.detail)
        return _result("failed", None, [], error.detail)

    references: dict[str, None] = {}
    official_pages: dict[str, dict[int, int]] = {}
    headings = []
    header = []
    for page in pages:
        lines = [_matching_text(line["text"]) for line in page["lines"]]
        page_forms: set[str] = set()
        omb_forms: Counter[str] = Counter()
        for number, text in enumerate(lines, 1):
            omb_forms.update(_OMB_FORMS[code] for code in _OMB.findall(text) if code in _OMB_FORMS)
            if text and len(header) < 12:
                header.append(text)
            for match in _FORM_REFERENCE.finditer(text):
                form = _canonical_form(match.group())
                references[form] = None
                page_forms.add(form)
                if _is_heading(text, match, form):
                    headings.append((form, page, number))
        if media_type == "application/pdf":
            official_on_page = page_forms & omb_forms.keys()
            if any(_W2_EARNINGS.match(line) for line in lines[:12]) and (
                any(re.search(r"\bemployer'?s?\s*(?:name|fed\s*id)", line) for line in lines)
                and any(re.match(r"1\s+wages", line) for line in lines)
            ):
                official_on_page.add("W-2")
            for form in official_on_page:
                official_pages.setdefault(form, {})[page["page"]] = max(1, omb_forms[form])

    mentioned = list(references)
    if any(_NOTICE.match(line) for line in header):
        return _result("filed", "Tax notices", mentioned, "Organized locally as a tax notice. No box amounts were extracted.")
    if any(_CORRESPONDENCE.match(line) for line in header):
        return _result("filed", "Intake correspondence", mentioned,
                       "Organized locally as intake correspondence. Form mentions are not source data.")
    if official_pages and not any(_NON_SOURCE.match(line) for line in header):
        forms = [form for form in mentioned if form in official_pages]
        if len(forms) != 1 or any(form == "W-2c" for form, *_ in headings):
            return _result("needs_review", "Tax forms", mentioned,
                           "Multiple form types or a correction were found. Review the original.")
        form = forms[0]
        try:
            facts, issue = _pdf_source_facts(content, pages, form, official_pages[form])
        except DocumentError:
            return _result("needs_review", "Tax forms", forms,
                           "The form geometry could not be read. Review the original.")
        return _result("needs_review" if issue else "filed", "Tax forms", forms, issue or
                       "Organized locally as a tax form. Check the cited boxes against the original.", facts)
    if not headings or any(_NON_SOURCE.match(line) for line in header):
        return _result("needs_review", "Uncategorized", mentioned,
                       "Form mentions do not identify an actual tax form. Check the original."
                       if mentioned else "No recognizable document type was found. Check the original.")

    forms = list(dict.fromkeys(form for form, *_ in headings))
    if len(headings) > 1:
        return _result("needs_review", "Tax forms", forms,
                       "Multiple tax-form headings or copies were found. Review the original to identify each form.")

    form, page, heading_line = headings[0]
    lines = [line["text"] for line in page["lines"]]
    facts, ambiguous, duplicates = ([], False, False)
    if form in _BOX_LABELS and media_type == "text/plain":
        facts, ambiguous, duplicates = _source_facts(lines, form, None)
    if not (facts or ambiguous or duplicates or _has_form_detail(lines, form, heading_line)):
        return _result("needs_review", "Uncategorized", mentioned,
                       "A form heading was found without enough source-form detail. Check the original.")
    if form not in _BOX_LABELS:
        return _result("needs_review", "Tax forms", forms, f"{form} was identified. Review its boxes in the original.")
    if media_type == "application/pdf":
        return _result("needs_review", "Tax forms", forms,
                       "The form was recognized, but no supported PDF box layout could be verified. Review the original.")
    if duplicates or ambiguous:
        issue = "Repeated box labels" if duplicates else "Ambiguous box amounts"
        if duplicates and ambiguous:
            issue = "Repeated box labels and ambiguous box amounts"
        return _result("needs_review", "Tax forms", forms, f"{issue} were found. Check the original.", facts)
    if not facts:
        return _result("needs_review", "Tax forms", forms,
                       "No supported box had one clearly labeled amount on the same source line. Check the original.")
    return _result("filed", "Tax forms", forms, "Organized locally as a tax form. Check cited amounts against the original.", facts)
