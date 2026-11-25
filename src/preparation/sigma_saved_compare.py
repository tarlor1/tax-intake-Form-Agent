"""Compare two verified, complete Sigma saved-field inventories without printing tax values."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: Path) -> tuple[int, dict[tuple[str, str], dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("complete") is not True or data.get("reopened") is not True:
        raise ValueError("Both inventories must be complete and captured after reopening.")

    year = data.get("taxYear")
    forms = data.get("forms")
    if type(year) is not int or not isinstance(forms, dict) or not forms:
        raise ValueError("The inventory needs a tax year and identified forms.")

    fields = {}
    for form, controls in forms.items():
        if not form or not isinstance(controls, list) or not controls:
            raise ValueError("Every form needs an identity and a saved field inventory.")
        for control in controls:
            binding = control.get("binding")
            kind = control.get("kind")
            visible = control.get("visible")
            key = (form, binding)
            if (
                not isinstance(binding, str) or not binding or key in fields
                or not isinstance(kind, str) or not kind or type(visible) is not bool
                or "value" not in control
            ):
                raise ValueError("A saved field is ambiguous, unidentified, or missing its value.")
            fields[key] = {"kind": kind, "visible": visible, "value": control["value"]}
    return year, fields


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    try:
        baseline_year, baseline = _load(args.baseline)
        candidate_year, candidate = _load(args.candidate)
        if baseline_year != candidate_year:
            raise ValueError("The inventories have different tax years.")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        parser.exit(2, f"Comparison blocked: {exc}\n")

    differences = []
    for form, binding in sorted(baseline.keys() | candidate.keys()):
        key = (form, binding)
        if key not in candidate:
            status = "missing"
        elif key not in baseline:
            status = "unexpected"
        elif json.dumps(baseline[key], sort_keys=True) != json.dumps(candidate[key], sort_keys=True):
            status = "different"
        else:
            continue
        differences.append({"form": form, "binding": binding, "status": status})

    print(json.dumps({"taxYear": baseline_year, "comparedFields": len(baseline.keys() & candidate.keys()),
                      "differences": differences}, indent=2))
    if differences:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
