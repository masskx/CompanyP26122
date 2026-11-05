"""Equipment master data loaded from a JSON file.

Placeholder until the MES main backend exposes its equipment master-data API; at that
point an HTTP-backed implementation of `EquipmentCatalog` replaces this one.
"""

import json
from pathlib import Path

from pydantic import TypeAdapter

from industrial_ai.schemas.equipment import Equipment

_EQUIPMENT_LIST = TypeAdapter(list[Equipment])


class StaticEquipmentCatalog:
    def __init__(self, equipment: list[Equipment]) -> None:
        self._items: dict[tuple[str, str], Equipment] = {}
        for item in equipment:
            key = (item.site_id, item.equipment_id)
            if key in self._items:
                raise ValueError(f"duplicate equipment {item.site_id}/{item.equipment_id}")
            self._items[key] = item

    @classmethod
    def from_file(cls, path: Path) -> "StaticEquipmentCatalog":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(_EQUIPMENT_LIST.validate_python(raw.get("equipment", [])))

    async def list_equipment(self, site_id: str, category: str | None = None) -> list[Equipment]:
        return [
            e for (s, _), e in sorted(self._items.items())
            if s == site_id and (category is None or e.category == category)
        ]

    async def get(self, site_id: str, equipment_id: str) -> Equipment | None:
        return self._items.get((site_id, equipment_id))
