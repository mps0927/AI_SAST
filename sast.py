from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parent
SRC = WORKSPACE / "src"
CORE_TEST_FILES = (
    "tests/test_batch_cache_scanner.py",
    "tests/test_gemini_provider.py",
    "tests/test_gemini_recovery_integration.py",
    "tests/test_gemini_recovery_smoke.py",
    "tests/test_integration.py",
    "tests/test_judge_safety_kernel.py",
    "tests/test_live_gemini_analysis.py",
    "tests/test_parser_chunker.py",
    "tests/test_stage3_evidence_governor.py",
    "tests/test_stage3_schemas.py",
)
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def scan(args: argparse.Namespace) -> int:
    from refine_sast.pipeline import run_pipeline

    result = run_pipeline(
        args.target,
        args.artifacts,
        args.cache,
        args.chunk_tokens,
        args.batch_tokens,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def analyze(args: argparse.Namespace) -> int:
    if not args.execute_approved:
        raise SystemExit(
            "실제 Gemini 분석은 --execute-approved를 명시해야 실행됩니다."
        )
    from refine_sast.runtime.recovery_gemini_analysis import RecoveryGeminiAnalysis

    result = RecoveryGeminiAnalysis(
        WORKSPACE,
        max_api_calls=19,
        min_start_interval_seconds=4.1,
    ).run()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def smoke(args: argparse.Namespace) -> int:
    if not args.execute_approved:
        raise SystemExit(
            "실제 Gemini Smoke Test는 --execute-approved를 명시해야 실행됩니다."
        )
    from run_gemini_recovery_smoke import run_smoke

    result = run_smoke(WORKSPACE)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def test(_: argparse.Namespace) -> int:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), str(WORKSPACE / ".deps")]
    )
    return subprocess.call(
        [sys.executable, "-m", "unittest", "-v", *CORE_TEST_FILES],
        cwd=WORKSPACE,
        env=environment,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="REFINE-SAST: reproducible Multi-Agent SAST for C/C++"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser(
        "scan", help="TargetCode를 분할하고 고정 분석 Batch를 생성"
    )
    scan_parser.add_argument(
        "--target", type=Path, default=WORKSPACE / "target" / "userland"
    )
    scan_parser.add_argument(
        "--artifacts", type=Path, default=WORKSPACE / "artifacts"
    )
    scan_parser.add_argument(
        "--cache", type=Path, default=WORKSPACE / ".cache" / "parser-cache.json"
    )
    scan_parser.add_argument("--chunk-tokens", type=int, default=1800)
    scan_parser.add_argument("--batch-tokens", type=int, default=6000)
    scan_parser.set_defaults(handler=scan)

    analyze_parser = subparsers.add_parser(
        "analyze", help="고정 3개 Batch를 Gemini Multi-Agent로 분석"
    )
    analyze_parser.add_argument(
        "--execute-approved",
        action="store_true",
        help="실제 API 호출을 인지하고 승인한 경우에만 지정",
    )
    analyze_parser.set_defaults(handler=analyze)

    smoke_parser = subparsers.add_parser(
        "smoke", help="가상 C 코드로 네 Agent의 Gemini 호환성 확인"
    )
    smoke_parser.add_argument(
        "--execute-approved",
        action="store_true",
        help="실제 4회 API 호출을 인지하고 승인한 경우에만 지정",
    )
    smoke_parser.set_defaults(handler=smoke)

    test_parser = subparsers.add_parser(
        "test", help="네트워크 없는 전체 단위·통합 테스트 실행"
    )
    test_parser.set_defaults(handler=test)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
