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

<p align="center"><strong>KiCad 上に構築する、オープンでローカルな agent-safe PCB ランタイム。</strong></p>

PCBDraft は固定基板のテンプレート生成器でも、新しい PCB GUI でもありません。
KiCad を回路図、基板、幾何、DRC、製造出力のバックエンドとして使い、その上に
構造化された設計意図、レビュー可能な回路計画、ローカル部品解決、トランザクション、
失敗証跡、検証ゲートを追加します。

現在の汎用経路は、通常の自然言語による基板要求を受け取り、層数が未指定なら
初期スタックアップを設計判断として自動選択し、明示された正の層数は保持します。
指定部品名を保ったまま、ローカル KiCad ライブラリの実在シンボルとピンで構成された
計画を意味 IR へコンパイルします。ネイティブ回路図を生成し、PCB 配置/配線を有界に
試行します。失敗時には要求、計画、IR、部品証跡、利用可能なネイティブ成果物、
エラーを保持します。

これは任意の基板を量産可能にする自動操縦ではありません。ローカル・ライブラリ抽出は
暫定であり、MPN、データシート制約、調達、物理設計、製造を証明しません。市電、大電力、
DDR/PCIe/SerDes、RF、医療、航空、安全重要用途という語は生成拒否を引き起こしません。
ローカル KiCad ライブラリと実際の配線能力で通常どおり試行し、不足するシンボル、ピン、
ルール、検証証拠はそのまま報告します。

通常の MCU、センサー、コネクタ、レギュレータは、固定デモ部品に置換されません。
名前を保持したまま解決・計画・生成を試み、見つからないシンボル、ピン不整合、
不正な計画、配線不能はその試行の証跡として報告します。

    uv sync --extra dev
    scripts/prepare-kicad-environment.sh
    uv run pcbdraft app --provider codex

<code>--provider builtin</code> はオフラインで要求を整理できますが、回路トポロジーを
捏造しません。計画には Codex または OpenAI-compatible provider を使います。

詳しい最新情報は <a href="README.md">English README</a>、
<a href="README.zh-CN.md">中文 README</a>、
<a href="docs/ARCHITECTURE.md">architecture</a> を参照してください。
以前の RP2040/TMP117 固定製品経路は削除済みです。
