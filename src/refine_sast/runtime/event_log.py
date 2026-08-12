from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..stage3_schemas import AgentName, MessageType, MockUsage


FORBIDDEN_LOG_KEYS = {
    "source", "source_code", "content", "raw", "raw_code", "prompt", "api_key",
    "secret", "environment", "env",
}


def _assert_log_safe(value: Any, path: str = "event") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_LOG_KEYS:
                raise ValueError(f"forbidden log field: {path}.{key}")
            _assert_log_safe(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_log_safe(nested, f"{path}[{index}]")


class EventLogger:
    def __init__(self, run_id: str, batch_id: str):
        self.run_id = run_id
        self.batch_id = batch_id
        self.events: list[dict[str, Any]] = []

    def record(
        self,
        *,
        event_type: str,
        agent: AgentName | None,
        from_state: str,
        to_state: str,
        prompt_version: str | None = None,
        message_type: MessageType | None = None,
        input_evidence_ids: list[str] | None = None,
        output_message_ids: list[str] | None = None,
        usage: MockUsage | None = None,
        detail_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        sequence = len(self.events) + 1
        event = {
            "event_id": f"EVT-{sequence:04d}",
            "sequence": sequence,
            "run_id": self.run_id,
            "batch_id": self.batch_id,
            "event_type": event_type,
            "agent": agent.value if agent else None,
            "from_state": from_state,
            "to_state": to_state,
            "prompt_version": prompt_version,
            "message_type": message_type.value if message_type else None,
            "input_evidence_ids": input_evidence_ids or [],
            "output_message_ids": output_message_ids or [],
            "mock_token_usage": usage.model_dump(mode="json") if usage else None,
            "detail_codes": detail_codes or [],
        }
        _assert_log_safe(event)
        self.events.append(event)
        return event

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for item in self.events
            ),
            encoding="utf-8",
        )
