from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from tree_sitter import Language, Node, Parser
import tree_sitter_c
import tree_sitter_cpp

from .models import FunctionRegion, ParseResult


PARSER_VERSION = "tree-sitter-0.26-c0.24.2-cpp0.23.4+fallback-v2"

CONTROL_NODES = {
    "if_statement",
    "for_statement",
    "while_statement",
    "do_statement",
    "switch_statement",
    "conditional_expression",
    "goto_statement",
}
TYPE_NODES = {"type_identifier", "primitive_type", "sized_type_specifier"}


def walk(node: Node) -> Iterable[Node]:
    cursor = node.walk()
    while True:
        yield cursor.node
        if cursor.goto_first_child():
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                return


def node_text(data: bytes, node: Node) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _declarator_name(data: bytes, node: Node | None) -> str:
    if node is None:
        return "<anonymous>"
    candidates: list[str] = []
    for child in walk(node):
        if child.type in {"identifier", "field_identifier", "operator_name", "destructor_name"}:
            candidates.append(node_text(data, child))
    return candidates[0] if candidates else node_text(data, node).split("(", 1)[0].strip()[-120:]


def _call_name(data: bytes, node: Node) -> str:
    function = node.child_by_field_name("function")
    if function is None:
        return "<indirect>"
    text = node_text(data, function).strip()
    text = text.replace(" ", "")
    return text[-160:] if text else "<indirect>"


class TreeSitterBackend:
    version = PARSER_VERSION

    def __init__(self):
        # Keep Language objects alive for at least as long as their native Parser.
        # Some Windows builds do not retain the Python owner strongly enough.
        self._c_language = Language(tree_sitter_c.language())
        self._cpp_language = Language(tree_sitter_cpp.language())
        self._c = Parser(self._c_language)
        self._cpp = Parser(self._cpp_language)

    def parse(self, data: bytes, language: str) -> ParseResult:
        parser = self._cpp if language in {"cpp", "cpp-header"} else self._c
        tree = parser.parse(data)
        root = tree.root_node
        # Capture primitive ranges first, then parse each function slice in an
        # isolated tree.  This avoids a native Node-wrapper lifetime defect in
        # the Windows binding when many sibling nodes are nested-walked.
        descriptors: list[tuple[int, int, int, int]] = []
        errors = 0
        for node in walk(root):
            if node.type == "ERROR" or node.is_missing:
                errors += 1
            if node.type == "function_definition":
                start_byte = node.start_byte
                end_byte = node.end_byte
                descriptors.append(
                    (
                        start_byte,
                        end_byte,
                        data.count(b"\n", 0, start_byte) + 1,
                        data.count(b"\n", 0, max(start_byte, end_byte - 1)) + 1,
                    )
                )

        functions: list[FunctionRegion] = []
        for absolute_start, absolute_end, start_line, end_line in descriptors:
            function_data = data[absolute_start:absolute_end]
            function_tree = parser.parse(function_data)
            function_nodes = list(walk(function_tree.root_node))
            node = next((item for item in function_nodes if item.type == "function_definition"), None)
            if node is None:
                continue
            symbol = _declarator_name(function_data, node.child_by_field_name("declarator"))
            body = node.child_by_field_name("body")
            calls: list[dict[str, object]] = []
            referenced_types: set[str] = set()
            macros: set[str] = set()
            complexity = 1
            guards = 0
            pointers = 0
            for child in walk(node):
                if child.type == "call_expression":
                    calls.append(
                        {
                            "name": _call_name(function_data, child),
                            "line": start_line + function_data.count(b"\n", 0, child.start_byte),
                        }
                    )
                if child.type in TYPE_NODES:
                    referenced_types.add(node_text(function_data, child))
                if child.type in CONTROL_NODES:
                    complexity += 1
                    if child.type in {"if_statement", "conditional_expression"}:
                        guards += 1
                if child.type in {"pointer_declarator", "pointer_expression", "subscript_expression"}:
                    pointers += 1
                if child.type == "identifier":
                    text = node_text(function_data, child)
                    if len(text) > 2 and text.isupper():
                        macros.add(text)
            segments: list[dict[str, object]] = []
            if body is not None:
                for child in body.named_children:
                    child_start = child.start_byte
                    child_end = child.end_byte
                    segments.append(
                        {
                            "start_byte": absolute_start + child_start,
                            "end_byte": absolute_start + child_end,
                            "start_line": start_line + function_data.count(b"\n", 0, child_start),
                            "end_line": start_line + function_data.count(b"\n", 0, max(child_start, child_end - 1)),
                            "kind": child.type,
                        }
                    )
            functions.append(
                FunctionRegion(
                    symbol=symbol,
                    start_byte=absolute_start,
                    end_byte=absolute_end,
                    start_line=start_line,
                    end_line=end_line,
                    calls=sorted(calls, key=lambda item: (int(item["line"]), str(item["name"]))),
                    referenced_types=sorted(referenced_types),
                    referenced_macros=sorted(macros),
                    safe_segments=segments,
                    complexity=complexity,
                    guard_count=guards,
                    pointer_operations=pointers,
                )
            )
        quality = "full" if errors == 0 else ("partial" if functions else "degraded")
        if not functions and errors:
            fallback = BraceAwareFallback().parse(data)
            if fallback.functions:
                fallback.errors += errors
                return fallback
        return ParseResult(quality=quality, errors=errors, backend="tree-sitter", functions=functions)


class BraceAwareFallback:
    """Conservative last-resort extractor that never treats braces in text as code."""

    version = "brace-fallback-v1"
    _signature = re.compile(r"([A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*$", re.DOTALL)

    def parse(self, data: bytes) -> ParseResult:
        text = data.decode("utf-8", errors="replace")
        masked = self._mask_non_code(text)
        line_starts = [0]
        for match in re.finditer("\n", text):
            line_starts.append(match.end())

        def line_of(offset: int) -> int:
            import bisect

            return bisect.bisect_right(line_starts, offset)

        stack: list[int] = []
        functions: list[FunctionRegion] = []
        for index, char in enumerate(masked):
            if char == "{":
                stack.append(index)
            elif char == "}" and stack:
                start = stack.pop()
                if stack:
                    continue
                prefix_start = max(masked.rfind(";", 0, start), masked.rfind("}", 0, start)) + 1
                signature = masked[prefix_start:start]
                match = self._signature.search(signature)
                if not match or match.group(1) in {"if", "for", "while", "switch"}:
                    continue
                region_start = prefix_start + match.start()
                functions.append(
                    FunctionRegion(
                        symbol=match.group(1),
                        start_byte=len(text[:region_start].encode("utf-8")),
                        end_byte=len(text[: index + 1].encode("utf-8")),
                        start_line=line_of(region_start),
                        end_line=line_of(index),
                        safe_segments=[],
                    )
                )
        return ParseResult(quality="degraded", errors=1, backend="brace-fallback", functions=functions)

    @staticmethod
    def _mask_non_code(text: str) -> str:
        result = list(text)
        state = "code"
        index = 0
        line_start = True
        while index < len(text):
            char = text[index]
            nxt = text[index + 1] if index + 1 < len(text) else ""
            if state == "code":
                if line_start and char in " \t":
                    index += 1
                    continue
                if line_start and char == "#":
                    state = "preproc"
                elif char == "/" and nxt == "/":
                    state = "line-comment"
                elif char == "/" and nxt == "*":
                    state = "block-comment"
                elif char == '"':
                    state = "string"
                elif char == "'":
                    state = "char"
            else:
                if char not in "\r\n":
                    result[index] = " "
                if state in {"string", "char"} and char == "\\":
                    if index + 1 < len(text):
                        result[index + 1] = " "
                        index += 2
                        line_start = False
                        continue
                if state == "string" and char == '"':
                    state = "code"
                elif state == "char" and char == "'":
                    state = "code"
                elif state in {"line-comment", "preproc"} and char == "\n":
                    state = "code"
                elif state == "block-comment" and char == "*" and nxt == "/":
                    result[index + 1] = " "
                    state = "code"
                    index += 1
            line_start = char == "\n"
            index += 1
        return "".join(result)


def parser_cache_key(path: str, file_hash: str, language: str) -> str:
    return f"parse|{PARSER_VERSION}|{language}|{path}|{file_hash}"
