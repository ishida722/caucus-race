# caucus-race 設計書（ドラフト v0.1）

仕様は `docs/spec.md` を参照する。本書は仕様を実現するための構成・データモデル・状態遷移・外部接続を定める。

## 1. 全体構成

```
           人間（CLI）                         AI
  承認 / スパイク回答 / レビュー        プランナー（claude -p）
            │                                 │
            ▼                                 ▼
 ┌──────────────────────── caucus-race ─────────────────────────┐
 │  interface   : CLI（Typer）                                   │
 │  usecase     : プランニング / 承認 / 実行 / レビュー / レトロ │
 │  domain      : エンティティ・状態遷移・ルール                 │
 │  port        : Repository / Planner / Executor / Notifier     │
 │  adapter     : SQLite / ClaudePlanner / VuoiExecutor / ...    │
 └───────────────────────────────────────────────────────────────┘
            │                                 │
            ▼                                 ▼
         SQLite                     che vuoi（vuoi exec）
                                      └─ 対象リポジトリ
```

- クリーンアーキテクチャで層を分ける。domain は外部に依存しない。
- 外部との接続（DB、AI、実行器、通知）はすべて port（抽象）で定義し、adapter で実装する。DI には `injector` を使う。
- caucus-race 自身は判断を行わない。**判断は AI（Planner）と人間が行い、caucus-race は状態と遷移の正しさを保証する** という役割分担にする。

### 技術スタック（案）

| 項目 | 選定 | 理由 |
|---|---|---|
| 言語 | Python（実装時点の最新安定版） | che vuoi と揃える |
| パッケージ管理 | uv（src レイアウト） | |
| CLI | Typer | |
| 永続化 | SQLite（SQLAlchemy 2.x） | ローカル単一利用者。再開可能性のため全状態を DB に置く |
| モデル | Pydantic v2 | AI の出力（JSON）の検証にも使う |
| DI | injector | |
| テスト | pytest | 実行器・プランナーは偽実装で差し替える |

ライブラリのバージョンは実装時に最新の安定版を確認して固定する。

## 2. ドメインモデル

### 2.1 エンティティ

ID は人間が CLI で扱いやすいよう、種別の接頭辞付きの連番（`PRJ-1`、`ST-12`、`SP-3` など）を表示用に持つ。

**Project**

| 属性 | 型 | 説明 |
|---|---|---|
| id | str | |
| name | str | 一意 |
| repo_path | path | 開発対象リポジトリ |
| description | str | プロジェクトの目的。プランナーがストーリーを提案する際の材料 |
| base_branch | str | スプリント用ブランチの分岐元（既定 `main`） |
| executor_config | json | 実行器の設定（vuoi のワークフロー名、テストコマンドなど） |
| max_stories_per_sprint | int | 1 スプリントのストーリー数の上限（既定 5） |
| created_at / updated_at | datetime | |

**Sprint**

| 属性 | 型 | 説明 |
|---|---|---|
| id | str | |
| project_id | str | |
| number | int | プロジェクト内の連番 |
| goal | str | スプリントゴール |
| status | SprintStatus | §3.1 |
| branch | str? | スプリント用のブランチ名 |
| approved_at / started_at / reviewed_at / closed_at | datetime? | |
| plan_comment | str? | 差し戻し時の人間のコメント |

**Story**

| 属性 | 型 | 説明 |
|---|---|---|
| id | str | |
| project_id | str | |
| sprint_id | str? | null ならバックログ |
| title | str | |
| description | str | |
| acceptance_criteria | list[str] | 受け入れ条件 |
| decisions | list[str] | スパイクの回答などから確定した決定事項。実行器へのブリーフに含める |
| source | StorySource | `human` / `ai` / `feedback` / `executor_proposal` |
| source_ref | str? | 起票元のフィードバック ID など |
| priority | int | バックログ内の並び順 |
| status | StoryStatus | §3.2 |
| result | ExecutionResult? | 最新の実行結果（§6） |
| start_sha | str? | 実行前のスプリント用ブランチの HEAD |
| commits | list[str] | このストーリーで追加されたコミット |
| wip_branch | str? | 失敗・中断時に変更を退避したブランチ |

**Task**

| 属性 | 型 | 説明 |
|---|---|---|
| id | str | |
| story_id | str | |
| title / description | str | |
| order | int | |
| status | `todo` / `done` / `skipped` | 実行器の結果から更新する |

**Spike**

| 属性 | 型 | 説明 |
|---|---|---|
| id | str | |
| story_id | str | |
| question | str | 人間に決めてほしいこと |
| context | str | 判断材料（背景、調べた内容） |
| options | list[str] | AI が提示する選択肢（任意） |
| recommendation | str? | AI の推奨案（任意） |
| answer | str? | 人間の回答 |
| raised_by | `planner` / `executor` | プランニング時か実行中か |
| status | `open` / `answered` / `dropped` | |

**Feedback**

| 属性 | 型 | 説明 |
|---|---|---|
| id | str | |
| sprint_id | str | |
| story_id | str? | 特定のストーリーへの指摘なら設定 |
| body | str | |
| converted_story_id | str? | 変換後のストーリー |

**RetroNote**

| 属性 | 型 | 説明 |
|---|---|---|
| id | str | |
| sprint_id | str | |
| author | `human` / `ai` | |
| body | str | |

**Event**（監査ログ）

| 属性 | 型 | 説明 |
|---|---|---|
| id | int | |
| at | datetime | |
| actor | `human` / `planner` / `executor` / `system` | |
| entity_type / entity_id | str | |
| kind | str | `sprint.approved`、`spike.answered` など |
| payload | json | |

### 2.2 ドメインルール

- R-1: 1 プロジェクトで `active` 状態のスプリントは同時に 1 つまで。
- R-2: スプリントを開始できるのは、`approved` かつ、含まれる全ストーリーの全スパイクが `open` でない場合のみ。
- R-3: スプリントに割り当て済みのストーリーを変更できるのは `planning` 状態の間のみ。承認後に変更する場合は `planning` に戻す（再承認が必要）。
- R-4: スパイクに回答すると、その回答をストーリーの `decisions` に追加する。
- R-5: スプリントを `review` に進められるのは、全ストーリーが終了状態（`done` / `failed` / `suspended`）の場合のみ。
- R-6: スプリントを `closed` にすると、未完了（`failed` / `suspended`）のストーリーはバックログへ戻す（`sprint_id = null`、`status = backlog`）。

## 3. 状態遷移

### 3.1 Sprint

```
 planning ──(AI が計画作成、人間が承認)──▶ approved ──(start)──▶ active
    ▲                                         │                    │
    └────────(差し戻し / 計画変更)────────────┘                    │
                                                    全ストーリー終了 │
                                                                   ▼
                                    closed ◀──(レビュー完了)── review
```

| 遷移 | 操作 | 実行者 | 条件 |
|---|---|---|---|
| (new) → planning | `plan` | AI | アクティブなスプリントがない |
| planning → approved | `sprint approve` | 人間 | ストーリーが 1 件以上ある |
| approved → planning | `sprint reject` / 計画変更 | 人間 | |
| approved → active | `sprint start` | 人間 or system | R-2 |
| active → review | 自動 | system | R-5 |
| review → closed | `review done` | 人間 | |

スパイクへの回答と承認の順序は問わない。承認を先に済ませ、スパイク回答が揃った時点で自動開始する設定（`auto_start`）も持てるようにする。

### 3.2 Story

```
 backlog ──(計画に選定)──▶ planned ──(スプリント開始)──▶ queued ──▶ running ──▶ done
    ▲                         │                                       │
    └──(計画から外す)─────────┘                                       ├──▶ failed
    ▲                                                                 └──▶ suspended
    └──────────────── スプリント終了時（failed / suspended）─────────────────┘
```

- `running`: 実行器に渡している間。
- `done`: 実行器が `done` を返し、テストが通った。
- `failed`: 実行器が `error` を返した、またはリトライ上限に達した。
- `suspended`: 実行中に人間の判断が必要になった（実行器が `blocked` を返した）。スパイクを起票する（`raised_by = executor`）。

### 3.3 Spike

`open` → `answered`（人間が回答） / `dropped`（ストーリーを計画から外した、または不要になった）

## 4. ユースケース

| ユースケース | 実行者 | 主な処理 |
|---|---|---|
| RegisterProject | 人間 | プロジェクトを登録する |
| AddStory | 人間 / AI | バックログにストーリーを追加する |
| CreateSprintPlan | AI | バックログからストーリーを選定し、ゴール・タスク・スパイクを作成する |
| AnswerSpike | 人間 | 回答を記録し、`decisions` に反映。必要なら RefineStory を呼ぶ |
| RefineStory | AI | スパイクの回答を受けてタスクを見直す |
| ApprovePlan / RejectPlan | 人間 | 計画を承認・差し戻す |
| StartSprint | 人間 / system | 開始条件を検証し、オーケストレーターを起動する |
| RunSprint | system | §5 のオーケストレーション |
| BuildReviewReport | system | レビュー資料を生成する |
| AddFeedback | 人間 | フィードバックを登録する |
| ConvertFeedback | AI | フィードバックをストーリーに変換し、バックログへ追加する |
| CloseSprint | 人間 | レビューを完了し、R-6 を適用する |
| AddRetroNote | 人間 / AI | レトロ記録を追加する |

## 5. オーケストレーター

スプリント実行（RunSprint）はプロセスが止まっても再開できるよう、**DB の状態だけを見て次の一手を決めるループ** として実装する。

```python
def run_sprint(sprint_id):
    while True:
        sprint = repo.get_sprint(sprint_id)
        if sprint.status != ACTIVE:
            return
        story = next_story(sprint)          # queued を priority 順に 1 件。running が残っていれば再実行対象
        if story is None:
            transition(sprint, REVIEW)      # R-5
            notifier.notify_review_ready(sprint)
            return
        mark_running(story)
        result = executor.run(build_brief(story), workdir=project.repo_path)
        apply_result(story, result)         # done / failed / suspended への遷移、提案をバックログへ
```

- 実行は直列。並列化する場合はストーリーごとに worktree を分ける必要がある。
- `running` のまま停止していたストーリーは、再開時に作業ツリーの状態を確認してから再実行する（未コミットの変更をどう扱うかは実行器の設計次第）。
- 起動方法: `caucus sprint start` は端末で最後まで実行する。`--detach` を付けると、端末から切り離した別プロセス（`caucus sprint run`）で実行し、出力を `CAUCUS_HOME/logs/<project>-<時刻>.log` に書く。常駐デーモンは置かない。
- 二重実行の防止: 実行プロセスは `CAUCUS_HOME/run/<project>.lock` のロック（flock）を保持する。同じプロジェクトのスプリントを 2 つのプロセスで実行しようとするとエラーになる。別のプロジェクトは別プロセスで並行に実行できる。複数プロセスが同じ SQLite に書き込むため、ロック待ちの時間を 30 秒にしている。
- 停止: `caucus sprint stop` は実行プロセスのプロセスグループ（vuoi・claude を含む）に SIGTERM を送る。実行中だったストーリーは running のまま残り、`caucus sprint resume` で退避・巻き戻してから再実行される。
- 実行プロセスが例外で止まった場合は `runner_error` を通知する。

## 6. 外部接続

### 6.1 Executor（実行器）

```python
class Executor(Protocol):
    def run(self, brief: Brief, workdir: Path) -> ExecutionResult: ...

class Brief(BaseModel):
    story_id: str
    goal: str                    # ストーリーの目的
    decisions: list[str]         # 確定事項（スパイク回答を含む）
    tasks: list[str]
    acceptance_criteria: list[str]
    out_of_scope: list[str]

class ExecutionResult(BaseModel):
    status: Literal["done", "blocked", "error"]
    summary: str
    blocked_reason: str | None
    changed_files: list[str]
    proposals: list[Proposal]    # 範囲外の課題（title / body / kind）→ バックログ候補
    raw: dict                    # 実行器の出力をそのまま保存
```

**VuoiExecutor**（`adapter/executor/vuoi.py`）: `vuoi exec` のインターフェースに合わせる。

1. `Brief` を vuoi のブリーフ形式（目的 / 決定事項 / 作業内容 / 受け入れ条件 / 触らない範囲）の Markdown に変換し、`<git-dir>/caucus/brief-<story>-<時刻>.md` に置く。作業ツリーに置くと変更ファイルとして混ざるためである。
2. 対象リポジトリをカレントにして `vuoi exec <workflow> --brief <file> --json <result> [--test-command ...]` を実行する。標準出力・標準エラーは `<git-dir>/caucus/log-*.txt` に保存する。
3. 結果 JSON（vuoi の `ExecOutcome`）を `ExecutionResult` に写す。`blocked` → `blocked_reason`、`proposals[].{title, body, kind}` → `Proposal`。判定には終了コードではなく JSON の `status` を使う。
4. JSON が読めない、コマンドがない、タイムアウトした場合は `error` とする。
5. `blocked` はスパイク、`proposals` は `source = executor_proposal` のストーリーとしてバックログへ取り込む（オーケストレーターが行う）。

`Project.executor_config` の項目:

| キー | 既定 | 説明 |
|---|---|---|
| `type` | `fake` | `vuoi` で VuoiExecutor を使う |
| `workflow` | `dev` | vuoi のワークフロー名 |
| `test_commands` | `[]` | `--test-command` として渡す。空なら vuoi 側の設定に従う |
| `command` | `vuoi` | vuoi の実行ファイル |
| `timeout_seconds` | 10800 | 1 ストーリーの実行時間の上限 |

ブランチ操作は caucus-race が行う（§6.4）。vuoi のワークフローがコミットすることは許容し、未コミットの変更が残った場合だけ caucus-race がコミットする。

**FakeExecutor**: テスト用。シナリオに従って `done` / `blocked` / `error` を返す。

### 6.2 Planner（AI）

プランニング系の判断（ストーリー選定、タスク分解、スパイク抽出、フィードバック変換）を担う（`port/planner.py`）。

```python
class Planner(Protocol):
    def create_sprint_plan(self, context: PlanningContext) -> SprintPlanDraft: ...
    def refine_story(self, context: PlanningContext, story: StoryView) -> StoryRefinementDraft: ...
    def convert_feedback(self, context: PlanningContext, feedback: list[FeedbackView]) -> FeedbackConversionDraft: ...
```

- `PlanningContext` は、プロジェクトの説明、ストーリー数の上限、バックログ（未解決のスパイク・決定事項を含む）、前スプリントの結果（ストーリーの結果・フィードバック・レトロ記録）を持つ。再計画のときは、現在の計画と差し戻しコメントも持つ。
- プランナーは **下書き（Draft）を返すだけ** で、DB への反映は `AIPlanningService`（`usecase/ai_planning.py`）が行う。下書きは次のように検証してから反映する。
  - バックログにないストーリー ID は無視する。上限を超えたストーリーは先頭から上限までを採用する。採用できるストーリーが 0 件なら何も反映しない
  - バックログのストーリーの未解決のスパイクは引き継ぐ（プランナーには書き直させない）
- プランナーの呼び出しは時間がかかるため、DB のトランザクションの外で行う。

**ClaudePlanner**（`adapter/planner/claude.py`）

- 対象リポジトリをカレントにして `claude -p --output-format json --json-schema <Draft のスキーマ> --tools Read,Glob,Grep --permission-mode dontAsk --no-session-persistence` を実行する。プロンプトは標準入力で渡す。
- 読み取り専用のツールだけを渡し、コードやドキュメントを読んだうえで判断させる。
- 出力の `structured_output`（なければ `result` を JSON として）を Pydantic で検証する。失敗したら再試行する（既定 2 回）。
- プロンプトは `adapter/planner/prompts.py` にある。スパイクの判断基準（spec §5.1.1）は `planner_config.spike_policy` で置き換え、`planner_config.instructions` でプロジェクト固有の指示を追加できる。

`Project.planner_config` の項目:

| キー | 既定 | 説明 |
|---|---|---|
| `type` | `manual` | `claude` で ClaudePlanner を使う。`manual` は AI を使わない（計画は人間が作り、フィードバックは機械的に変換する） |
| `model` | なし | Claude のモデル |
| `tools` | `["Read", "Glob", "Grep"]` | プランナーに渡すツール |
| `timeout_seconds` | 1800 | 1 回の問い合わせの上限 |
| `max_attempts` | 2 | 再試行を含む試行回数 |
| `spike_policy` | なし | スパイクの判断基準を置き換える |
| `instructions` | `""` | プロジェクト固有の追加指示 |

**ユースケースへの組み込み**

| 操作 | 処理 |
|---|---|
| `caucus plan sprint <project>` | 新しいスプリントの計画を作る。差し戻された計画（planning）があれば、コメントに従って作り直す。作り直すときは、現在の計画のストーリーをバックログに戻し、プランナーが起票した未解決のスパイクは取り下げる（回答済みのスパイクと決定事項は残す） |
| `caucus spike answer` | ストーリーの未解決のスパイクがなくなったら、AI がタスクと受け入れ条件を見直す（`--no-refine` で抑止）。見直しは承認後（approved）でも行える。新たなスパイクが出た場合はスプリントを開始できなくなる |
| `caucus review done` | 未変換のフィードバックを AI がストーリーに変換する（複数をまとめたり、1 件を分けたりできる）。AI が失敗した場合や取りこぼした分は、機械的に変換する |

### 6.3 Notifier

人間の対応待ちを知らせる。

```python
class Notifier(Protocol):
    def notify(self, event: HumanActionRequired) -> None: ...
```

| 通知の種類（kind） | 発生する場面 |
|---|---|
| `plan_ready` | AI がスプリント計画を作成した |
| `spike_raised` | 実行中のストーリーが中断した、または見直しで新たなスパイクが出た |
| `review_ready` | 全ストーリーが終わり、レビュー待ちになった |
| `runner_error` | スプリントの実行プロセスが例外で止まった |

送り先（`adapter/notifier/channels.py`）は `CAUCUS_HOME/config.toml` の `[notify]` で選ぶ。標準エラーと履歴（`CAUCUS_HOME/notifications.jsonl`、`caucus notifications` で表示）には常に出す。どの送り先も、失敗してもスプリントの進行を止めない。

```toml
[notify]
desktop = true                          # macOS の通知センター（osascript）
command = ["my-notify-script"]          # 通知の内容を JSON で標準入力に渡して実行する
slack_webhook_env = "CAUCUS_SLACK_WEBHOOK_URL"   # Incoming Webhook の URL を入れた環境変数の名前
```

`command` は、entwurf への起票など任意の連携をつなぐための口として使う。Webhook の URL は秘密情報なので、ファイルには書かず環境変数で渡す。

### 6.4 Workspace（git 操作）

```python
class Workspace(Protocol):
    def is_clean(self, repo: Path) -> bool: ...
    def head(self, repo: Path) -> str: ...
    def create_branch(self, repo: Path, name: str, base: str) -> None: ...
    def checkout(self, repo: Path, name: str) -> None: ...
    def commit_all(self, repo: Path, message: str) -> str | None: ...   # 変更がなければ None
    def save_to_branch(self, repo: Path, name: str) -> None: ...        # 現在の HEAD を name で保存
    def reset_hard(self, repo: Path, sha: str) -> None: ...
    def commits_between(self, repo: Path, base: str, head: str) -> list[str]: ...
```

実装は `git` コマンドを subprocess で呼ぶ（`GitWorkspace`）。オーケストレーターでの使い方は次のとおり。

| 時点 | 処理 |
|---|---|
| スプリント開始 | 作業ツリーがクリーンか確認 → `caucus/sprint-<n>` をベースブランチから作成して checkout。ブランチ名を `Sprint.branch` に保存 |
| ストーリー実行前 | スプリント用ブランチを checkout。HEAD を `Story.start_sha` に保存 |
| 実行後（done） | 未コミットの変更があれば `<story_id>: <title>` でコミット。`start_sha..HEAD` のコミットを `Story.commits` に記録 |
| 実行後（failed / suspended） | 未コミットの変更があれば WIP コミット → HEAD を `caucus/wip/<story_id>` に保存 → `start_sha` に reset |
| 再開時に running が残っていた | 上の failed と同じ手順で退避・reset してから再実行 |

コミットの author は利用者の git 設定を使い、メッセージに `Caucus-Story: <story_id>` トレーラーを付ける。

## 7. 永続化

- SQLite ファイルを 1 つ置く（既定は `~/.local/share/caucus-race/caucus.db`、環境変数で変更可）。
- テーブルは §2.1 のエンティティに 1 対 1 で対応させる。エンティティは `data` 列に JSON で丸ごと保存し、絞り込みに使う属性（`project_id` / `sprint_id` / `story_id` / `status` / `name`）だけを列として持つ。
- 表示用 ID（`ST-12` など）は接頭辞ごとの連番テーブル（`counters`）で採番する。
- 状態遷移と Event の記録は同一トランザクションで行う。
- M1 では起動時にテーブルを作成する。スキーマ変更が必要になった時点で Alembic を導入する。

## 8. レビュー資料

`caucus review show` で、次を含む Markdown を生成する（保存先: `CAUCUS_HOME/reviews/<project>/sprint-<n>.md`）。

- スプリントゴール、ブランチ、完了したストーリーの件数
- 確認手順（AI が作成）: AI プランナーが設定されていれば、リポジトリと各ストーリーの結果を読んで、成果物を操作して確認する手順を書かせる。初回に作成してスプリントに保存し、以降は使い回す（`--regenerate` で作り直す）
- 変更の概要: ベースブランチとスプリント用ブランチの `git diff --stat`
- ストーリーごとの結果: 状態、受け入れ条件（チェックリスト）、要約、中断理由・エラー、実行ログの場所、コミットと差分の統計、退避ブランチ、未解決のスパイク
- 実行器が挙げた範囲外の課題

## 9. ディレクトリ構成（案）

```
caucus-race/
├── pyproject.toml
├── docs/
│   ├── spec.md
│   └── design.md
├── src/caucus_race/
│   ├── domain/          # entities.py, status.py, rules.py, errors.py
│   ├── usecase/         # planning.py, sprint.py, review.py, retro.py
│   ├── port/            # repository.py, planner.py, executor.py, notifier.py
│   ├── adapter/
│   │   ├── persistence/ # sqlite（SQLAlchemy）
│   │   ├── planner/     # claude.py, fake.py
│   │   ├── executor/    # vuoi.py, fake.py
│   │   └── notifier/
│   ├── orchestrator.py
│   ├── container.py     # injector の設定
│   └── cli/             # Typer
└── tests/
```

## 10. 実装の進め方（案）

| 段階 | 内容 | 完了の目安 |
|---|---|---|
| M1（完了） | domain・SQLite・CLI。人間がストーリーを手で起票し、計画・スパイク・承認・状態遷移が動く | FakeExecutor でスプリントが closed まで進む |
| M2（実装済み・実際の vuoi での確認待ち） | オーケストレーターと VuoiExecutor | 実リポジトリでストーリーが vuoi により実装される |
| M3（実装済み・実際の claude での確認待ち） | ClaudePlanner（計画作成・スパイク抽出・フィードバック変換） | 人間の操作が承認・回答・レビューだけになる |
| M4（実装済み） | 通知・レビュー資料・複数プロジェクトの並行実行 | 実行をバックグラウンドに任せ、対応待ちを通知で受け取れる |
| 以降 | レトロの活用、Web UI、ストーリーの並列実行 | |
