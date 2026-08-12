# REFINE-SAST

REFINE-SAST는 대형 C/C++ 저장소를 함수 중심 Chunk로 분할하고, 네 개의
독립 Agent가 검증 가능한 Evidence와 proof obligation을 공유해 취약점
후보를 판단하는 Multi-Agent SAST 도구입니다.

과제 보고서는 [report.md](report.md), 실제 최종 분석 결과는
[`artifacts/final-run/`](artifacts/final-run/)에 있습니다.

## 주요 특징

- 오류 허용 Tree-sitter C/C++ parser와 brace-aware fallback
- 함수·주석·문자열·전처리기 경계를 고려한 Semantic Chunker
- 분석 결과를 확인하기 전에 위험도와 다양성으로 분석 Batch 고정
- Triage, Investigator, Challenger, Judge의 별도 클래스·prompt·schema
- Evidence 경로·행·byte·content hash 재검증
- Evidence ID 기반 구조형 Agent 통신
- proof obligation을 강제하는 결정론적 Judge safety kernel
- Token Governor, 최소 문맥 검색, Content Hash Cache
- Gemini 중심의 공통 Provider 인터페이스와 선택적 Local·Mock·OpenAI 구현
- API Key와 코드 원문을 남기지 않는 event/token ledger

## 구조

```text
.
├── sast.py                  # 공개 사용자 기본 진입점
├── report.md                # 과제 제출 보고서
├── config/                  # 역할별 Provider/model profile
├── prompts/                 # Agent 역할 prompt
├── schemas/                 # 입출력·메시지 JSON schema
├── src/refine_sast/
│   ├── agents/              # Agent
│   ├── providers/           # Gemini/Ollama/Mock/OpenAI 경계
│   └── runtime/             # Orchestrator, Evidence, Proof, Token 제어
├── tests/                   # 네트워크 없는 단위·통합 테스트
├── artifacts/
│   ├── inventory/           # 저장소 스캔 결과
│   ├── chunks/              # 함수 중심 Chunk
│   ├── batches/             # Batch 및 고정 selection
│   └── final-run/           # 제출 기준 실제 분석 결과
└── docs/                    # 설계와 acceptance matrix
```

## 요구 환경

- Python 3.12 권장
- Git
- 실제 Gemini 분석을 할 때만 `GEMINI_API_KEY`

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## TargetCode 준비

TargetCode는 이 저장소에 포함하지 않습니다. 다음 위치에 별도로 복제하고
분석에 사용한 commit으로 고정합니다.

```powershell
git clone https://github.com/raspberrypi/userland target/userland
git -C target/userland checkout a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976
```

REFINE-SAST는 `target/userland`를 읽기 전용으로 취급하며 생성물은 전부
TargetCode 외부에 저장합니다.

## 기본 실행 방법

공개 사용자 진입점은 루트의 `sast.py` 하나입니다.

### 1. 스캔·Chunk·Batch 생성

```powershell
python sast.py scan
```

주요 결과:

- `artifacts/inventory/repository.json`
- `artifacts/chunks/chunks.jsonl`
- `artifacts/batches/batches.jsonl`
- `artifacts/batches/selection.json`

### 2. 오프라인 테스트

```powershell
python sast.py test
```

테스트는 fake Gemini/Ollama/Mock Provider를 사용하며 실제 API를 호출하지
않습니다. 정제된 GitHub 제출본 기준 핵심 테스트는 39/39 통과했습니다.

### 3. Gemini Agent별 Smoke Test

Windows 환경변수 등록 예시:

```powershell
$env:GEMINI_API_KEY = "발급받은_키"
python sast.py smoke --execute-approved
```

가상 C 코드만 사용해 Triage, Investigator, Challenger, Judge를 한 번씩
호출합니다. 실제 TargetCode와 고정 Batch는 사용하지 않습니다. 실제 분석
preflight는 이 Smoke 성공 결과를 확인합니다.

### 4. 선정 Batch 분석

```powershell
python sast.py analyze --execute-approved
```

`--execute-approved`가 없으면 실제 호출은 차단됩니다. 기본 12회 호출,
전체 19회 상한, transient 오류에 한한 역할별 1회 재시도, 최소 4.1초 호출
간격을 적용합니다. 모델은 `gemini-recovery` profile의
`gemini-3.5-flash-lite`입니다.

## 최종 실행 결과

- Batch 1: `CONFIRMED` — 수동 검토에서는 상위 길이 제한으로 오탐 가능성
- Batch 2: `CONFIRMED` — filename format 사용 경로, 실제 검토 우선 후보
- Batch 3: `INCONCLUSIVE` — 두 필수 proof가 UNKNOWN
- Agent/API 호출: 12/12, retry 0
- 실제 입력/출력: 32,148/1,181 tokens
- 전체 저장소 source baseline 대비 절감: 98.8314%

자동 판정과 수동 검토의 상세 구분은
[`artifacts/final-run/security-report.md`](artifacts/final-run/security-report.md)에
있습니다.

## 보안 주의사항

- API Key는 환경변수에서만 읽습니다.
- `.env`, key 파일, TargetCode, raw 실행 이력은 `.gitignore`로 제외합니다.
- 최종 artifact에는 API Key, prompt 본문, 불필요한 코드 원문을 기록하지
  않습니다.
- `CONFIRMED`는 자동 분석 결과이며 실제 배포 전 수동 검증이 필요합니다.
