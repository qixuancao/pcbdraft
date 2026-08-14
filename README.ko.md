<p align="center">
  <img src="docs/assets/brand/pcbdraft-mark-256.png" width="180" alt="PCBDraft mark">
</p>

<h1 align="center">PCBDraft</h1>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a>
</p>

<p align="center"><strong>KiCad 위에 구축한 오픈 소스·로컬 agent-safe PCB 런타임.</strong></p>

PCBDraft는 고정 보드 템플릿 생성기도, 새로운 PCB GUI도 아닙니다. KiCad를
회로도, PCB, 기하, DRC, 제조 출력 백엔드로 사용하고 그 위에 구조화된 설계 의도,
검토 가능한 회로 계획, 로컬 부품 해석, 트랜잭션, 실패 증거, 검증 게이트를 더합니다.

현재 일반 경로는 자연어 기판 요구를 받아 층 수가 지정되지 않으면 초기 스택업을
설계 판단으로 자동 선택하고, 사용자가 명시한 양의 층 수는 그대로 보존합니다.
지정한 부품 이름과 로컬 KiCad 라이브러리에 실제로 있는 심볼·핀으로 계획을 구성해
의미 IR로 컴파일하고, 네이티브 회로도를 만든 다음 제한된 PCB 배치/배선을 시도합니다.
실패하면 요구, 계획, IR, 부품 출처, 생성된 네이티브 파일, 구체적인 오류를 보존합니다.

이는 모든 보드를 양산 가능하게 만드는 자동 조종 장치가 아닙니다. 로컬 라이브러리
추출은 잠정적이며 MPN, 데이터시트 제약, 소싱, 물리 설계, 제조성을 증명하지 않습니다.
주전원, 고전력, DDR/PCIe/SerDes, RF, 의료, 항공, 안전 핵심이라는 말은 생성 거부를
유발하지 않습니다. 로컬 KiCad 라이브러리와 실제 배선 능력으로 정상적으로 시도하며,
부족한 심볼, 핀, 규칙, 검증 증거는 그대로 보고합니다.

일반 MCU, 센서, 커넥터, 레귤레이터 요청은 고정 데모 부품으로 대체하지 않습니다.
이름을 보존한 채 해석·계획·생성을 시도하고, 심볼 부재, 핀 불일치, 잘못된 계획,
배선 불가는 해당 시도의 증거로 보고합니다.

    uv sync --extra dev
    scripts/prepare-kicad-environment.sh
    uv run pcbdraft app --provider codex

<code>--provider builtin</code>은 오프라인 요구 정리는 가능하지만 회로 토폴로지를
지어내지 않습니다. 계획 생성에는 Codex 또는 OpenAI-compatible provider를 사용하세요.

자세한 최신 내용은 <a href="README.md">English README</a>、
<a href="README.zh-CN.md">中文 README</a>、
<a href="docs/ARCHITECTURE.md">architecture</a>를 참고하세요.
기존 RP2040/TMP117 고정 제품 경로는 삭제되었습니다.
