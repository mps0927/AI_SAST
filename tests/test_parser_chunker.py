from __future__ import annotations

import unittest

from refine_sast.chunker import SemanticChunker
from refine_sast.parser import BraceAwareFallback, TreeSitterBackend
from refine_sast.tokens import TokenEstimator


class ParserChunkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = TreeSitterBackend()
        self.estimator = TokenEstimator()

    def test_tree_sitter_function_ground_truth_and_text_boundaries(self) -> None:
        source = br'''
#define WRAP(x) do { x; } while (0)
/* fake(void) { must not become a function } */
int safe(const char *value) {
    const char *text = "string with } and { and ;";
    if (value) { WRAP(memcpy((void *)text, value, 2)); }
    return 0;
}
static void second(void) { /* } */ return; }
'''
        result = self.parser.parse(source, "c")
        self.assertEqual(result.quality, "full")
        self.assertEqual({item.symbol for item in result.functions}, {"safe", "second"})
        safe = next(item for item in result.functions if item.symbol == "safe")
        self.assertIn("memcpy", {item["name"] for item in safe.calls})

    def test_fallback_ignores_braces_in_strings_comments_and_preprocessor(self) -> None:
        source = br'''
#define BAD { not_code }
/* fake() { } */
int real(void) {
    const char *s = "{ still text }";
    return 1;
}
'''
        result = BraceAwareFallback().parse(source)
        self.assertEqual([item.symbol for item in result.functions], ["real"])

    def test_oversized_function_uses_safe_boundaries_and_stable_ids(self) -> None:
        statements = "\n".join(
            f'if (value == {index}) {{ puts("literal {{ {index} }} ;"); }} /* comment {{ }} */'
            for index in range(60)
        )
        source = f"int huge(int value) {{\n{statements}\nreturn value;\n}}\n".encode()
        result = self.parser.parse(source, "c")
        self.assertEqual(len(result.functions), 1)
        chunker = SemanticChunker(self.estimator, max_tokens=90)
        first = chunker.chunk_function("huge.c", "primary-source", result.quality, source, result.functions[0])
        second = chunker.chunk_function("huge.c", "primary-source", result.quality, source, result.functions[0])
        self.assertGreater(len(first), 1)
        self.assertEqual([item.chunk_id for item in first], [item.chunk_id for item in second])
        self.assertTrue(all(item.estimated_tokens <= 90 for item in first if not item.budget_exception))
        for item in first:
            raw = source[item.start_byte : item.end_byte]
            self.assertEqual(raw.count(b'"') % 2, 0, raw)
            self.assertEqual(raw.count(b"/*"), raw.count(b"*/"), raw)

    def test_token_estimator_treats_literals_as_single_lexical_tokens(self) -> None:
        simple = self.estimator.estimate_text('puts("a very long string with spaces and } ; {");')
        self.assertLessEqual(simple, 5)

    def test_identical_function_text_at_different_offsets_has_unique_ids(self) -> None:
        source = b"int same(void){return 1;}\nint same(void){return 1;}\n"
        result = self.parser.parse(source, "c")
        chunks = []
        for function in result.functions:
            chunks.extend(
                SemanticChunker(self.estimator).chunk_function(
                    "duplicate.c", "primary-source", result.quality, source, function
                )
            )
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len({item.chunk_id for item in chunks}), 2)

    def test_preprocessor_conditional_is_never_split_internally(self) -> None:
        source = b'''int configured(int value) {
value += 1;
value += 2;
value += 3;
#if FEATURE_FLAG
value += 10;
value += 11;
#else
value += 20;
#endif
value += 4;
value += 5;
return value;
}
'''
        result = self.parser.parse(source, "c")
        chunks = SemanticChunker(self.estimator, max_tokens=24).chunk_function(
            "configured.c", "primary-source", result.quality, source, result.functions[0]
        )
        self.assertGreater(len(chunks), 1)
        preprocessor_chunks = [
            source[item.start_byte : item.end_byte]
            for item in chunks
            if b"#if" in source[item.start_byte : item.end_byte]
            or b"#endif" in source[item.start_byte : item.end_byte]
        ]
        self.assertEqual(len(preprocessor_chunks), 1)
        self.assertIn(b"#if FEATURE_FLAG", preprocessor_chunks[0])
        self.assertIn(b"#endif", preprocessor_chunks[0])


if __name__ == "__main__":
    unittest.main()
