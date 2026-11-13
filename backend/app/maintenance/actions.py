"""Versioned standard action catalogue (data/maintenance/actions.json).

Findings map to ordered action codes. `candidates()` returns at most 3 distinct actions in
finding order; the narrator may pick which one is primary (Question.txt: "建议动作是模型在
预置好的三种里面输出一种主建议"), but texts always come from the catalogue.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from app.maintenance.schemas import Action, Finding


@dataclass(frozen=True)
class ActionCatalog:
    version: str
    actions: dict[str, str]
    rules: dict[str, list[str]]

    @classmethod
    def load(cls, path: Path) -> "ActionCatalog":
        if not path.is_file():
            raise FileNotFoundError(f"action catalogue not found: {path} "
                                    "(MAINTENANCE_ACTION_CATALOG_PATH)")
        raw = json.loads(path.read_text(encoding="utf-8"))
        catalog = cls(raw["version"], raw["actions"], raw["rules"])
        unknown = {a for codes in catalog.rules.values() for a in codes} - set(catalog.actions)
        if unknown:
            raise ValueError(f"{path}: rules reference unknown actions {sorted(unknown)}")
        return catalog

    def candidates(self, findings: list[Finding], limit: int = 3) -> list[str]:
        out: list[str] = []
        # first the top action of every finding, then the remaining ones, in finding order
        for depth in range(3):
            for f in findings:
                codes = self.rules.get(f.code, [])
                if depth < len(codes) and codes[depth] not in out:
                    out.append(codes[depth])
                if len(out) == limit:
                    return out
        return out

    def build(self, codes: list[str], primary: str | None = None) -> list[Action]:
        if primary in codes:
            codes = [primary, *(c for c in codes if c != primary)]  # type: ignore[list-item]
        return [Action(action_code=c, priority="primary" if i == 0 else "secondary",
                       text=self.actions[c]) for i, c in enumerate(codes)]
