# caucus-race

AI エージェントによるスプリント開発のオーケストレーター。人間の関与を「スプリント計画の承認（スパイクへの回答を含む）」と「スプリントレビュー」に限定し、実装・テストは che vuoi（`vuoi exec`）に任せる。

- 仕様: [docs/spec.md](docs/spec.md)
- 設計: [docs/design.md](docs/design.md)

## セットアップ

```bash
uv sync
uv run caucus --help
```

データは `~/.local/share/caucus-race/` に保存する（環境変数 `CAUCUS_HOME` で変更できる）。

## 使い方

```bash
# プロジェクトを登録する（実行器に vuoi の dev ワークフローを使う）
caucus project add myapp --repo ~/projects/myapp --executor vuoi --workflow dev \
  --test-command "uv run pytest"

# バックログにストーリーを積む
caucus story add myapp "一覧画面を作る" -a "一覧が新しい順に表示される"

# スプリントを計画する
caucus sprint new myapp --goal "一覧画面で記事を探せる"
caucus sprint add myapp ST-1
caucus task add ST-1 "一覧 API を追加する"
caucus spike add ST-1 "ページングは必要か" -o "必要" -o "不要"

# 人間: スパイクに回答して計画を承認する
caucus spike answer SP-1 "必要。20 件ずつ"
caucus sprint approve myapp

# 実行する（caucus/sprint-1 ブランチを作り、ストーリーを順に vuoi に渡す）
caucus sprint start myapp
caucus sprint resume myapp   # 途中で止まった場合

# 人間: レビューする
caucus review show myapp
caucus feedback add myapp "件数も表示したい"
caucus review done myapp     # フィードバックは次のバックログに入る

caucus status                # 全プロジェクトの状態と要対応
```

## 開発

```bash
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
```
