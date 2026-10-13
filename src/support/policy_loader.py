# clin_longsum - physician feedback loop
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of version 3 of the GNU Affero General Public License as published
# by the Free Software Foundation. This program is distributed WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

"""
policy_loader.py
================
Loader and validator for policy/triage_policy.yaml.

NAMED `policy_loader`, NOT `policy`: a module `policy.py` sitting beside the
directory `policy/` is shadowed by it, because the import machinery resolves
directories as namespace packages before same-named file modules. The clash is
silent - imports succeed and resolve to an empty package - so the rename is
load-bearing, not cosmetic.

The YAML is the GOVERNANCE OBJECT: it holds parameters and human-readable
intent, and is versioned and clinically signed off exactly like
retention_taxonomy.py. This module holds no policy of its own - it parses,
validates, and exposes lookups.

WHY VALIDATION IS STRICT
------------------------
The split between "parameters in YAML" and "logic in triage.py" only works if
the two halves cannot drift. So:

  - a rule id in the YAML with no implementation in triage.py is a hard error;
  - an implementation with no YAML entry is a hard error;
  - a symptom routing to an unknown role, or a state transition naming an
    unknown state, is a hard error.

All four fail at LOAD, not at first use. A policy file that would misroute a
physician's report must never reach the point of triaging one. `validate()` is
called by triage.py at import; import the package and a bad policy raises
immediately.

Loading is cached on (path, mtime), so a running API picks up an edited policy
without a restart while still not re-parsing on every report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

POLICY_PATH = Path(__file__).resolve().parent / "policy" / "triage_policy.yaml"

VALID_ROLES = {"clinician", "engineer", "admin"}
VALID_DEFECT_CLASSES = {
    "clinical_taxonomy", "engineering_routing", "prompt_wording", "documentation",
}


class PolicyError(RuntimeError):
    """Malformed or internally inconsistent policy. Always fatal at load."""


@dataclass(frozen=True)
class Symptom:
    id: str
    physician_label: str
    owner_roles: tuple[str, ...]
    defect_class_hint: Optional[str]
    notes: Optional[str]
    sort_order: int


@dataclass(frozen=True)
class UrgencyRule:
    id: str
    description: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Policy:
    version: str
    symptoms: dict[str, Symptom]
    urgency_rules: tuple[UrgencyRule, ...]
    s1_subclass: dict[str, dict[str, Any]]
    states: dict[str, tuple[str, ...]]          # state -> allowed next states
    terminal_states: frozenset[str]
    dispositions: dict[str, dict[str, Any]]
    dedup: dict[str, Any]
    notification: dict[str, Any]
    source_path: Path

    # ── lookups ────────────────────────────────────────────────────────────────

    def symptom(self, symptom_id: str) -> Symptom:
        try:
            return self.symptoms[symptom_id]
        except KeyError:
            raise PolicyError(
                f"unknown symptom '{symptom_id}' under {self.version}. Known: "
                f"{sorted(self.symptoms)}") from None

    def rule(self, rule_id: str) -> UrgencyRule:
        for r in self.urgency_rules:
            if r.id == rule_id:
                return r
        raise PolicyError(f"unknown urgency rule '{rule_id}' under {self.version}")

    def can_transition(self, from_state: str, to_state: str) -> bool:
        return to_state in self.states.get(from_state, ())

    def defect_class_for_s1(self, subclass: str) -> Optional[str]:
        return (self.s1_subclass.get(subclass) or {}).get("defect_class")

    def owner_role_for_disposition(self, disposition: str) -> Optional[str]:
        return (self.dispositions.get(disposition) or {}).get("owner_role")


# ── loading ────────────────────────────────────────────────────────────────────

_cache: dict[tuple[str, float], Policy] = {}


def load(path: Path | str | None = None, *, refresh: bool = False) -> Policy:
    """Parse and validate the policy. Cached on (path, mtime)."""
    p = Path(path) if path else POLICY_PATH
    if not p.exists():
        raise PolicyError(f"policy file not found: {p}")
    key = (str(p), p.stat().st_mtime)
    if refresh:
        _cache.pop(key, None)
    if key in _cache:
        return _cache[key]

    import yaml
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    pol = _build(raw, p)
    _cache[key] = pol
    return pol


def _build(raw: dict, source: Path) -> Policy:
    if not isinstance(raw, dict) or "version" not in raw:
        raise PolicyError(f"{source}: missing top-level 'version'")

    symptoms: dict[str, Symptom] = {}
    for i, s in enumerate(raw.get("symptoms") or []):
        sid = s.get("id")
        if not sid:
            raise PolicyError(f"{source}: symptom #{i} has no id")
        if sid in symptoms:
            raise PolicyError(f"{source}: duplicate symptom id '{sid}'")
        roles = tuple(s.get("owner_roles") or ())
        bad = set(roles) - VALID_ROLES
        if bad:
            raise PolicyError(f"{source}: symptom '{sid}' routes to unknown role(s) {sorted(bad)}")
        if not roles:
            raise PolicyError(f"{source}: symptom '{sid}' has no owner_roles - it would be unroutable")
        hint = s.get("defect_class_hint")
        if hint is not None and hint not in VALID_DEFECT_CLASSES:
            raise PolicyError(f"{source}: symptom '{sid}' has unknown defect_class_hint '{hint}'")
        symptoms[sid] = Symptom(sid, s.get("physician_label", sid), roles, hint,
                                s.get("notes"), i)
    if not symptoms:
        raise PolicyError(f"{source}: no symptoms defined")

    rules = tuple(
        UrgencyRule(r["id"], r.get("description", ""), dict(r.get("params") or {}))
        for r in (raw.get("urgency") or {}).get("rules") or []
    )
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise PolicyError(f"{source}: duplicate urgency rule id(s)")

    # Cross-check every symptom named inside a rule actually exists. A typo here
    # would silently disable the rule rather than raising, which is the worst
    # possible failure for a safety gate.
    for r in rules:
        for sid in r.params.get("symptoms") or ():
            if sid not in symptoms:
                raise PolicyError(
                    f"{source}: urgency rule '{r.id}' names unknown symptom '{sid}'")

    states: dict[str, tuple[str, ...]] = {}
    terminal: set[str] = set()
    for st in raw.get("states") or []:
        sid = st["id"]
        states[sid] = tuple(st.get("to") or ())
        if st.get("terminal"):
            terminal.add(sid)
    if not states:
        raise PolicyError(f"{source}: no states defined")
    for sid, nxt in states.items():
        unknown = set(nxt) - set(states)
        if unknown:
            raise PolicyError(
                f"{source}: state '{sid}' transitions to unknown state(s) {sorted(unknown)}")
    if "new" not in states:
        raise PolicyError(f"{source}: no 'new' state - cards would have nowhere to start")
    if not terminal:
        raise PolicyError(f"{source}: no terminal state - cards could never close")

    dispositions = {d["id"]: d for d in (raw.get("dispositions") or [])}
    if not dispositions:
        raise PolicyError(f"{source}: no dispositions defined")
    for did, d in dispositions.items():
        role = d.get("owner_role")
        if role and role not in VALID_ROLES:
            raise PolicyError(f"{source}: disposition '{did}' names unknown role '{role}'")

    s1 = {}
    for entry in raw.get("s1_subclass") or []:
        dc = entry.get("defect_class")
        if dc and dc not in VALID_DEFECT_CLASSES:
            raise PolicyError(
                f"{source}: s1_subclass '{entry.get('id')}' has unknown defect_class '{dc}'")
        s1[entry["id"]] = entry

    return Policy(
        version=raw["version"],
        symptoms=symptoms,
        urgency_rules=rules,
        s1_subclass=s1,
        states=states,
        terminal_states=frozenset(terminal),
        dispositions=dispositions,
        dedup=dict(raw.get("dedup") or {}),
        notification=dict(raw.get("notification") or {}),
        source_path=source,
    )


def validate_against_implementations(pol: Policy, implemented: set[str]) -> None:
    """Assert the YAML rule set and the Python rule set are the same set.

    Called by triage.py at import. Either direction of drift is fatal:
      - declared but unimplemented -> the policy claims a gate that never fires;
      - implemented but undeclared -> a gate fires that no one signed off on.
    The second is the more dangerous and the easier to introduce.
    """
    declared = {r.id for r in pol.urgency_rules}
    missing = declared - implemented
    extra = implemented - declared
    problems = []
    if missing:
        problems.append(f"declared in {pol.source_path.name} but not implemented: {sorted(missing)}")
    if extra:
        problems.append(f"implemented but not declared in {pol.source_path.name}: {sorted(extra)}")
    if problems:
        raise PolicyError(f"triage policy {pol.version} is inconsistent - " + "; ".join(problems))


def summary(pol: Policy) -> str:
    return (f"{pol.version}: {len(pol.symptoms)} symptoms, "
            f"{len(pol.urgency_rules)} urgency rules, {len(pol.states)} states, "
            f"{len(pol.dispositions)} dispositions")


if __name__ == "__main__":
    print(summary(load()))
