# REFINE-SAST 최종 보안 분석 결과

- Run ID: `gemini-recovery-3batch-20260812T175919Z`
- Target commit: `a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976`
- Selection hash: `sha256:8c89de6d61f5338d11066821fa374432dc95d23bf8a1ad47ffc11b137f087a50`
- Provider/model: `gemini-generate-content` / `gemini-3.5-flash-lite`
- Agent/API calls: 12 / 12, retry 0
- Repository source baseline reduction: 98.8314%

> 아래 Verdict는 도구가 생성한 자동 판정이다. 수동 검토는 자동 판정과
> 구분해 기록하며, 특히 `CONFIRMED`를 곧바로 악용 가능한 취약점으로
> 해석하지 않는다.

## Batch 결과

### BAT-A8363625BEDB28094BEF — 자동 판정: CONFIRMED

- Focus: `containers/simple/simple_reader.c::simple_read_header`
- 자동 가설: URI를 고정 배열로 읽는 폭 제한 없는 `sscanf("%s")`로 인한
  버퍼 오버플로 가능성(HIGH)
- Evidence: `EVD-D925C12B7E35BD982DDE`, 138~260행
- Proof: required 2, supported 2, refuted 0, unknown 0
- Challenger: 반례를 생성하지 않음
- Judge: `ALL_OBLIGATIONS_SUPPORTED`
- 수동 검토: `simple_read_line`이 한 행을 `MAX_LINE_SIZE`로 제한하고 URI
  배열이 `MAX_LINE_SIZE+1`이므로 현재 경로에서 실제 overflow 가능성은
  낮다. Challenger가 상위 입력 길이 불변식을 포착하지 못한 오탐 후보로
  분류한다.

### BAT-B3DA91B545FB3EF2B360 — 자동 판정: CONFIRMED

- Focus: `host_applications/linux/apps/raspicam/RaspiVid.c::open_filename`
- 자동 가설: 외부 filename을 `asprintf` 또는 `strftime` format으로 직접
  사용하는 format-string/비정상 자원 사용 가능성(MEDIUM)
- Evidence: `EVD-72AD55E6591FE38453B9`, 968~1,139행
- Proof: required 2, supported 2, refuted 0, unknown 0
- Challenger: 반례를 생성하지 않음
- Judge: `ALL_REQUIRED_OBLIGATIONS_SUPPORTED`
- 수동 검토: Segment 또는 split 경로에서 사용자가 제어한 filename이
  format 문자열로 사용된다. 단일 `%d`/`%u` 의도와 다른 복수 변환 지정자
  또는 부적절한 형식이 입력되면 정의되지 않은 동작 가능성이 있어 세 후보
  중 실제 보안 검토 우선순위가 가장 높다. 공격 조건은 해당 옵션과 조작된
  출력 파일명을 사용할 수 있어야 한다.

### BAT-7E6A7DCB5C894DC0A989 — 자동 판정: INCONCLUSIVE

- Focus: `containers/net/net_sockets_common.c::vc_container_net_open`
- 자동 가설: `p->ai_addrlen` 크기를 그대로 사용한 `memcpy`의 목적지 범위
  초과 가능성(HIGH)
- Evidence: `EVD-23C43AD15AADF6213B7C`, 135~276행
- Proof: required 2, supported 0, refuted 0, unknown 2
- Context Retriever: 타입 정의 요청 1회 승인, 추가 LLM 호출 없음
- Challenger: 반례를 생성하지 않음
- Judge: `OBLIGATIONS_UNKNOWN`
- 수동 검토: 목적지는 `sockaddr_storage`를 포함한 union이고 `getaddrinfo`
  결과는 지원되는 IPv4/IPv6 주소 구조이므로 실제 overflow 가능성은 낮아
  보인다. 그러나 분석된 문맥만으로 모든 플랫폼의 `ai_addrlen` 상한을
  형식적으로 증명하지 못했으므로 자동 판정 `INCONCLUSIVE`를 유지한다.

## 토큰과 안전성

- Provider-reported input/output: 32,148 / 1,181 tokens
- Verified source lexical estimate transmitted: 7,887 tokens
- Repository four-Agent source baseline: 2,750,876 tokens
- Selected Batch four-Agent source baseline: 14,852 tokens
- Repository baseline reduction: 98.8314%
- Selected-code baseline reduction: 46.896%
- Cache hits: 0, retries: 0
- TargetCode clean before/after: true/true
- API Key, prompt 본문, source 원문은 실행 artifact에 저장하지 않았다.
