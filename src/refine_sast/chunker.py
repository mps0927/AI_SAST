from __future__ import annotations

from .hashing import content_hash, stable_id
from .models import Chunk, FunctionRegion
from .risk import tag_calls
from .tokens import TokenEstimator


CHUNKER_VERSION = "semantic-chunker-v3"


class SemanticChunker:
    def __init__(self, estimator: TokenEstimator, max_tokens: int = 1800):
        self.estimator = estimator
        self.max_tokens = max_tokens

    def chunk_function(
        self,
        path: str,
        scope: str,
        quality: str,
        data: bytes,
        function: FunctionRegion,
    ) -> list[Chunk]:
        source = data[function.start_byte : function.end_byte]
        if self.estimator.estimate_bytes(source) <= self.max_tokens:
            return [self._make_chunk(path, scope, quality, data, function, function.start_byte, function.end_byte, 1, 1, "function")]

        ranges = self._semantic_ranges(data, function)
        if not ranges:
            return [
                self._make_chunk(
                    path,
                    scope,
                    quality,
                    data,
                    function,
                    function.start_byte,
                    function.end_byte,
                    1,
                    1,
                    "function",
                    budget_exception="no safe semantic boundary for oversized function",
                )
            ]

        signature_end = ranges[0][0]
        signature = data[function.start_byte:signature_end]
        signature_tokens = self.estimator.estimate_bytes(signature)
        groups: list[tuple[int, int, str | None]] = []
        current_start: int | None = None
        current_end: int | None = None
        current_tokens = signature_tokens
        for start, end, segment_kind in ranges:
            segment_tokens = self.estimator.estimate_bytes(data[start:end])
            if segment_tokens + signature_tokens > self.max_tokens:
                if current_start is not None and current_end is not None:
                    groups.append((current_start, current_end, None))
                    current_start = current_end = None
                    current_tokens = signature_tokens
                if segment_kind.startswith("preproc_"):
                    groups.append((start, end, "indivisible preprocessor block exceeds token budget"))
                    continue
                subranges = self._lexical_safe_ranges(data, start, end)
                packed = self._pack_ranges(data, subranges, signature_tokens)
                groups.extend(packed)
                continue
            if current_start is not None and current_tokens + segment_tokens > self.max_tokens:
                groups.append((current_start, current_end or current_start, None))
                current_start = current_end = None
                current_tokens = signature_tokens
            if current_start is None:
                current_start = start
            current_end = end
            current_tokens += segment_tokens
        if current_start is not None and current_end is not None:
            groups.append((current_start, current_end, None))

        chunks = [
            self._make_chunk(
                path,
                scope,
                quality,
                data,
                function,
                start,
                end,
                index,
                len(groups),
                "function-part",
                context_header=signature.decode("utf-8", errors="replace").strip(),
                budget_exception=exception,
            )
            for index, (start, end, exception) in enumerate(groups, 1)
        ]
        return chunks

    def _make_chunk(
        self,
        path: str,
        scope: str,
        quality: str,
        data: bytes,
        function: FunctionRegion,
        start: int,
        end: int,
        part: int,
        count: int,
        kind: str,
        context_header: str = "",
        budget_exception: str | None = None,
    ) -> Chunk:
        raw = data[start:end]
        start_line = data.count(b"\n", 0, start) + 1
        end_line = data.count(b"\n", 0, max(start, end - 1)) + 1
        risk_tags, evidence = tag_calls(function.calls, start_line, end_line)
        calls = sorted(
            {
                str(item["name"])
                for item in function.calls
                if start_line <= int(item["line"]) <= end_line
            }
        )
        estimated = self.estimator.estimate_bytes(raw)
        if context_header:
            estimated += self.estimator.estimate_text(context_header)
        identifier = stable_id(
            "CHK",
            {
                "version": CHUNKER_VERSION,
                "path": path,
                "symbol": function.symbol,
                "part": part,
                "start_byte": start,
                "raw_hash": content_hash(raw),
            },
        )
        return Chunk(
            chunk_id=identifier,
            path=path,
            symbol=function.symbol if count == 1 else f"{function.symbol}#part{part}",
            kind=kind,
            start_line=start_line,
            end_line=end_line,
            start_byte=start,
            end_byte=end,
            content_hash=content_hash(raw),
            scope=scope,
            estimated_tokens=estimated,
            calls=calls,
            referenced_types=function.referenced_types,
            referenced_macros=function.referenced_macros,
            risk_tags=risk_tags,
            risk_evidence=evidence,
            parse_quality=quality,
            complexity=function.complexity,
            guard_count=function.guard_count,
            pointer_operations=function.pointer_operations,
            parent_symbol=function.symbol,
            part_index=part,
            part_count=count,
            context_header=context_header,
            budget_exception=budget_exception,
        )

    @staticmethod
    def _semantic_ranges(data: bytes, function: FunctionRegion) -> list[tuple[int, int, str]]:
        valid = []
        for item in function.safe_segments:
            start, end = item["start_byte"], item["end_byte"]
            if function.start_byte <= start < end <= function.end_byte:
                valid.append((start, end, str(item.get("kind", "statement"))))
        return sorted(valid)

    def _pack_ranges(
        self, data: bytes, ranges: list[tuple[int, int]], overhead: int
    ) -> list[tuple[int, int, str | None]]:
        if not ranges:
            return []
        result: list[tuple[int, int, str | None]] = []
        start = ranges[0][0]
        end = start
        tokens = overhead
        for range_start, range_end in ranges:
            current = self.estimator.estimate_bytes(data[range_start:range_end])
            if tokens + current > self.max_tokens and end > start:
                result.append((start, end, None))
                start = range_start
                tokens = overhead
            end = range_end
            tokens += current
            if tokens > self.max_tokens:
                result.append((start, end, "indivisible syntax region exceeds token budget"))
                start = end
                tokens = overhead
        if end > start:
            result.append((start, end, None))
        return result

    @staticmethod
    def _lexical_safe_ranges(data: bytes, start: int, end: int) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        state = "code"
        escaped = False
        segment_start = start
        line_start = True
        index = start
        while index < end:
            char = chr(data[index])
            nxt = chr(data[index + 1]) if index + 1 < end else ""
            if state == "code":
                if line_start and char in " \t":
                    index += 1
                    continue
                if line_start and char == "#":
                    state = "preproc"
                elif char == "/" and nxt == "/":
                    state = "line-comment"
                    index += 1
                elif char == "/" and nxt == "*":
                    state = "block-comment"
                    index += 1
                elif char == '"':
                    state = "string"
                elif char == "'":
                    state = "char"
                elif char in ";}":
                    ranges.append((segment_start, index + 1))
                    segment_start = index + 1
            elif state in {"string", "char"}:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif state == "string" and char == '"':
                    state = "code"
                elif state == "char" and char == "'":
                    state = "code"
            elif state == "line-comment" and char == "\n":
                state = "code"
            elif state == "preproc" and char == "\n" and (index == start or data[index - 1] != ord("\\")):
                state = "code"
                ranges.append((segment_start, index + 1))
                segment_start = index + 1
            elif state == "block-comment" and char == "*" and nxt == "/":
                state = "code"
                index += 1
            line_start = char == "\n"
            index += 1
        if segment_start < end:
            ranges.append((segment_start, end))
        return [(a, b) for a, b in ranges if data[a:b].strip()]
