from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from refine_sast.pipeline import run_pipeline
from refine_sast.validator import validate_artifacts


class IntegrationTests(unittest.TestCase):
    def test_fixture_repository_end_to_end_and_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            artifacts = root / "artifacts"
            cache = root / "cache.json"
            files = {
                "interface/a.c": "void a(char*d,const char*s){memcpy(d,s,8);}\n",
                "containers/b.c": "int b(int s,char*p){return recv(s,p,128,0);}\n",
                "helpers/c.c": 'int c(void){return system("true");}\n',
                "middleware/d.c": 'int d(char*x){return sprintf(x,"%s","x");}\n',
                "interface/context.h": "typedef struct { int n; } Context;\n",
                "README.md": "fixture\n",
            }
            for relative, content in files.items():
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            subprocess.run(["git", "-C", str(target), "config", "user.email", "fixture@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(target), "config", "user.name", "Fixture"], check=True)
            subprocess.run(["git", "-C", str(target), "add", "."], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-qm", "fixture"], check=True)

            first = run_pipeline(target, artifacts, cache, chunk_tokens=1800, batch_tokens=6000)
            first_selection = json.loads((artifacts / "batches" / "selection.json").read_text(encoding="utf-8"))
            second = run_pipeline(target, artifacts, cache, chunk_tokens=1800, batch_tokens=6000)
            second_selection = json.loads((artifacts / "batches" / "selection.json").read_text(encoding="utf-8"))

            self.assertEqual(first["tracked_files"], len(files))
            self.assertEqual(first["artifact_fingerprint"], second["artifact_fingerprint"])
            self.assertEqual(first_selection, second_selection)
            self.assertEqual(len(second_selection["selected_batch_ids"]), 3)
            self.assertGreater(second["cache"]["hits"], 0)
            validation = validate_artifacts(target, artifacts)
            self.assertEqual(validation["chunks"], second["chunks"])
            self.assertTrue(validation["target_clean"])
            status = subprocess.check_output(
                ["git", "-c", f"safe.directory={target.as_posix()}", "-C", str(target), "status", "--porcelain=v1"]
            )
            self.assertEqual(status, b"")


if __name__ == "__main__":
    unittest.main()
