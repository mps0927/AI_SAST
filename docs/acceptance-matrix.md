# REFINE-SAST 요구사항 추적 및 인수 기준

## 상태 정의

- `PASS`: 현재 단계에서 객관적 증거로 충족
- `DESIGNED`: 설계와 합격 기준은 있으나 구현 전
- `PENDING`: 후속 단계가 필요
- `BLOCKED`: 외부 입력이나 권한 없이는 진행 불가

## 1. 과제 필수조건

| ID | 요구사항 | 설계 대응 | 필수 산출물 | 합격 기준 | 단계 | 현재 상태 |
|---|---|---|---|---|---:|---|
| REQ-01 | 거대한 저장소를 효과적으로 탐색 | Repository Mapper, scope classifier, parser backend | `repository.json` | 모든 추적 파일이 정확히 한 scope로 분류되고 skip 이유가 기록됨 | 2 | DESIGNED |
| REQ-02 | 코드 분할 처리 | 함수 중심 Semantic Chunker, oversized 의미 분할 | `chunks.jsonl` | 모든 primary 함수에 안정적 ID, 경로, 줄, 해시, 토큰 추정치가 존재 | 2 | DESIGNED |
| REQ-03 | Batch 구성 | focal 함수+근접 의존성 Batch Builder | `batches.jsonl` | 모든 Batch가 설정한 token budget 이하이고 원문 경계를 보존 | 2 | DESIGNED |
| REQ-04 | 분할 Batch 중 3개 분석 | result-blind risk+diversity selector | `selection.json`, 결과 JSON 3개 | LLM 실행 전에 정확히 3개 ID가 고정되고 모두 결과를 가짐 | 2/5 | DESIGNED |
| REQ-05 | Multi-Agent 구조 | Triage, Investigator, Challenger, Judge | Agent 모듈, prompt, schema, event log | 네 Agent가 독립 context로 실제 실행되고 각 실행 흔적이 남음 | 3 | DESIGNED |
| REQ-06 | Agent별 구체적 역할 | 권한·입력·출력·금지 동작 분리 | `prompts/`, `schemas/` | 동일 prompt 복제 대신 역할별 schema와 종료 조건이 다름 | 3 | DESIGNED |
| REQ-07 | 토큰 절약 방안 수립 | semantic selection, 3-Batch 실행, cache, evidence refs, progressive context | 설계 문서, Token Governor 설정 | 실제 토큰 절감과 비용 절감을 구분해 설명 | 1 | PASS |
| REQ-08 | 토큰 절약 방안 적용 | Token Governor, usage tracker, content cache | `token-ledger.json` | baseline과 actual 사용량이 동일 tokenizer 기준으로 계산됨 | 4/5 | DESIGNED |

## 2. 신뢰성과 창의성

| ID | 품질 요구 | 설계 대응 | 합격 기준 | 단계 | 현재 상태 |
|---|---|---|---|---:|---|
| QLT-01 | 근거 없는 취약점 방지 | Evidence ID 없이는 `SUPPORTED` 불가 | 모든 CONFIRMED claim이 실제 파일·줄·해시와 연결 | 3/5 | DESIGNED |
| QLT-02 | 오탐 억제 | 독립 Challenger Agent | 모든 제출 Batch에 contradiction 검토 흔적 존재 | 5 | DESIGNED |
| QLT-03 | 판단 불가 표현 | `INCONCLUSIVE` 상태 | 미확인 필수 obligation 또는 예산 소진 시 안전 판정 금지 | 3/5 | DESIGNED |
| QLT-04 | 창의적 토큰 절약 | proof-obligation 기반 context refinement | Agent가 유사도 Top-K가 아니라 미확인 증명 조건으로 코드를 요청 | 3/5 | DESIGNED |
| QLT-05 | Multi-Agent 토큰 폭증 방지 | Evidence Blackboard | Agent 간 메시지가 4개 구조형 종류로 제한되고 원문 중복률 측정 | 3/5 | DESIGNED |
| QLT-06 | 결과 cherry-pick 방지 | result-blind selector | `selection.json` 생성 시간이 첫 LLM 호출보다 앞서고 규칙/seed 기록 | 2/5 | DESIGNED |
| QLT-07 | 재현 가능성 | commit/parser/prompt/model/hash 기록 | 동일 설정 재실행 시 Chunk/Batch ID와 선택 3개가 동일 | 2 | DESIGNED |
| QLT-08 | TargetCode 불변 | 입력/산출물 경로 분리 | 분석 전후 TargetCode `git status --short`가 비어 있음 | 모든 단계 | PASS |

## 3. Stage 1 인수 기준

| ID | 확인 항목 | 증거 | 상태 |
|---|---|---|---|
| S1-01 | TargetCode 존재 및 commit 고정 | `target/userland`, commit `a54a0d...` | PASS |
| S1-02 | 저장소 크기·언어·구조 조사 | `docs/repository-inventory.md` 3장 | PASS |
| S1-03 | 빌드 방식 조사 | `docs/repository-inventory.md` 4장 | PASS |
| S1-04 | 분석/제외 정책 수립 | `docs/repository-inventory.md` 5장 | PASS |
| S1-05 | 위험 API 분포 조사 | `docs/repository-inventory.md` 6장 | PASS |
| S1-06 | 코드 분할·Batch 설계 | `docs/design.md` 6~8장 | PASS |
| S1-07 | Agent 역할·통신 설계 | `docs/design.md` 10~11장 | PASS |
| S1-08 | 토큰 예산·측정 설계 | `docs/design.md` 12~13장 | PASS |
| S1-09 | TargetCode 변경 없음 | Target `git status --short` 출력 없음 | PASS |

## 4. Stage 2 인수 기준

| ID | 확인 항목 | 합격 기준 | 상태 |
|---|---|---|---|
| S2-01 | Inventory 생성 | 830개 기준 추적 파일 누락 0, 각 파일 scope/해시 보유 | PASS (`artifacts/inventory/repository.json`) |
| S2-02 | 함수 추출 | fixture ground truth 100%, Target parse 결과에 품질 상태 기록 | PASS (Tree-sitter+fallback, 654개 파싱) |
| S2-03 | Chunk 경계 | 함수/문자열/주석/전처리 블록을 잘못 절단하는 테스트 0 | PASS (경계 테스트 통과) |
| S2-04 | Token budget | 모든 일반 Chunk ≤ 1,800, 초기 Batch ≤ 6,000; 예외는 사유 기록 | PASS (최대 1,798/5,987, 예외 0) |
| S2-05 | Stable identity | 재실행 시 변경 없는 입력의 Chunk/Batch ID 동일 | PASS (핵심 산출물 SHA-256 재실행 일치) |
| S2-06 | Risk manifest | 점수 구성 요소와 근거 위치가 각 Batch에 존재 | PASS (520개 Batch 전수 검증) |
| S2-07 | 세 Batch 선정 | 정확히 3개, 서로 다른 focal 파일, 다양성 제약 결과 기록 | PASS (`selection.json`, result-blind) |
| S2-08 | 테스트 | Unit/integration test 전체 통과 | PASS (10/10) |
| S2-09 | Target 불변 | 실행 전후 Target `git status --short` 비어 있음 | PASS |

## 5. Stage 3 인수 기준

| ID | 확인 항목 | 합격 기준 | 상태 |
|---|---|---|---|
| S3-01 | 실제 Agent 분리 | Triage/Investigator/Challenger/Judge가 별도 prompt/schema/context 보유 | PASS (4개 클래스, prompt 4개, 역할별 input/output schema 8개) |
| S3-02 | 구조형 메시지 | 허용된 4종 이외 자유형식 Agent 메시지 차단 | PASS (`FINDING/EVIDENCE/REQUEST_CONTEXT/CONTRADICTION`, Pydantic strict validation) |
| S3-03 | Evidence 검증 | 존재하지 않는 파일·줄·변조된 hash 등록 거부 | PASS (경로·줄·byte·hash 변조 테스트 통과) |
| S3-04 | Token Governor | 예산/확장 한도 초과 시 요청 거부 및 `INCONCLUSIVE` 가능 | PASS (2회 승인 후 3회차 거부, INCONCLUSIVE) |
| S3-05 | Mock end-to-end | 세 verdict 시나리오가 실제 상태 머신을 끝까지 통과 | PASS (CONFIRMED/REJECTED/INCONCLUSIVE, 19/19 테스트) |
| S3-06 | 실행 로그 | 네 Agent의 실행 순서·입출력 참조가 JSONL에 남음 | PASS (3 runs, 14 Agent calls, 41 events, raw source 0) |

## 6. Stage 4~6 인수 기준

| ID | 확인 항목 | 합격 기준 | 단계 | 상태 |
|---|---|---|---:|---|
| LLM-01 | Provider 추상화 | Mock/실제 provider가 동일 인터페이스 사용 | 4 | PENDING |
| LLM-02 | Secret 보호 | API key가 log, exception, artifact에 없음 | 4 | PENDING |
| LLM-03 | 모델 라우팅 | Triage는 저비용, 분석/반증/검증은 고성능 설정 | 4 | PENDING |
| LLM-04 | 사용량 기록 | 모든 호출에 모델, input/output/cached/reasoning token 기록 | 4 | PENDING |
| RUN-01 | 3개 Batch 완료 | 고정된 세 ID 각각에 verdict와 Evidence가 존재 | 5 | PENDING |
| RUN-02 | 독립 반증 | 세 Batch 각각 Challenger 결과 존재 | 5 | PENDING |
| RUN-03 | 독립 검증 | 세 Batch 각각 Judge 결과 존재 | 5 | PENDING |
| RUN-04 | 근거 재확인 | 제출 직전 모든 인용 범위와 hash가 Target과 일치 | 5/6 | PENDING |
| MET-01 | Baseline 비교 | repository/all-batch baseline과 actual 비교 | 6 | PENDING |
| MET-02 | 절감률 | 입력 토큰, 고성능 호출, cache, 비용을 각각 보고 | 6 | PENDING |
| REP-01 | 최종 보고서 | 구조도, 역할, Skill 주안점, prompt, 토큰 설계, 3개 결과, 차별점 포함 | 6 | PENDING |
| REP-02 | 재현 절차 | 새 환경에서 commit 고정부터 결과 생성까지 명령 제공 | 6 | PENDING |

## 7. 제출 전 최종 게이트

다음 중 하나라도 실패하면 완료로 표시하지 않는다.

1. TargetCode에 변경이 남음
2. 선택한 Batch가 3개가 아님
3. Batch 선택이 첫 LLM 결과 이후 변경됨
4. 네 Agent 중 하나가 실제 실행되지 않음
5. CONFIRMED 결과에 원본 Evidence가 없음
6. 토큰 baseline 또는 actual usage가 없음
7. 비용 절감 기능을 실제 토큰 절감으로 잘못 보고함
8. API key 또는 민감정보가 artifact에 포함됨
9. 보고서에 실제 사용 prompt가 빠짐
10. 실행·테스트 명령이 재현되지 않음

## 8. Stage 4 재평가 (2026-08-11)

아래 표는 위의 Stage 4 `PENDING` 행을 대체한다. 실제 3개 Batch 분석은 Stage 5 범위이므로 `RUN-*`은 계속 `PENDING`이다.

| ID | 검증 항목 | 증거 | 상태 |
|---|---|---|---|
| LLM-01 | Mock/실제 Provider 동일 인터페이스와 schema | `providers/base.py`, 4역할 contract test | PASS |
| LLM-02 | API key가 설정·로그·예외·artifact에 없음 | lazy env read, sanitization test, `stage4-verification.json` | PASS |
| LLM-03 | 역할별 모델·reasoning 라우팅 | `config/model-routing.json`, routing test | PASS |
| LLM-04 | 모든 실제 호출의 usage ledger 기반 | `usage_tracker.py`, provider success/failure test, dry-run ledger | PASS |
| LLM-05 | timeout/rate limit/schema/provider 오류 제한 재시도 | `retry.py`, retry/exhaustion test | PASS |
| LLM-06 | key 부재 시 네트워크 호출 없음 | missing-key network-free contract test | PASS |
| LLM-07 | 선택적 OpenAI Provider 계약 분리 | `providers/openai_responses.py` | PASS |
| LLM-08 | 실제 분석·유료 호출 미실행 | verification의 `network_calls=0`, `paid_calls=0` | PASS |
| LLM-09 | TargetCode 불변 | 작업 전후 `git status --short` clean | PASS |

## 9. Stage 5 Local LLM Provider 검증 (2026-08-11)

Stage 5는 설치·모델 다운로드·실제 보안 분석 없이 로컬 Provider 실행 기반만 검증한다. 실제 3개 Batch 판정과 baseline 절감률은 Stage 6에서 수행하므로 `RUN-*`, `MET-*`는 계속 `PENDING`이다.

| ID | 검증 항목 | 증거 | 상태 |
|---|---|---|---|
| S5-01 | Local Provider 공통 계약 | `providers/local_llm.py`, Mock/Local 4역할 contract test | PASS |
| S5-02 | Ollama transport 분리 | `providers/ollama_transport.py`, 주입형 fake transport | PASS |
| S5-03 | 역할별 로컬 모델 profile | `config/model-routing.json`의 `local-balanced` | PASS |
| S5-04 | Structured Output | Agent별 Pydantic JSON Schema를 Ollama `format`에 전달하고 응답 재검증 | PASS |
| S5-05 | Schema 실패 안전 종료 | 검증 실패 후 정확히 1회 재시도, 이후 `INCONCLUSIVE` | PASS |
| S5-06 | Token usage 정규화 | `prompt_eval_count/eval_count` 매핑, cached/reasoning 미지원 표시 | PASS |
| S5-07 | 검증된 source packet | Evidence Blackboard + Context Retriever 재검증 후 일시적 입력, 영속 저장 0 | PASS |
| S5-08 | 결정론적 LLM cache | provider/model digest/prompt/schema/context hash key, 재호출 0 | PASS |
| S5-09 | Stage-barrier 순차 실행 | 고정 3개 Batch × 4역할 = 12 job, 역할 wave와 안전 종료 검증 | PASS |
| S5-10 | Local 기본·OpenAI 선택적 | 기본 `local-balanced`; Local factory에서 key 확인·OpenAI 초기화 0 | PASS |
| S5-11 | 설치·다운로드·실제 분석 없음 | `artifacts/stage5-verification.json`의 network/model/security count 0 | PASS |
| S5-12 | TargetCode 불변 | 작업 전후 Target `git status --short` clean | PASS |

## 10. Stage 6 Local 실행 사전검증 (2026-08-11)

실제 3개 Batch 분석을 시작하기 전 설치·모델·Structured Output·자원 안정성을 검증하고 중지했다. `RUN-*`, `MET-*`는 아직 `PENDING`이다.

| ID | 검증 항목 | 증거 | 상태 |
|---|---|---|---|
| S6-PRE-01 | Ollama 설치·API | Windows Ollama 0.32.8, localhost API | PASS |
| S6-PRE-02 | 승인된 모델만 설치 | 요청된 4개 Q4_K_M tag와 digest | PASS |
| S6-PRE-03 | 모델별 로딩 | 4개 모두 설정 context로 load/unload 성공 | PASS |
| S6-PRE-04 | Structured Output | 합성 C smoke schema 4/4 통과 | PASS |
| S6-PRE-05 | Token 속도 계측 | Ollama eval count/duration 기반 token/s 4개 기록 | PASS |
| S6-PRE-06 | GPU offload 확인 | 4개 모두 `size_vram=0`, CPU/RAM 실행 | PASS (제약 기록) |
| S6-PRE-07 | RAM 안정 여유 | Judge 8B 실행 중 가용 RAM 최저 0.513GB | CONSTRAINED |
| S6-PRE-08 | 실제 분석 미실행 | benchmark의 `target_code_used=false`, `actual_security_analysis=false` | PASS |
| S6-PRE-09 | TargetCode 불변 | 종료 시 Target clean, commit 고정 | PASS |

## 11. 저메모리 Local profile 검증 (2026-08-12)

| ID | 검증 항목 | 증거 | 상태 |
|---|---|---|---|
| S6-CON-01 | 기존 profile 보존 | `local-balanced`가 기본 profile과 기존 설정을 유지 | PASS |
| S6-CON-02 | 별도 저메모리 profile | `config/model-routing.json`의 `local-constrained`, 지정 모델·context 4개 | PASS |
| S6-CON-03 | Judge 결정론적 판정 | `JudgeAgent`의 proof-obligation 강제 규칙 및 전체 verdict 시나리오 테스트 | PASS |
| S6-CON-04 | 역할별 단독 모델 로딩 | Triage, Investigator, Challenger, Judge 4/4 로딩 및 역할 종료 후 언로드 | PASS |
| S6-CON-05 | 실제 Agent schema Structured Output | Triage·Investigator PASS, Challenger·Judge는 800-token 한도 소진으로 실패 | CONSTRAINED |
| S6-CON-06 | 실행 중 가용 RAM 2GB 이상 | 최저 Triage 4.075GB, Investigator 0.374GB, Challenger 1.632GB, Judge 1.813GB | BLOCKED |
| S6-CON-07 | 실제 Batch 분석 미실행 | benchmark에 source/TargetCode 미사용, `actual_three_batch_analysis=false` | PASS |
| S6-CON-08 | 설치 모델 불변 | 다운로드·삭제·tag 변경 0 | PASS |
| S6-CON-09 | TargetCode 불변 | 작업 후 `target/userland` git status clean | PASS |
| S6-CON-10 | constrained thinking-off 요청 | Challenger·Judge route의 `think=false`, 모델·context 불변 | PASS (설정) |
| S6-CON-11 | thinking 실제 비활성화 | 두 응답 모두 `thinking_present=true`; thinking-only 모델 특성 확인 | UNSUPPORTED BY MODEL |
| S6-CON-12 | 토큰 절감 효과 | Challenger 800→800, Judge 800→800 output token | 0% / FAILED |
| S6-CON-13 | 재시험 Structured Output | Challenger·Judge 모두 800-token 한도 소진 후 schema 실패 | BLOCKED |
| S6-CON-14 | 재시험 RAM 하한 | Challenger 최저 2.106GB, Judge 최저 2.291GB | PASS (두 역할만) |
| S6-CON-15 | 전체 profile RAM gate | 기존 Investigator 최저 0.374GB 결과가 남아 있음 | BLOCKED |
| S6-CON-16 | Judge Safety Kernel 직접 검증 | 3 verdict, provider override 거부, budget 소진 강제 INCONCLUSIVE 테스트 | PASS |
## 12. Gemini provider offline verification (2026-08-12)

| ID | Acceptance item | Evidence | Status |
|---|---|---|---|
| GEM-01 | Existing four-role provider contract | `providers/gemini_provider.py`, Mock/Gemini contract test | PASS |
| GEM-02 | Transport separated from provider | `providers/gemini_transport.py`, injected fake requester test | PASS |
| GEM-03 | Pydantic Structured Output | `responseMimeType=application/json`, `responseJsonSchema`, four role schemas | PASS |
| GEM-04 | Gemini usage normalization | prompt/candidate/cached/thought token mapping test | PASS |
| GEM-05 | Secret protection | lazy env read, header-only key, sanitized errors, ledger/cache scan | PASS |
| GEM-06 | Bounded schema repair | one retry, then deterministic `INCONCLUSIVE` | PASS |
| GEM-07 | Existing cache and verified context | context hash cache; source absent from cache and ledger | PASS |
| GEM-08 | Provider isolation | Gemini profile does not initialize OpenAI or Ollama | PASS |
| GEM-09 | Network-free verification | fake transport only; network calls 0; paid calls 0 | PASS |
| GEM-10 | Live smoke test | `gemini-3.6-flash`, synthetic Triage, exactly 1 call, valid `TriageOutput`; `artifacts/runs/gemini-smoke-triage-once/` | PASS |
| GEM-11 | Actual three-Batch analysis | Separate post-smoke approval gate | PENDING |
| GEM-12 | TargetCode read-only | pre/post `git status --short` clean | PASS |

## 13. Gemini actual three-Batch run (2026-08-12)

Run evidence: `artifacts/runs/gemini-live-3batch-20260812T135957Z/`.

| ID | Acceptance item | Evidence | Status |
|---|---|---|---|
| GEM-LIVE-01 | All preconditions before network use | Key existence Boolean, `gemini-free`, approved model, exactly three fixed result-blind batches, successful smoke, clean target | PASS |
| GEM-LIVE-02 | Analyze only fixed selection | Selection hash unchanged; no Batch reselection | PASS |
| GEM-LIVE-03 | Four agents execute for every Batch | Triage succeeded for two Batches; Investigator schema failed; third Triage timed out/schema-failed; downstream agents were fail-safe stopped | FAIL |
| GEM-LIVE-04 | Bounded retry and safe verdict | One retry per failed logical call; all incomplete proof tables resolved only to `INCONCLUSIVE` | PASS |
| GEM-LIVE-05 | Evidence validation | Focus path, line/byte range, and content hash revalidated against TargetCode | PASS |
| GEM-LIVE-06 | Usage ledger completeness | Successful-call usage recorded; Gemini returned no usage metadata for timeout/schema-invalid attempts | PARTIAL |
| GEM-LIVE-07 | Cache saves calls | Two successful Triage results reused; two network calls skipped | PASS |
| GEM-LIVE-08 | Secret/source protection | Key, full environment, prompt body, and raw source absent from run artifacts | PASS |
| GEM-LIVE-09 | No fallback or model switching | Gemini `gemini-3.6-flash` only; no OpenAI/Ollama fallback | PASS |
| GEM-LIVE-10 | TargetCode read-only | Target clean before and after; selection unchanged | PASS |
| GEM-LIVE-11 | Required six final run artifacts | events, ledger, batch results, summary, savings report, and security report present | PASS |
| GEM-LIVE-12 | Assignment-ready completed 3-Batch analysis | Four-role evidence-backed results are incomplete | FAIL / RE-RUN REQUIRED |

## 14. Gemini offline failure improvement (2026-08-12)

| ID | Acceptance item | Evidence | Status |
|---|---|---|---|
| GEM-FIX-01 | Diagnose without persisting invalid raw response | Ledger/run summary analysis and documented diagnostic limitation | PASS |
| GEM-FIX-02 | Simplify Gemini schema without weakening domain schema | Provider-only flat wire schema followed by strict domain adapter | PASS |
| GEM-FIX-03 | Preserve Evidence/proof/message constraints | Existing strict models plus role allowlist tests | PASS |
| GEM-FIX-04 | Continue after upstream failure | Synthetic required UNKNOWN proof packet and fail-safe state transitions | PASS |
| GEM-FIX-05 | Execute four roles per fixed Batch | Fake integration: each role 3 times, ordered Triage/Investigator/Challenger/Judge | PASS |
| GEM-FIX-06 | Deterministic safe verdict | Upstream failure and UNKNOWN proof force `INCONCLUSIVE` | PASS |
| GEM-FIX-07 | Bounded retry remains intact | Fake schema/timeout failures retry once only | PASS |
| GEM-FIX-08 | No live call or reselection | Fake transport, original selection hash, network calls 0 | PASS |

## 15. Gemini recovery verification (2026-08-13)

| ID | Acceptance item | Evidence | Status |
|---|---|---|---|
| GEM-REC-01 | Preserve domain Agent architecture | Agent classes, prompts, domain schema and Judge proof kernel hashes unchanged | PASS |
| GEM-REC-02 | Distinct safe failure diagnostics | Seven error codes plus finish/response/validation metadata; no response text | PASS |
| GEM-REC-03 | Preserve usage on invalid output | Fake missing/MAX_TOKENS/JSON/schema/domain responses retain usage | PASS |
| GEM-REC-04 | Avoid deterministic quota waste | JSON/wire/domain/output failures make one attempt; transient 429 retries once | PASS |
| GEM-REC-05 | Shallow provider-only schema | 429-1,762 byte role schemas; all fields required; no host ID fields | PASS |
| GEM-REC-06 | Deterministic host identifiers | Finding/message/obligation/Evidence links reconstructed from verified inputs | PASS |
| GEM-REC-07 | Structured inter-Agent packets | Finding, proof, Evidence summaries, contradictions and unresolved IDs delivered without repeated source | PASS |
| GEM-REC-08 | Exact fixed three-Batch execution | Fake Gemini: 3 Batches x 4 roles in fixed order = 12 calls | PASS |
| GEM-REC-09 | Meaningful normal fixtures | Finding, proof, contradiction and CONFIRMED/REJECTED/INCONCLUSIVE verdicts generated | PASS |
| GEM-REC-11 | No live API call | Injected fake transports only; network/API calls 0 | PASS |
| GEM-REC-12 | Regression and Target safety | Full suite 65/65; TargetCode clean before/after | PASS |

## 16. Gemini recovery execution readiness (2026-08-13)

| ID | Acceptance item | Evidence | Status |
|---|---|---|---|
| GEM-RDY-01 | Approved recovery model | Four-role live Smoke Test: `gemini-3.5-flash-lite`, 4/4 success, retry 0 | PASS |
| GEM-RDY-02 | Separate real execution profile | `gemini-recovery`와 별도 Smoke profile 유지 | PASS |
| GEM-RDY-03 | Fixed selection preserved | Three IDs and selection hash unchanged | PASS |
| GEM-RDY-04 | Existing four-Agent chain preserved | Fake transport: Triage → Investigator → Challenger → Judge, 3 times | PASS |
| GEM-RDY-05 | Bounded call plan | 12 base calls, 19 global attempts, at most 7 extra transient retries | PASS |
| GEM-RDY-06 | Explicit execution approval barrier | Recovery entrypoint requires `--execute-approved` | PASS |
| GEM-RDY-07 | Preflight performs no API call | Injected no-call transport; readiness network calls 0 | PASS |
| GEM-RDY-08 | Current free-tier capacity | AI Studio observed Lite usage: 3/15 RPM, 4.26K/250K TPM, 4/500 RPD | PASS |
| GEM-RDY-09 | TargetCode read-only | Readiness preflight TargetCode clean | PASS |
| GEM-RDY-10 | Regression suite | 제출본 핵심 오프라인 suite 통과; approval gate fails closed | PASS |

## 17. Actual fixed three-Batch Gemini execution (2026-08-13)

| ID | Acceptance item | Evidence | Status |
|---|---|---|---|
| GEM-RUN-01 | Exact fixed selection | Original three IDs and selection hash unchanged | PASS |
| GEM-RUN-02 | Four distinct roles per Batch | 3 Batches × Triage/Investigator/Challenger/Judge = 12 logical calls | PASS |
| GEM-RUN-03 | Approved model only | All 12 ledger calls use `gemini-3.5-flash-lite` | PASS |
| GEM-RUN-04 | Bounded calls and retry | API attempts 12/19, retries 0 | PASS |
| GEM-RUN-05 | Structured Evidence and proof | Three findings, verified Evidence ranges, required proof tables generated | PASS |
| GEM-RUN-06 | Deterministic Judge | Verdicts CONFIRMED, CONFIRMED, INCONCLUSIVE; UNKNOWN proof preserved | PASS |
| GEM-RUN-07 | Token measurement | input 32,148; output 1,181; cached/thinking 0 | PASS |
| GEM-RUN-08 | Token-saving measurement | repository source baseline reduction 98.8314%; selected-code reduction 46.896% | PASS |
| GEM-RUN-09 | No source/key leakage | Final ledger flags source/prompt false; API key value scan 0 | PASS |
| GEM-RUN-10 | TargetCode read-only | clean before and after; selection unchanged | PASS |
| GEM-RUN-11 | Honest manual review | Batch A false-positive risk and Batch C residual uncertainty documented separately | PASS |

## 18. GitHub submission readiness (2026-08-13)

| ID | Acceptance item | Evidence | Status |
|---|---|---|---|
| SUB-01 | Required report | Root `report.md` maps requirements, evaluation points, results and limitations | PASS |
| SUB-02 | Reproducible usage | Root `README.md`, `requirements.txt`, single `sast.py` entrypoint | PASS |
| SUB-03 | Curated final artifacts | `artifacts/final-run/` preserves six final analysis artifacts and its README | PASS |
| SUB-04 | Publication hygiene | `.gitignore` excludes TargetCode, caches, dependencies, raw runs and secrets | PASS |
| SUB-05 | Clean submission regression | Staging-only temporary checkout, 39/39 core offline tests | PASS |
