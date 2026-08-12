from __future__ import annotations

from collections import defaultdict
from typing import Any

from .models import Chunk


RISK_APIS: dict[str, set[str]] = {
    "command-process": {"system", "popen", "execl", "execle", "execlp", "execv", "execve", "execvp"},
    "unbounded-string": {"strcpy", "strcat", "sprintf", "vsprintf", "gets", "sscanf", "scanf", "fscanf"},
    "raw-memory": {"memcpy", "memmove", "memset", "memcmp", "bcopy"},
    "network": {"socket", "accept", "recv", "recvfrom", "send", "sendto", "bind", "listen", "connect"},
    "file-path": {"open", "fopen", "freopen", "read", "write", "fread", "fwrite", "unlink", "remove", "rename"},
    "allocation": {"malloc", "calloc", "realloc", "free", "strdup", "alloca"},
    "integer-conversion": {"atoi", "atol", "atoll", "strtol", "strtoul", "strtoll", "strtoull"},
    "bounded-string": {"snprintf", "vsnprintf", "strncpy", "strncat"},
}

API_TO_TAG = {api: tag for tag, apis in RISK_APIS.items() for api in apis}

RISK_WEIGHTS = {
    "command-process": 12.0,
    "unbounded-string": 8.0,
    "raw-memory": 5.0,
    "network": 6.0,
    "file-path": 4.0,
    "allocation": 3.0,
    "integer-conversion": 3.5,
    "bounded-string": 2.0,
}


def tag_calls(calls: list[dict[str, Any]], start_line: int, end_line: int) -> tuple[list[str], list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    for call in calls:
        if not (start_line <= int(call["line"]) <= end_line):
            continue
        name = str(call["name"]).split("::")[-1]
        tag = API_TO_TAG.get(name)
        if tag:
            evidence.append({"api": name, "tag": tag, "line": int(call["line"])})
    evidence.sort(key=lambda item: (item["line"], item["api"], item["tag"]))
    return sorted({item["tag"] for item in evidence}), evidence


class RiskRanker:
    version = "risk-v1"

    def score(self, chunk: Chunk) -> tuple[float, dict[str, float]]:
        counts: dict[str, int] = defaultdict(int)
        for item in chunk.risk_evidence:
            counts[item["tag"]] += 1
        sink = sum(RISK_WEIGHTS[tag] * min(count, 4) for tag, count in counts.items())
        path = chunk.path.lower()
        call_set = set(chunk.calls)
        network_input = 1.0 if counts["network"] or "/rtsp/" in f"/{path}" or "/rtp/" in f"/{path}" else 0.0
        file_input = 1.0 if counts["file-path"] or "/containers/" in f"/{path}" else 0.0
        cli_input = 1.0 if {"getopt", "getopt_long"} & call_set or "main" == chunk.parent_symbol or chunk.symbol == "main" else 0.0
        ipc_input = 1.0 if any(term in path for term in ("vchiq", "mmal", "vmcs", "vcos")) else 0.0
        boundary = min(2.0, network_input + file_input + cli_input + ipc_input) * 4.0
        proximity = 6.0 if boundary and sink else 0.0
        complexity = min(8.0, chunk.complexity * 0.35 + chunk.pointer_operations * 0.2)
        missing_guard = min(4.0, max(0, len(chunk.risk_evidence) - chunk.guard_count) * 0.75)
        scope = 5.0 if chunk.scope == "primary-source" else 0.0
        quality = {"full": 2.0, "partial": 0.5, "degraded": -5.0}.get(chunk.parse_quality, -8.0)
        components = {
            "sink": round(sink, 4),
            "input_boundary": round(boundary, 4),
            "source_sink_proximity": round(proximity, 4),
            "complexity": round(complexity, 4),
            "possible_missing_guard": round(missing_guard, 4),
            "primary_scope": round(scope, 4),
            "parser_quality": round(quality, 4),
        }
        return round(sum(components.values()), 4), components
