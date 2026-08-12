from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from refine_sast.batching import BatchBuilder, ResultBlindSelector
from refine_sast.cache import ContentHashCache
from refine_sast.models import Chunk
from refine_sast.risk import RiskRanker
from refine_sast.scanner import classify_scope, detect_language


def make_chunk(index: int, path: str, tag: str, tokens: int = 100) -> Chunk:
    return Chunk(
        chunk_id=f"CHK-{index:03d}",
        path=path,
        symbol=f"function_{index}",
        kind="function",
        start_line=1,
        end_line=10,
        start_byte=0,
        end_byte=100,
        content_hash=f"sha256:{index:064x}",
        scope="primary-source",
        estimated_tokens=tokens,
        calls=[],
        referenced_types=[],
        referenced_macros=[],
        risk_tags=[tag],
        risk_evidence=[{"api": "memcpy", "tag": tag, "line": 5}],
        parse_quality="full",
        complexity=3,
        guard_count=1,
        pointer_operations=1,
        parent_symbol=f"function_{index}",
    )


class BatchCacheScannerTests(unittest.TestCase):
    def test_cache_persists_hits_and_content_keys_do_not_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.json"
            cache = ContentHashCache(path)
            self.assertIsNone(cache.get("hash-a"))
            cache.put("hash-a", {"value": 1})
            cache.save()
            loaded = ContentHashCache(path)
            self.assertEqual(loaded.get("hash-a"), {"value": 1})
            self.assertIsNone(loaded.get("hash-b"))
            self.assertEqual((loaded.hits, loaded.misses), (1, 1))
            loaded.retain_prefix("new-version|")
            self.assertEqual(loaded.size, 0)
            self.assertEqual(loaded.pruned, 1)

    def test_scope_precedence(self) -> None:
        cases = {
            "containers/rtsp/rtsp_reader.c": "primary-source",
            "containers/test/test_bits.c": "test",
            "host_applications/linux/apps/hello_pi/hello.c": "example-demo",
            "opensrc/helpers/libfdt/fdt.c": "bundled-opensource",
            "vcfw/rtos/common/rtos_common_mem.c": "firmware-side",
            "interface/vcos/vcos.h": "header-context",
            "host_applications/android/apps/a.c": "android-specific",
        }
        for path, expected in cases.items():
            language = detect_language(path)
            self.assertEqual(classify_scope(path, language)[0], expected)

    def test_batch_budget_and_result_blind_three_selection(self) -> None:
        chunks = [
            make_chunk(1, "interface/a.c", "raw-memory", 500),
            make_chunk(2, "containers/b.c", "network", 500),
            make_chunk(3, "helpers/c.c", "command-process", 500),
            make_chunk(4, "interface/d.c", "unbounded-string", 500),
        ]
        batches = BatchBuilder(RiskRanker(), max_tokens=600).build(chunks)
        self.assertEqual(len(batches), 4)
        self.assertTrue(all(item.source_token_estimate <= 600 for item in batches))
        selected, manifest = ResultBlindSelector(3).select(batches)
        self.assertEqual(len(selected), 3)
        self.assertEqual(len(manifest["selected_batch_ids"]), 3)
        self.assertTrue(manifest["result_blind"])
        self.assertEqual(manifest["llm_calls_before_selection"], 0)
        self.assertEqual(len({item.focus_path for item in selected}), 3)


if __name__ == "__main__":
    unittest.main()
