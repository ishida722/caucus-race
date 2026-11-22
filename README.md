# caucus-race

AI エージェントによるスプリント開発のオーケストレーター。人間の関与を「スプリント計画の承認（スパイクへの回答を含む）」と「スプリントレビュー」に限定し、実装・テストは che vuoi（`vuoi exec`）に任せる。

- 仕様: [docs/spec.md](docs/spec.md)
- 設計: [docs/design.md](docs/design.md)

## セットアップ

`caucus` コマンドとして使う場合は、editable モードで uv tool にインストールする。ソースの変更はそのまま反映される。

```bash
uv tool install --editable ~/orca/projects/caucus-race
caucus --help
```

依存パッケージやコマンドの定義（`pyproject.toml`）を変えたときは、入れ直す。

```bash
uv tool install --editable --reinstall ~/orca/projects/caucus-race
```

開発用には `uv sync` してから `uv run caucus ...` でも動く。

データは `~/.local/share/caucus-race/` に保存する（環境変数 `CAUCUS_HOME` で変更できる）。

## 使い方

```bash
# プロジェクトを登録する（実行器に vuoi の dev ワークフロー、プランナーに Claude を使う）
caucus project add myapp --repo ~/projects/myapp -d "記事管理アプリ" \
  --executor vuoi --workflow dev --test-command "uv run pytest" --planner claude

# バックログにストーリーを積む
caucus story add myapp "一覧画面を作る" -a "一覧が新しい順に表示される"

# AI にスプリントを計画させる（ストーリー選定・タスク分解・スパイクの洗い出し）
caucus plan sprint myapp

# AI を使わずに人間が計画する場合
caucus sprint new myapp --goal "一覧画面で記事を探せる"
caucus sprint add myapp ST-1
caucus task add ST-1 "一覧 API を追加する"
caucus spike add ST-1 "ページングは必要か" -o "必要" -o "不要"

# 人間: スパイクに回答して計画を承認する（回答後、AI がタスクを見直す）
caucus spike list myapp
caucus spike answer SP-1 "必要。20 件ずつ"
caucus sprint approve myapp
# 差し戻す場合: caucus sprint reject myapp -m "理由" → caucus plan sprint myapp

# 実行する（caucus/sprint-1 ブランチを作り、ストーリーを順に vuoi に渡す）
caucus sprint start myapp --detach   # バックグラウンドで実行（承認と同時なら sprint approve --start）
caucus sprint logs myapp             # ログの末尾を表示
caucus sprint stop myapp             # 止める
caucus sprint resume myapp --detach  # 途中で止まった場合に再開

# 人間: レビューする
caucus review show myapp     # AI プランナーがあれば確認手順も載る
caucus feedback add myapp "件数も表示したい"
caucus review done myapp     # フィードバックを AI がストーリーに変換してバックログへ

caucus status                # 全プロジェクトの状態と要対応
caucus notifications         # 最近の通知
```

複数のプロジェクトは、それぞれ `--detach` で起動すれば並行に実行される。同じプロジェクトを二重に実行しようとするとエラーになる。

## 通知

人間の対応待ち（計画の確認、スパイクへの回答、スプリントレビュー、実行の停止）を通知する。`~/.local/share/caucus-race/config.toml` で送り先を選ぶ。

```toml
[notify]
desktop = true                                   # macOS の通知センター
command = ["my-notify-script"]                   # 通知の内容を JSON で標準入力に渡して実行する
slack_webhook_env = "CAUCUS_SLACK_WEBHOOK_URL"   # Webhook の URL を入れた環境変数の名前
```

## MCP サーバー

`caucus mcp` で MCP サーバーを stdio で起動する。Claude Code などのエージェントとの対話の中で、バックログの起票とスプリント計画ができる。

MCP の Python SDK（`mcp`）は依存に未追加のため、使う前に `uv add mcp` で追加する。

```bash
claude mcp add caucus-race -- caucus mcp
```

| ツール | 引数 | 内容 |
| --- | --- | --- |
| `add_story` | `project`, `title`, `description`, `acceptance_criteria` | バックログにストーリーを追加する（`caucus story add` 相当） |
| `create_sprint` | `project`, `goal` | スプリントを作成する（`caucus sprint new` 相当） |
| `add_story_to_sprint` | `project`, `story_id` | 計画中のスプリントにストーリーを追加する |
| `remove_story_from_sprint` | `project`, `story_id` | スプリントからストーリーを外してバックログに戻す |
| `add_task` | `story_id`, `title`, `description` | ストーリーにタスクを追加する |
| `add_spike` | `story_id`, `question`, `context`, `options`, `recommendation` | ストーリーにスパイクを起票する |
| `plan_sprint` | `project` | AI プランナーに計画を作らせる（`caucus plan sprint` 相当）。別プロセスで開始してすぐに返る |
| `get_plan_status` | `project` | `plan_sprint` の状態（`not_started` / `running` / `succeeded` / `failed`） |

- AI による計画の作成は数分かかるため、`plan_sprint` は `caucus plan run`（内部用）を別プロセスで起動して、完了を待たずに返る。エージェントは `get_plan_status` で結果を確認する。ログは `CAUCUS_HOME/logs/<project>-plan-*.log` に出る
- 計画の作成中は、そのプロジェクトのスプリント計画を変更するツール（スプリントの作成、ストーリーの追加・除外、タスク・スパイクの追加）はエラーになる
- ルール違反（承認後の計画変更、終了していないスプリントがある状態でのスプリント作成など）は、CLI の「エラー: ...」と同じメッセージがエラーの結果（isError）として返る
- 計画の承認・スパイクへの回答・スプリントの実行は、人間が `caucus` コマンドで行う

## 開発

```bash
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
```
