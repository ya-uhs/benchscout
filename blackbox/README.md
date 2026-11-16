# blackbox:別のモデルに、コードを見せずにテストを書かせる

テストを書く人とコードを書く人が同じだと、同じところが抜ける(2026-11-16、Codex の指摘をこちらのテストが見逃した)。
ここでは、別のモデルに**仕様と外から使える入口だけ**を渡してテストを書かせる。そのテストは作ったプログラムを外から動かす。
このファイルは利用者と、コードを読む側(Claude Code)のためのもの。別のモデルには渡さない。

## 仕組み

- プログラムを、利用者が使うのと同じ入口だけで動かす:人の経路(`benchscout core --json` の標準入出力)、エージェントの経路
  (`benchscout mcp` の MCP)、偽の作業台(OS の代わりに読む JSON ファイル)。偽の作業台は、届いたもの(ポートを開いた、書いた、
  測定器に命令した)を `BENCHSCOUT_FIXTURE_LOG` のファイルに書く。本物の機器が感じたはずのことを、外から確かめられる。
- 渡すもの:`TASK.md`(頼む内容)、`CONTRACT.md`(約束の一覧。design.md の要約)、`INTERFACES.md`(入口の説明。MCP の道具の
  説明は、渡すプログラムから取って足す)、`harness.py`(入口を動かす部品。benchscout を import しない)、例のテスト 2 つ、
  design.md、README.md、例の作業台、凍結したコア(`core/`)。**ソースもこちらのテストも渡さない。**
- テストは決定論的に:ランダムを使わず、場合を全部並べる(TASK.md)。数は約束ごとに 1 つの関数、場合は引数にする。

## 手順

1. 今のコードでプログラムを作る:`uv run python packaging/build_core.py`(`vscode/bin/benchscout` にできる)
2. リポジトリの外に渡すフォルダを作る:`uv run python blackbox/export.py <フォルダ>`
   (配布物で試すなら `--vsix benchscout-<target>.vsix`)。フォルダは git の作業場所になり、最初のコミットが入る。
3. そのフォルダで別のモデルを動かし、`TASK.md` のとおりに頼む(Codex なら、そのフォルダで起動して「TASK.md を読んで、その
   とおりに進めて」)。
4. 戻ってきたもの(`git diff` と `FINDINGS.md`)を、コードを読む側が分ける:
   - **コードの不具合**:コードを直す。テストは残す。
   - **仕様があいまい・食い違い**:design.md を直す。方針の判断なら利用者に聞く。
   - **テストの誤り**(仕様の読み違い):理由を書いて直すか外す。
5. 残すテストは `blackbox/tests/` に入れる。CI が、3 OS で開発版に、配布物 4 種で凍結版に対して回す。

手元で回す:`uv run pytest -q blackbox/tests`(開発版)、
`BENCHSCOUT_BIN=$PWD/vscode/bin/benchscout/benchscout uv run --no-project --with pytest pytest -q blackbox/tests`(凍結版)。
`uv run pytest` だけでは回らない(`tests/` だけを見る)。

## 隔離の強さ

- フォルダを分けても、同じ Mac の上なら、別のモデルはリポジトリを探せば読める。TASK.md で禁じ、終わった後にそのモデルの記録で
  読んだファイルを確かめる。
- もっと強くするなら、渡すフォルダだけを Ubuntu の VM に置いてそこで動かす(Linux 版のコアが要る:CI の linux-x64 の .vsix を
  `--vsix` で渡す。VM は arm64 なので、その場合は VM の中で `build_core.py` を回したものを `--core` で渡す)。

## 外から確かめられないもの

VS Code の受信箱の見た目と操作、エージェントを起こす輪(拡張の役目。拡張のテストと e2e が見る)、本物の機器と OS の違い。
