from __future__ import annotations

from ..stage3_schemas import ProofObligation


class ProofTable:
    def __init__(self, obligations: list[ProofObligation]):
        self._items = {item.obligation_id: item for item in obligations}
        if len(self._items) != len(obligations):
            raise ValueError("duplicate proof obligation ID")

    def apply(self, updates: list[ProofObligation]) -> None:
        for update in updates:
            if update.obligation_id not in self._items:
                raise ValueError(f"unknown proof obligation: {update.obligation_id}")
            current = self._items[update.obligation_id]
            if update.description_code != current.description_code or update.required != current.required:
                raise ValueError("proof obligation identity changed")
            self._items[update.obligation_id] = update

    def values(self) -> list[ProofObligation]:
        return [self._items[key] for key in sorted(self._items)]
