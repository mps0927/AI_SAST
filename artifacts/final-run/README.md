# Final run artifacts

이 디렉터리는 실제 고정 3개 Batch 분석
`gemini-recovery-3batch-20260812T175919Z`의 제출용 불변 사본이다. 원본은
로컬 `artifacts/runs/`에 보존돼 있으며 GitHub에서는 제외한다.

| 파일 | 내용 |
|---|---|
| `events.jsonl` | Agent 순서, 구조형 메시지, 상태 전이 |
| `token-ledger.json` | 호출별 model/version/token/retry/latency |
| `batch-results.json` | Finding, Evidence, proof obligation, verdict |
| `run-summary.json` | 3개 Batch와 전체 실행 요약 |
| `token-savings-report.json` | 전체 저장소/선정 코드 baseline 대비 절감량 |
| `security-report.md` | 자동 판정과 수동 검토를 구분한 최종 보안 결과 |

JSON/JSONL 파일은 실제 실행에서 생성된 원본 사본이며, 사람이 읽는
`security-report.md`만 깨진 문자와 수동 검토 설명을 제출용으로 정리했다.
