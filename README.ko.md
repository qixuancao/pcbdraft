<p align="center">
  <img src="docs/assets/brand/copperwright-mark-256.png" width="180" alt="CopperWright 구리색 PCB 트레이스 W 마크">
</p>

<h1 align="center">CopperWright</h1>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a>
</p>

<p align="center"><strong>증거에 기반한 KiCad용 PCB 자동화.</strong></p>

CopperWright는 Apache-2.0 라이선스의 모델 독립적 런타임으로, 범위가 명확한
전자 설계 요구사항을 검토하고 검증할 수 있으며 되돌릴 수 있는 KiCad 프로젝트로
변환합니다.
회로도/PCB, 형상 처리, 규칙 검사, 제조 백엔드는 계속 KiCad가 담당하며,
CopperWright는 여기에 시맨틱 의도, 신뢰 부품 계약, 트랜잭션, 결정론적 알고리즘,
증거 게이트, Agent용 API를 추가합니다.

저장소에 포함된 인수 설계는 실제로 라우팅된 ATtiny402/TMP102 컨트롤러입니다.
KiCad ERC와 DRC를 위반 없이 통과하고 제조 후보도 재현 가능하지만, 의도적으로
프로덕션 준비 완료라고 부르지 않습니다. 자격을 갖춘 엔지니어의 검토, 실시간 조달,
제조, 보드 기동, EMC, 물리적 실측 결과는 여전히 외부 게이트입니다.

## 구현된 기능

- 타입이 지정된 인터페이스, 전원 도메인, 요구사항, 블록, 제약조건, 분석, 위험,
  출처를 포함하는 엄격한 시맨틱 회로/PCB IR.
- 입력 순서와 무관한 결정론적 정규 JSON 및 콘텐츠 해시.
- 제조사/MPN, 핀, 심볼, 풋프린트, 정격, 수명 주기/출처 증거, 조달 상태, 제조 계약,
  사용 가능한 모델을 연결하는 CC0 신뢰 부품 그래프.
- 선언된 부품, 포트, 증거, 테스트 참조를 결정론적 구현과 대조하는 버전 관리 및
  규칙 검증이 완료된 재사용 블록.
- 전제조건, 미리보기, 시맨틱 diff, 원자적 게시, 멱등성, 충돌 감지, 백업, 실행 취소,
  크래시 복구를 지원하는 시맨틱 변경 세트.
- 요구사항 컴파일, 제한된 배치 최적화, 결정론적 다층 A* 라우팅, 미세 피치 이스케이프
  라우팅, 채움 기준면, 결정론적 스티칭 비아, 네이티브 KiCad 생성.
- 인식된 풋프린트 위치/자세 편집을 위한 양방향 KiCad 동기화. 토폴로지, 부품, 배선,
  회로도, 규칙 드리프트는 안전하게 거부됩니다.
- L0–L7 검증 상태를 정직하게 표현: `completed`, `not_applicable`, `unavailable`,
  `heuristic`, `human_required`.
- 실제 KiCad ERC/DRC, 회로도 일치성, BOM, 배치, Gerber, 드릴, IPC-D-356,
  보드 통계, PDF, SVG, 렌더링, 보드 전용 STEP 통합.
- 바이트 단위로 재현 가능한 콘텐츠 릴리스, 원본 해시를 감사 영수증에 보존하는
  타임스탬프 정규화, 결정론적 ZIP, 오프라인 검증기.
- 버전이 지정된 CLI, Python API, 크기가 제한된 줄 단위 JSON-RPC 2.0 API.
- 탐지, 오탐, 복구, 회귀, 반복성, 지연 시간, 선택적 블라인드 모델 지표를 측정하는
  90개 사례의 독립 CC0 오류 주입 코퍼스.
- 기존 검토기/안전 패치 워크플로도 비관리 프로젝트에서 사용할 수 있습니다.
  단, 원시 텍스트 치환은 레거시 호환 경로이며 기본 변경 모델이 아닙니다.

요구사항, 구현, 테스트 간 매핑은
[명세 추적표](docs/SPEC_TRACEABILITY.md)를 참고하십시오. 실제 제공된 검증 결과와
남아 있는 외부 게이트는 [최종 중국어 보고서](docs/FINAL_REPORT_ZH.md)에 기록되어
있습니다.

## 지원 범위

내장 생성기 프로필은 의도적으로 좁고 명확하게 제한되어 있습니다.

| 계약 | 현재 지원 |
|---|---|
| 프로필 | `low_voltage_i2c_controller_v1` |
| 회로 | 외부 안정화 3.3 V ATtiny402 + TMP102 + I2C/Qwiic + UPDI + LED |
| 구리 적층 | 2층 또는 4층 |
| 용도 | 프로토타입 또는 안전 필수 요소가 아닌 저전압 센싱/제어 |
| KiCad | 메이저 10, 정확한 인수 테스트 버전은 10.0.5 |
| Python | 3.11+ |

SPI, UART, 기본 USB 2.0, LDO, 단순 buck은 정책 도메인으로 인식되지만 아직 내장
생성 프로필이 없습니다. 이를 지정한 요청은 I2C 픽스처로 몰래 대체되지 않고 생성
전에 거부됩니다. DDR, PCIe, SerDes, RF, 주전원, 고전력, 의료, 항공 및 안전 필수
작업은 자동 범위 게이트에서 명시적으로 거부됩니다.

테스트 호스트에서 KiCad 자체는 KiCad 10 Python API로 생성한 홀수 3구리층 보드를
다시 불러올 수 없습니다. 따라서 네이티브 계약은 분석의 2–4층 목표 중 일반적인
2/4층 하위 범위를 사용합니다.

## 요구 사항

- Linux 및 `uv`
- Python 3.11 이상
- KiCad 10.x CLI, 심볼, 풋프린트 및 시스템 `pcbnew` Python 바인딩
- 진단 및 개발용 Git
- 선택 사항: `review`, 레거시 `patch`, 라이브 모델 일관성 벤치마크에 사용할
  인증된 Codex CLI

정확하게 로컬 인수를 완료한 버전은 KiCad 10.0.5입니다. 다른 10.x 버전은 같은
메이저 버전으로 보고하지만 정확한 테스트를 거친 것으로 간주하지 않으며, 다른
메이저 버전은 안전하게 거부됩니다. Ubuntu 사용자는 KiCad 공식
`ppa:kicad/kicad-10.0-releases` 지침을 사용할 수 있습니다.

## 설치

저장소 체크아웃에서 설치:

```bash
scripts/deploy.sh
uv run copperwright doctor --json
```

빌드된 wheel에서 격리 설치:

```bash
uv build
uv venv /tmp/copperwright-venv
uv pip install --python /tmp/copperwright-venv/bin/python dist/*.whl
/tmp/copperwright-venv/bin/copperwright --version
```

`doctor.ok`는 결정론적 코어를 사용할 수 있음을 뜻합니다. Codex 사용 가능 여부는
`ai_review_available`로 별도 보고됩니다. 생성, 검증, 릴리스, 확인 또는 결정론적
벤치마크에는 유료 또는 비공개 자격 증명이 필요하지 않습니다.

## 엔드투엔드 빠른 시작

모든 출력 경로는 새로 만들기 전용입니다. 새 경로를 사용하거나 이전의 일회성 출력을
직접 제거하십시오.

```bash
copperwright compile \
  examples/attiny_sensor_controller/requirements.json \
  --output /tmp/controller.pcbir.json --json

copperwright generate \
  examples/attiny_sensor_controller/requirements.json \
  /tmp/controller --json

copperwright inspect /tmp/controller --json
copperwright validate /tmp/controller --output /tmp/controller-validation --json
copperwright release /tmp/controller /tmp/controller-release --json
copperwright release-verify /tmp/controller-release --json
```

생성된 프로젝트에는 원본 요구사항, 시맨틱 IR, 네이티브
`.kicad_sch/.kicad_pcb/.kicad_pro`, 격리 워커 영수증, 시맨틱 스냅샷, 네이티브
패드 가장자리 제약 측정값, 라우팅/기준면 증거, 해시 매니페스트가 포함됩니다.
릴리스에는 교차 확인된 제조 파일, 정규화된 검증 증거, 실행 영수증, 콘텐츠
매니페스트, 결정론적 ZIP이 포함됩니다.

커밋된 참조 출력:

- [`examples/attiny_sensor_controller`](examples/attiny_sensor_controller)
- [`artifacts/acceptance/release`](artifacts/acceptance/release)
- [`artifacts/acceptance/review`](artifacts/acceptance/review)
- [`artifacts/benchmark/benchmark-20260812.json`](artifacts/benchmark/benchmark-20260812.json)

## 시맨틱 트랜잭션

Agent는 타입이 지정된 `pcb-agent-change-set`을 출력한 뒤 KiCad 텍스트를 직접
편집하지 않고 트랜잭션 명령을 사용해야 합니다.

```bash
copperwright semantic-preview design.pcbir.json change-set.json --output /tmp/tx
copperwright semantic-apply /tmp/tx
copperwright semantic-undo /tmp/tx
copperwright semantic-recover /tmp/tx
```

작업은 요구사항, 부품, 네트/엔드포인트, 제약조건, 보드 규칙, 메타데이터를
포괄합니다. 각 작업에는 이유가 포함되며 필드 수준의 예상값도 지정할 수 있습니다.
런타임은 기준 해시를 확인하고 모든 작업을 메모리에서 적용한 뒤 결과 IR을 검증하고
시맨틱 diff를 작성한 다음에만 스테이징을 만듭니다. 게시할 때는 리소스 잠금 아래에서
원본 및 스테이징 해시를 다시 확인합니다.

검토된 네이티브 KiCad 풋프린트 이동을 가져오려면:

```bash
copperwright sync /tmp/controller --json
copperwright sync /tmp/controller --apply --json
copperwright sync-undo /tmp/.pcb-agent-transactions/sync-...
```

위치/자세 변경만 가져옵니다. 알 수 없는 보드 바이트, 풋프린트 변경, 부품 추가/제거,
배선, 네트 매핑, 회로도 변경, 프로젝트 규칙 변경은 유실시키지 않고 거부합니다.

## 검증 및 증거

검증 단계는 검사별, 레벨별로 보고됩니다.

| 레벨 | 런타임 증거 |
|---|---|
| L0 | 매니페스트/해시 무결성, 시맨틱 파싱, 네이티브 KiCad 파싱 |
| L1 | 정규 부품, 핀, 풋프린트/패드, 연결성, 회로도/PCB 일치성 |
| L2 | 실제 KiCad ERC 및 DRC 보고서 |
| L3 | 인터페이스, 디커플링, 풀업, 전류, 배치, 라우팅, 의도 규칙 |
| L4 | 수명 주기/BOM/제조 계약, DFM 프록시, 릴리스 교차 확인, 외부 조달/PCB 제조 업체 증거 |
| L5 | 적용 가능한 결정론적 DC/전력 검사. 증거가 없으면 SI/PI/열/EMI 사용 불가 |
| L6 | 외부 증거로 가져온 귀속 정보가 있는 적격 엔지니어 검토 |
| L7 | 외부 증거로 가져온 귀속 정보가 있는 보드 일련번호/테스트 계획/결과 아티팩트 |

후보 준비 완료 상태가 되려면 로컬에서 구현 가능한 모든 차단 게이트를 통과해야
합니다. 프로덕션 준비 완료에는 유효한 L4 조달/PCB 제조 업체 정보, L6 검토, L7 물리적
증거가 추가로 필요합니다. 런타임은 제공된 증거를 복사하고 해시하지만
`externally_supplied_not_independently_verified`로 표시하며, 스스로 서명하지
않습니다.

내장 프로필의 전력 범위는 하나의 동시 최댓값 계약입니다
(`3.465 V × 0.1 A = 0.3465 W`, 소스 상한에 +5%를 적용하면서 센서의 3.6 V 동작
한계보다 낮게 여유 확보). I2C는 200 pF와 4.7 kOhm 풀업으로 제한되며 외부 풀업은
허용하지 않습니다. UPDI VTREF는 감지 전용이고, 디커플링 거리는 관련 네이티브 구리
패드 사각형 사이에서 측정합니다. I2C 라우팅 계약에는 채워진 GND 기준면과 최소 두
개의 결정론적 GND 스티칭 비아가 필요합니다.

관리 프로젝트 검토에는 엄격하게 파싱된 요구사항, IR, 신뢰 부품 및 블록 레코드,
생성 영수증, 네이티브 시맨틱 내보내기가 제공됩니다. 추적 파일에 드리프트가 있으면
해당 의도 레코드를 몰래 사용하지 않고 비권위 데이터로 표시합니다. 모델 응답은
여전히 휴리스틱 검토이며 L6를 충족할 수 없습니다.

## CLI 및 Agent API

권위 있는 CLI는 `copperwright --help`와
`copperwright COMMAND --help`에서 확인할 수 있습니다. 주요 명령 그룹:

- 설계: `compile`, `generate`, `inspect`, `parts`
- 검증/릴리스: `validate`, `release`, `release-verify`, `evidence-record`
- 동기화/트랜잭션: `sync`, `sync-undo`, `sync-recover`, `semantic-preview`,
  `semantic-apply`, `semantic-undo`, `semantic-recover`
- 평가: `benchmark`
- 비관리 호환: `review`, `patch`, `apply`
- 자동화: `api`

API는 stdin에서 한 줄에 하나의 JSON-RPC 요청을 읽고 stdout에 한 줄에 하나의
응답을 씁니다. 먼저 `runtime.capabilities`를 사용하십시오. 이는 지원 범위와
메서드를 나타내는 기계 판독 가능 정보원입니다.

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"runtime.capabilities","params":{}}' \
  | copperwright api
```

프로세스는 최대 10,000개 요청을 받고 요청당 최대 크기는 4 MiB입니다. 매개변수
집합은 정확해야 하며, 경로와 수치 범위를 검증하고, 프로토콜 오류에서도 JSON-RPC
프레이밍을 유지합니다. [API 참조](docs/API.md)를 확인하십시오.

## 벤치마크

모델이나 네트워크 없이 결정론적 코퍼스를 실행할 수 있습니다.

```bash
scripts/benchmark.sh
# or
copperwright benchmark /tmp/copperwright-benchmark.json --repetitions 5 --json
```

명시적으로 요청한 라이브 모델 일관성 실행은 블라인드 처리하고 격리한 반복을 두 번
이상 수행합니다.

```bash
MODEL_RUNS=2 scripts/benchmark.sh
```

현재 측정 결과와 한계는 [BENCHMARK.md](BENCHMARK.md)에 있습니다. 이 벤치마크는
회귀 코퍼스이며 모든 PCB 오류를 포괄한다는 주장이 아닙니다.

## 호환 이름

배포 패키지와 기본 명령은 `copperwright`입니다. 설치되는 `pcb-agent` 명령은
동일한 기능의 호환 별칭으로 유지됩니다. 위험만 늘리고 실질적 가치가 없는 모듈
마이그레이션을 피하기 위해 내부 Python 모듈도 `pcb_agent`를 유지합니다.

안정적인 디스크 및 프로토콜 식별자도 변경하지 않습니다. 여기에는
`pcb-agent-*` schema, `project.pcb-agent.json`, `.pcb-agent-*` 트랜잭션/잠금
디렉터리, `PCB_AGENT_*` 테스트/설정 네임스페이스가 포함됩니다. 이름 변경 전에
생성되어 커밋된 엔지니어링 영수증과 벤치마크 아티팩트는 기록된
`pcb-agent-runtime`을 그대로 유지합니다. CopperWright는 과거 증거를 더 최신인
것처럼 보이게 만들기 위해 다시 작성하지 않습니다.

## 개발 및 릴리스 검사

```bash
scripts/test.sh
scripts/smoke.sh                 # real KiCad demo; no model by default
scripts/compatibility.sh         # Python 3.11–3.14 core matrix
scripts/release-check.sh         # tests, wheel/sdist, clean install, E2E release
```

호환되는 로컬 툴체인이 있으면 `scripts/test.sh`가 실제 KiCad 테스트를 자동으로
실행하고, 없으면 `unittest`의 skip으로 기록합니다. CI 정의는
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)에 있습니다. 코퍼스 및
프로필 기여 규칙은 [개발 가이드](docs/DEVELOPMENT.md)를 참고하십시오.

## 보안 모델

프로젝트 콘텐츠, 모델 출력, 메타데이터, 아카이브, 파일 이름은 신뢰하지 않습니다.
런타임은 엄격한 schema, 바이트/멤버/깊이 제한, 비유한 수 거부, 비로그인 하위
프로세스, 시간/출력 제한, 새로 만들기 전용 출력, 정규 경로, 심볼릭 링크/하드
링크/특수 파일 거부, 파일 매니페스트, 리소스 잠금, 원자적 쓰기, 쓰기 후 검증을
사용합니다.

격리된 `pcbnew` 워커는 내부에서 생성한 크기 제한 JSON 작업을 받고 시스템 Python을
`-I`로 실행하며 프로젝트 코드를 import하지 않습니다. Codex 검토는 읽기 전용 도구
정책을 사용하고 프로젝트 설정, hooks, multi-agent, 네트워크, 권한 도구를 비활성화한
뒤 stdin으로 프롬프트를 전달합니다. 이 정책은 OS 샌드박스가 아닙니다. 신뢰할 수
없는 프로젝트는 컨테이너/VM에서 실행하고 공개 권한이 있는 데이터만 전송하십시오.
[SECURITY.md](SECURITY.md)를 참고하십시오.

## 라이선스

런타임 소스와 문서는 Apache-2.0이며 [LICENSE](LICENSE)를 참고하십시오. 내장
부품/블록 카탈로그와 독립 벤치마크 데이터는
[`src/pcb_agent/data/LICENSE.md`](src/pcb_agent/data/LICENSE.md)에 설명된 대로
CC0-1.0입니다. 생성된 예제 설계는 KiCad 라이브러리의
CC-BY-SA 4.0 design exception에 따라 공식 KiCad 라이브러리 자료를 사용합니다.
종속성 및 저작자 표시 정보는 [NOTICE](NOTICE)에 있습니다.

어떠한 보증이나 엔지니어링 인증도 제공하지 않습니다. 제품, 관할권, 위험 수준에
맞는 적격 검토를 반드시 수행하십시오.
