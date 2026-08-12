# Raspberry Pi Userland 저장소 조사

## 1. 조사 기준

| 항목 | 값 |
|---|---|
| TargetCode | `https://github.com/raspberrypi/userland` |
| 로컬 경로 | `target/userland` |
| 기준 브랜치 | `master` |
| 기준 커밋 | `a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976` |
| 마지막 커밋 시각 | `2024-12-23T13:52:07+11:00` |
| 마지막 커밋 제목 | `Fix spelling mistake in man file` |
| 조사일 | 2026-08-10 (Asia/Seoul) |
| TargetCode 변경 여부 | 변경 없음(`git status --short` 출력 없음) |

모든 수치는 위 커밋의 Git 추적 파일을 기준으로 계산했다. 파일 크기는 실제 체크아웃된 파일 크기, LOC는 빈 줄과 주석을 포함한 물리적 줄 수다. 위험 API 수치는 함수 호출 형태의 정규식 후보 수이며 AST로 검증된 취약점 수가 아니다.

## 2. 저장소 성격

README는 이 저장소를 오래되고 폐기된 코드로 명시하며, VideoCore 펌웨어의 독점 API와 연결되는 ARM 측 라이브러리를 포함한다고 설명한다(`target/userland/README.md:4`, `target/userland/README.md:6`, `target/userland/README.md:22`). 최신 Raspberry Pi OS에서는 더 이상 설치되지 않고, 일부 도구는 `raspberrypi/utils`로 이동했다(`target/userland/README.md:11`, `target/userland/README.md:14`).

이 특성은 과제에 적합하다. 오래된 C 코드, 다수의 플랫폼 추상화, 펌웨어 IPC, 멀티미디어 컨테이너 파서, 명령행 도구가 혼재해 대규모 문맥 탐색과 오탐 억제 문제가 동시에 나타난다.

## 3. 정량 규모

### 3.1 전체 파일

| 지표 | 값 |
|---|---:|
| Git 추적 파일 | 830 |
| 추적 파일 크기 | 39.85 MiB |
| C/C++/Assembly 계열 파일 | 655 |
| C/C++/Assembly 물리 LOC | 236,045 |
| `CMakeLists.txt` | 64 |
| CMake toolchain 파일 | 3 |

39.85 MiB 중 약 31 MiB는 `host_applications/linux/apps/hello_pi/hello_video/test.h264` 파일이다. 이와 같은 미디어·바이너리 자산은 소스 분석에서 즉시 제외해야 한다.

### 3.2 언어별 구성

| 확장자 | 파일 수 | 물리 LOC | 용도 |
|---|---:|---:|---|
| `.c` | 284 | 157,220 | 주 구현 언어 |
| `.h` | 367 | 75,273 | API, 매크로, 타입, 인라인 구현 |
| `.cpp` | 3 | 3,323 | 일부 테스트/도구 |
| `.s` | 1 | 229 | Assembly |
| 기타 | 175 | 측정 제외 | CMake, 문서, QPU assembly, 펌웨어 데이터, 미디어 등 |

분석의 중심 언어는 C다. 헤더는 독립 분석 배치의 중심으로 사용하지 않되, 타입·매크로·인라인 함수와 조건부 컴파일을 판정하는 문맥으로 반드시 인덱싱한다.

### 3.3 상위 디렉터리 분포

| 경로 | 추적 파일 | 크기 | C/C++ 계열 LOC | 주요 역할 |
|---|---:|---:|---:|---|
| `interface/` | 388 | 4.54 MiB | 125,149 | Khronos, MMAL, VCOS, VCHIQ, VMCS 인터페이스 |
| `host_applications/` | 244 | 33.08 MiB | 49,964 | Linux/Android 앱, 카메라·진단·예제 도구 |
| `containers/` | 124 | 1.66 MiB | 44,747 | MP4, MKV, RTSP, RTP 등 컨테이너 파서와 I/O |
| `opensrc/` | 14 | 0.15 MiB | 5,527 | 번들 오픈소스 `libfdt` |
| `helpers/` | 9 | 0.16 MiB | 4,618 | Device Tree overlay 처리 |
| `middleware/` | 21 | 0.11 MiB | 2,880 | OpenMAX IL 등 미들웨어 |
| `vcfw/` | 5 | 0.09 MiB | 2,649 | 펌웨어 측/공유 코드 |

가장 큰 구현 파일은 `interface/khronos/glxx/glxx_client.c`(5,734줄), `interface/khronos/vg/vg_client.c`(5,666줄), `helpers/dtoverlay/dtoverlay.c`(2,992줄), `host_applications/linux/apps/raspicam/RaspiVid.c`(2,962줄)다. 파일 단위 LLM 입력이 비효율적이며 함수·심볼 단위 분할이 필수다.

## 4. 빌드 구조

루트 프로젝트는 CMake 2.8을 최소 버전으로 선언한다(`target/userland/CMakeLists.txt:1`). `buildme` 스크립트가 다음 세 경로를 제공한다.

- Raspberry Pi에서의 native 빌드(`buildme:20-34`)
- 호스트 native 빌드의 `--native` 모드(`buildme:35-41`)
- ARM 32/64비트 cross compile(`buildme:42-51`)

README도 CMake와 ARM cross compiler를 요구하며 64비트 지원은 공식적이지 않다고 설명한다(`target/userland/README.md:26`, `target/userland/README.md:29`). 루트 CMake는 ARM64 여부에 따라 MMAL과 관련 앱을 제외하고(`target/userland/CMakeLists.txt:12-16`), VCOS, VMCS Host, VCHIQ, Khronos, MMAL, containers, OpenMAX IL, Linux 앱, `libfdt`, `dtoverlay`를 하위 프로젝트로 연결한다(`target/userland/CMakeLists.txt:66-86`, `target/userland/CMakeLists.txt:114-116`).

현재 환경 조사 결과:

| 도구 | 상태 |
|---|---|
| CMake | 기본 PATH에서 없음 |
| ARM GCC cross compiler | 기본 PATH에서 없음 |
| Clang | 22.1.8 사용 가능 |
| Python | Codex 번들 Python 3.12.13 사용 가능 |
| `tree_sitter` Python 패키지 | 현재 없음 |
| `tiktoken` Python 패키지 | 현재 없음 |
| `pydantic` Python 패키지 | 사용 가능 |
| CI workflow | 없음(이슈 템플릿만 존재) |

Stage 1에서는 TargetCode 불변 원칙과 cross compiler 부재 때문에 빌드를 실행하지 않았다. Stage 2는 빌드 성공에 의존하지 않는 오류 허용 파서를 기본으로 사용하고, 파싱 가능한 파일만 Clang으로 선택 보강해야 한다.

## 5. 분석 범위 정책

다음 분류는 Stage 1의 경로 기반 휴리스틱이다. Stage 2에서 CMake target membership과 파서 결과로 다시 정제한다.

| 분류 | 파일 | LOC | 기본 정책 |
|---|---:|---:|---|
| Primary source | 217 | 129,692 | Chunk/Batch 후보로 분석 |
| Header context | 330 | 66,888 | 타입·매크로·인라인 문맥으로 인덱싱 |
| Example/demo | 50 | 16,672 | 기본 순위 제외, 의존 문맥에는 허용 |
| Test | 31 | 12,792 | 기본 순위 제외, 입력 형식·호출 예시로만 검색 |
| Bundled open source | 13 | 5,527 | 기본 순위 제외, 경계를 넘는 데이터 흐름 시 포함 |
| Firmware-side | 5 | 2,649 | 기본 순위 제외, host 경계 모델링에 활용 |
| Android-specific | 9 | 1,825 | Linux 중심 1차 분석에서 제외, 별도 프로필로 보존 |

### 5.1 기본 분석 포함

- `containers/`의 파서, I/O, 네트워크 코드(단 `containers/test/` 제외)
- `helpers/dtoverlay/`
- `host_applications/linux/`의 실제 앱과 라이브러리(예제·테스트 제외)
- `interface/khronos/`, `interface/mmal/`, `interface/vchiq_arm/`, `interface/vmcs_host/`, `interface/vcos/`의 host 측 구현
- 루트 빌드에 연결된 `middleware/openmaxil/`
- 위 코드가 참조하는 헤더, 타입, 매크로, 인라인 함수

### 5.2 기본 분석 순위에서 제외

- `test`, `tests`, `test_apps`, `*_test.*`, `test*.*`
- `host_applications/linux/apps/hello_pi/`와 example/demo 경로
- `opensrc/`의 번들 외부 코드
- `vcfw/` 펌웨어 측 코드
- Android 전용 코드
- `.h264`, `.hex`, `.raw`, `.dat`, `.ttf`, `.qasm`, `.qinc` 등 비-C/C++ 자산
- 생성물과 향후 `build/` 아래 파일

제외는 삭제나 완전한 무시를 의미하지 않는다. 기본 위험 순위와 세 배치 선정에서 제외하되, 호출·타입·데이터 흐름의 증거가 해당 경계를 넘으면 `REQUEST_CONTEXT`로 가져올 수 있다.

## 6. 위험 API 후보 분포

### 6.1 전체 C/C++ 후보

| 분류 | 호출 형태 출현 | 고유 파일 | 대표 API |
|---|---:|---:|---|
| Raw memory | 534 | 164 | `memcpy`, `memmove`, `memset`, `memcmp` |
| Allocation/lifetime | 387 | 90 | `malloc`, `calloc`, `realloc`, `free` |
| Unbounded/parse string | 153 | 38 | `strcpy`, `strcat`, `sprintf`, `sscanf` |
| Bounded string | 134 | 42 | `snprintf`, `strncpy`, `strncat`, `vsnprintf` |
| File/path | 74 | 48 | `fopen`, `open`, `read` |
| Integer conversion | 53 | 20 | `atoi`, `strtol`, `strtoul` |
| Network | 25 | 4 | `socket`, `bind`, `accept`, `recv`, `send` |
| Command/process | 1 | 1 | `system` |

전체 1,361개 호출 형태 후보 중 `memcpy` 204회, `strcpy` 23회, `sprintf` 10회, `sscanf` 116회가 관찰됐다. `sscanf`와 bounded API도 포맷 폭, 종료 문자, 반환값 검사에 따라 안전할 수 있으므로 모두 취약점이 아니라 검토 트리거다.

### 6.2 Primary source만 적용한 후보

| 분류 | 호출 형태 출현 | 고유 파일 |
|---|---:|---:|
| Raw memory | 392 | 124 |
| Allocation/lifetime | 316 | 70 |
| Bounded string | 129 | 38 |
| Unbounded/parse string | 119 | 27 |
| File/path | 39 | 25 |
| Integer conversion | 29 | 13 |
| Network | 24 | 3 |
| Command/process | 1 | 1 |

Primary source에서는 총 1,049개 후보가 141개 파일에 분포한다. Stage 2의 AST/구문 분석은 선언·주석·래퍼를 제거하고 실제 호출, 인자, 주변 guard, source/sink 관계를 붙여야 한다.

### 6.3 우선 조사 hotspot

| 파일 | 후보 출현 | 관찰된 범주 |
|---|---:|---|
| `helpers/dtoverlay/dtoverlay.c` | 68 | 문자열, 메모리, 파일, 정수 변환, 할당 |
| `containers/rtsp/rtsp_reader.c` | 48 | 네트워크 인접 파서, 문자열, 메모리, 할당 |
| `host_applications/linux/libs/sm/user-vcsm.c` | 44 | 메모리, 할당, 파일 |
| `host_applications/linux/apps/raspicam/RaspiStill.c` | 40 | 문자열, 메모리, 파일, 할당 |
| `host_applications/linux/apps/raspicam/RaspiVid.c` | 33 | 문자열, 네트워크, 메모리, 파일, 할당 |
| `containers/simple/simple_reader.c` | 22 | 문자열 파싱, 메모리, 할당 |
| `containers/net/net_sockets_common.c` | 21 | 네트워크, 메모리, 할당 |

`host_applications/linux/apps/dtoverlay/utils.c:268`에는 동적으로 조립된 명령을 `system()`에 전달하는 호출이 한 곳 있다. Stage 1에서는 취약점으로 판정하지 않으며, Stage 2에서 입력 기원, 명령 조립, 권한, 호출 가능성을 함께 추적할 고우선 후보로만 표시한다.

## 7. 초기 CWE 초점

실제 Batch 선정 전까지 CWE를 확정하지 않지만, 저장소 특성상 다음 proof obligation 템플릿을 우선 준비한다.

1. 메모리 경계: CWE-120/121/122/125/787
2. 형식 문자열: CWE-134
3. 정수 변환과 크기 계산: CWE-190/191/681
4. 명령 실행: CWE-78
5. 파일·경로 처리: CWE-22/73
6. 자원 수명: CWE-401/415/416
7. 네트워크 파서의 입력 검증: 관련 CWE를 실제 source/sink에 따라 선택

## 8. 조사 한계

- 위험 API 분포는 lexical 후보이며 데이터 흐름이나 실제 취약성을 증명하지 않는다.
- 함수 수와 call graph는 아직 정확히 산출하지 않았다. 이는 Stage 2 파서의 검증 대상이다.
- CMake configure/build를 실행하지 않아 실제 활성 target과 조건부 컴파일 결과가 완전히 확정되지 않았다.
- proprietary API와 누락된 바이너리 소스 때문에 전체 프로그램 의미를 복원할 수 없는 경로가 존재할 수 있다.
- Linux/ARM 환경과 Windows 조사 환경의 전처리·헤더 차이를 고려해야 한다.

이 한계를 숨기지 않고 `INCONCLUSIVE` 상태와 증거 기반 문맥 확장으로 처리한다.
