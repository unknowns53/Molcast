# Molcast — Slack 分子可視化ボット

生成された SMILES、IUPAC 名、座標ファイルから小分子を 3D 可視化する研究室内 Slack ツール。Cloud Run + FastAPI + Firestore + RDKit + 3Dmol.js で構築されている。設計ブリーフ全体は `Slack_分子可視化ボット_統合版_v2.md` にあり、この README ではサービスの実行・デプロイ・運用について記述する。

リポジトリは Phase 1 (SMILES → 3D ビューア)、Phase 2 (座標ファイル D&D)、Phase 3 (OPSIN による IUPAC / 慣用名のパース) まで実装済み。Phase 3 では `app/opsin_utils.py` が慣用名 mapping → 同梱 OPSIN JAR (`py2opsin` wheel に同梱) → EBI OPSIN Web の三段構えで `name:` 経路を解決する。バックエンドは `OPSIN_BACKEND` 環境変数で `local` / `local_only` / `web` を選択。

## 1. 概要

### できること

- `/mol <SMILES>` → サーバ側で 3D の MolBlock を生成し、ボットが `https://<service>/view/<random-id>` を返す。URL を開くと 3Dmol.js で分子が描画される (stick / ball-and-stick / sphere の各表示)。
- `/mol` (引数なし) → ボットが素のビューアページ (`/view/`) へのリンクを返す。そこに `pdb` / `sdf` / `mol2` / `xyz` / `cube` ファイルをドロップするとブラウザ内で描画される。
- `/mol name: <IUPAC 名 or 慣用名>` → 慣用名 (DMSO, THF, スチレン 等) は `app/opsin_aliases.json` で即解決、それ以外は OPSIN を呼んで SMILES に変換してから SMILES 経路と同じビューアを返す。

### 用途外のもの

ざっくりとした見た目の確認用途のみ。配座探索、MMFF/UFF 単発を超える構造最適化、DFT、MD、OCSR は明示的に対象外で、その用途には RDKit / Gaussian / GROMACS を使う。

## 2. Slack App のセットアップ

1. <https://api.slack.com/apps> を開いて **Create New App** → *From scratch* をクリック。名前 (例: `Molcast`) と対象ワークスペースを指定する。
2. **OAuth & Permissions** → *Scopes* → *Bot Token Scopes* で `commands` と `chat:write` を追加する。
3. **Basic Information** から **Signing Secret** をコピーする。Secret Manager に保管する — §8 を参照。
4. アプリをワークスペースにインストールする。*Require approved apps* が有効な場合、ワークスペース管理者の承認が先に必要。

## 3. Slash command のセットアップ

**Slash Commands** → *Create New Command*:

| Field | Value |
|---|---|
| Command | `/mol` |
| Request URL | `https://<cloud-run-url>/slack/mol` |
| Short description | `SMILES や座標ファイルから 3D 分子ビューアを生成` |
| Usage hint | `<SMILES> または name: <IUPAC>` |

保存する。新しいコマンドを反映するためにアプリをワークスペースに再インストールする。

## 4. 環境変数

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SLACK_SIGNING_SECRET` | yes | — | Slack リクエスト署名 (HMAC-SHA256) |
| `SLACK_RESPONSE_TYPE` | no | `ephemeral` | `ephemeral` か `in_channel` |
| `BASE_URL` | no | リクエストから取得 | `/view/{id}` リンクに使う |
| `FIRESTORE_COLLECTION` | no | `molecules` | Firestore のコレクション名 |
| `RETENTION_DAYS` | no | `7` | `/view/{id}` の TTL |
| `MAX_ATOMS` | no | `200` | `AddHs` 後の原子数ゲート |
| `OPSIN_BACKEND` | no | `local` | Phase 3: `local` / `local_only` / `web` |
| `OPSIN_JAR_PATH` | no | `/opt/opsin/opsin.jar` | Phase 3 のフォールバックパス |
| `OPSIN_WEB_URL` | no | `https://opsin.ch.cam.ac.uk/opsin/` | Phase 3 の EBI エンドポイント |
| `TASKS_PROJECT_ID` | name: 経路で必須 | 空 | Phase 3 二段フロー: Cloud Tasks queue を持つ GCP プロジェクト |
| `TASKS_QUEUE_ID` | name: 経路で必須 | 空 | 例: `molcast-name-resolution` |
| `TASKS_LOCATION` | no | `asia-northeast1` | Cloud Run と同一リージョン |
| `TASKS_INVOKER_SA` | name: 経路で必須 | 空 | Cloud Tasks が OIDC token を発行する SA |
| `INTERNAL_PROCESS_PATH` | no | `/internal/process` | Cloud Tasks が叩く内部エンドポイント |
| `IDEMPOTENCY_COLLECTION` | no | `molecules_idempotency` | Cloud Tasks at-least-once 配信のデデュープ用 |
| `IDEMPOTENCY_TTL_SECONDS` | no | `3600` | 同上、TTL ポリシーで自動 expire させたい場合の参考値 |
| `LOG_LEVEL` | no | `INFO` | Python `logging` のレベル |
| `PORT` | — | Cloud Run が注入 | Uvicorn のバインドポート |

`EMBED_MAX_RETRIES` は意図的に外に出していない。各試行は独自の ETKDGv3 パラメータセットを使うため、埋め込み再試行回数は 3 回で固定している (`app/rdkit_utils.py` および設計ブリーフ §6.1 を参照)。

## 5. ローカル開発

```bash
python -m venv .venv
. .venv/bin/activate           # または: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env

# 単体テストを実行 (Firestore は不要 — ストアはモック化されている):
pytest

# サービスを起動。Firestore のクレデンシャルが無いと /view/{id} と
# /slack/mol の POST エンドポイントは Firestore 呼び出しで落ちる。
# /health、/view/、/、手動 D&D のビューアエンドポイントは動く。
uvicorn app.main:app --reload
```

ローカルで Firestore 経由のパスを試したい場合は、Firestore エミュレータを並行起動する (`gcloud components install cloud-firestore-emulator` を実行した後で):

```bash
gcloud emulators firestore start --host-port=localhost:8088
# 別シェルで:
export FIRESTORE_EMULATOR_HOST=localhost:8088
uvicorn app.main:app --reload
```

Windows での `rdkit`: Python 3.11 の pip wheel は動作する (Docker イメージのランタイムと合わせている)。pip install が手元の環境で失敗するなら、Cloud Run のビルド経路 (`gcloud run deploy --source .`) が動作する環境を作る正規ルート。

## 6. `name:` 経路 (Phase 3) のローカル開発

OPSIN は 3 つのモード (`OPSIN_BACKEND`) で動く:

- `local` — `app/opsin_aliases.json` の慣用名を先に引き、次に同梱 OPSIN JAR (`py2opsin` wheel に同梱の `opsin-cli-*-jar-with-dependencies.jar` を `subprocess.run` 経由で呼ぶ。タイムアウト 10 秒)。これも失敗したら EBI OPSIN Web (タイムアウト 5 秒) にフォールバック。本番のデフォルト。
- `local_only` — alias → ローカル OPSIN のみ。Web フォールバックを切る。CI で JRE 欠落を確実に検知したいときに使う。
- `web` — alias → EBI OPSIN Web のみ。`py2opsin` の import 自体をスキップするので、手元に JRE を入れたくないローカル開発で便利。

ホスト側で `local` / `local_only` モードのテストを動かしたいときは Temurin 17 (もしくは JRE 11+) を入れる:

```bash
# macOS
brew install --cask temurin@17
# Debian/Ubuntu
sudo apt install default-jre-headless
# Windows: Temurin を https://adoptium.net/ からダウンロード
```

`tests/test_opsin_utils.py` は `subprocess.run` と `httpx.get` を mock するので、CI / 通常開発では JRE 不要 (alias JSON の RDKit round-trip 検証は走る)。同様に `tests/test_main.py`, `tests/test_tasks_dispatch.py`, `tests/test_oidc_verify.py` も Cloud Tasks クライアントと OIDC 検証を mock するので、ローカル開発で GCP クレデンシャルや `gcloud auth` は要らない。Cloud Tasks 経由の二段フローを実機で確認したいときだけ `DEPLOY.md §5.6` の手順で Queue / SA を作成する。

## 7. Docker ビルド

```bash
docker build -t molcast .

# ローカルのスモークテスト (Firestore クレデンシャルなし):
docker run --rm -p 8080:8080 \
    -e SLACK_SIGNING_SECRET=dummy \
    -e GOOGLE_APPLICATION_CREDENTIALS=/dev/null \
    molcast
# http://localhost:8080/health を開く
```

イメージはシングルステージ (`python:3.11-slim` + `ca-certificates` + `default-jre-headless` + RDKit + py2opsin)。OPSIN は `subprocess` 経由で JAR を叩くので JVM のウォームアップは毎回かかる (~1-2 秒)。

## 8. Cloud Run へのデプロイ

1. プロジェクトを作成して API を有効化する:

   ```bash
   gcloud config set project YOUR_PROJECT_ID
   gcloud services enable run.googleapis.com \
       firestore.googleapis.com \
       secretmanager.googleapis.com \
       artifactregistry.googleapis.com
   ```

2. Slack の signing secret を Secret Manager に保管する:

   ```bash
   echo -n "$(read -s SS; echo "$SS")" | \
       gcloud secrets create slack-signing-secret \
           --data-file=- --replication-policy=automatic
   ```

3. デプロイ:

   ```bash
   PROJECT_ID=YOUR_PROJECT_ID ./deploy.sh
   ```

4. 表示されるサービス URL を控え、末尾に `/slack/mol` を付けたものを Slack コマンドの *Request URL* に貼り付ける。

## 9. Firestore の有効化と TTL

1. GCP コンソールの **Firestore → Native mode** で、リージョンに `asia-northeast1` (Cloud Run と同じ) を選ぶ。Native mode が必須。
2. コードはリクエストごとの期限チェックで `expires_at` を使っているため、TTL ポリシーは任意。自動削除を有効化する場合 (反映に最大 24 時間の遅延あり):

   - **Firestore → TTL → Add policy**
   - Collection: `molecules`
   - Field: `expires_at`

   TTL ポリシーを有効化するまで期限切れドキュメントは残り続けるが、`/view/{id}` は期限切れページを正しく返す (コードが `{"expired": True}` センチネルで弾く)。

## 10. 使用例

```text
/mol CCO
  → 3D viewer generated: https://<service>/view/abcd...

/mol C/C=C/C
  → trans-2-butene (E)

/mol C/C=C\\C
  → cis-2-butene (Z)

/mol C[C@H](N)C(=O)O
  → L-alanine

/mol not_a_smiles
  → SMILES の解釈に失敗しました。表記をご確認ください。

/mol
  → 使い方: /mol <SMILES> ...
    座標ファイルを描画するには https://<service>/view/ を ...

# Phase 3 (name: 経路、Cloud Tasks 二段化)
# まず即時 ack「処理中です... 完了したら結果を投稿します。」が ephemeral で表示され、
# その後に下記が response_url 経由で後追い投稿される。
/mol name: ethanol
  → 3D viewer generated: https://<service>/view/abcd...   # alias hit (CCO)
/mol name: DMSO
  → 3D viewer generated: ...                              # alias hit (CS(C)=O)
/mol name: 4-vinylpyridine
  → 3D viewer generated: ...                              # alias hit (C=Cc1ccncc1)
/mol name: hexafluorobenzene
  → 3D viewer generated: ...                              # OPSIN local 解決 (~5-15 秒)
/mol name: メタノール
  → 3D viewer generated: ...                              # alias hit (CO)、kana 経路
/mol name: not_a_real_compound
  → OPSIN は体系名のみ対応です。慣用名・商品名は解釈できません。
    SMILES を直接入力してください: /mol <SMILES>
```

座標ファイル (Phase 2) はブラウザで `/view/` を開き、ビューア領域にファイルをドロップする。対応フォーマットは `pdb`、`sdf`、`mol2`、`xyz`、`cube`。CIF は手元で OpenBabel で変換する:

```bash
obabel input.cif -O output.xyz
```

## 11. 運用とチューニング

### Slack ハンドラのフロー

経路ごとに 2 種類:

- **SMILES 経路**: `/slack/mol` を**同期処理**で完結 (Phase 1 で実証)。RDKit embed + Firestore で 3 秒 ack 窓内に収まる。
- **`name:` 経路**: **二段フロー** (Phase 3 で導入)。`/slack/mol` で Cloud Tasks に enqueue + ack 即返 (「処理中です...」) → `/internal/process` を Cloud Tasks が OIDC token 付きで叩く → OPSIN + RDKit + Firestore + Slack `response_url` POST。

Cloud Run の `cpu-throttling=always` (デフォルト) は FastAPI の `BackgroundTasks` を ack 返却直後に絞ってしまうため、ack 後の重い処理は同一インスタンス内では実行できない。`name:` 経路の OPSIN local backend は JVM 起動 (`java -jar` を毎回起動) で 1-2 秒のオーバーヘッドが恒常的に乗るので、3 秒 ack 内に収めるのは無理がある。そこで Cloud Tasks にタスクを「投げ直す」形で同一インスタンスを再度のリクエストとして起こす。これなら cpu-throttling=always を維持したまま (アイドル時 0 課金) 重い処理を許容できる。

重複処理対策は二段防衛: (a) Cloud Tasks の deterministic task name (`sha256(response_url)`) で Slack のスラッシュコマンドリトライを CT 側で弾く、(b) Firestore `molecules_idempotency` コレクションへの `create()` で CT の at-least-once 内部リトライ (worker クラッシュ後など) を弾く。

Cloud Tasks のセットアップは `DEPLOY.md §5.6` 参照。Queue 作成 + invoker SA 作成 + CT P4SA から invoker SA への `serviceAccountTokenCreator` 付与 + invoker SA への Cloud Run `roles/run.invoker` 付与 + 環境変数の設定の 5 ステップ。

### コールドスタート

`--min-instances 1` 無しだと Cloud Run は数分のアイドル後にインスタンスを 0 まで落とす。アイドル明け初回リクエストは RDKit の import (~1 秒) + インスタンス起動 (~1〜2 秒) + RDKit embed (CCO で ~0.5 秒) で計 3 秒前後 — Slack の 3 秒 ack 制限のギリギリ。普通分子なら大半通るが、運悪く溢れたら `アプリが応答しなかった` として Slack 側でタイムアウト表示される。その場合はユーザーが同じコマンドを再投入すれば warm インスタンスで通る。

頻発するなら以下に切り替える:

```bash
MIN_INSTANCES=1 PROJECT_ID=... ./deploy.sh
```

`--min-instances 1` は 1 インスタンスを 24 時間常時暖機状態に保つ。cpu-throttling は default のままなのでアイドル時の CPU 課金は無く、メモリ常時分の課金が乗るだけ — 月数百円程度 (実トラフィック次第で `asia-northeast1` の Cloud Run 価格表を参照)。

### ロギング

ログは stdout への構造化 JSON で、Cloud Logging が自動で拾う。許可された構造化フィールドは `app/logging_config.py` に列挙されている。**SMILES 本体と MolBlock は生のまま記録されることは絶対に無い**、記録するのはサイズ・件数・識別子だけ。

ERROR レベルの例外スタックトレースは `gcloud run services logs read` だと出ない場合があるので、Firestore 例外や RDKit 例外を追うときは Cloud Logging コンソール (<https://console.cloud.google.com/logs>) で severity フィルタ `>= ERROR` を使うのが手早い。

### `response_url` のリトライ (Phase 3 用、Phase 1 では未使用)

`app/slack_dispatch.py` は 5xx またはネットワークエラー時に `response_url` への POST を 2 回 (指数バックオフ) 再試行する実装が入っているが、Phase 1 の同期フローでは呼ばれない。Phase 3 で `name:` 経路を非同期化するときに再利用する。継続的に失敗するとログに `response_url POST failed after retries` として残るので、その時に Cloud Logging でこの文字列を検索する。

## 12. セキュリティに関するメモ

- **Signing secret の扱い。** 生のシークレットを `deploy.sh`、`.env`、コミット対象のファイルに置かない。Secret Manager に置き、`--set-secrets` でランタイム注入する (`deploy.sh` を参照)。
- **ログ漏洩なし。** 構造化ロガーはホワイトリスト化されたフィールドのみ出力する。uvicorn のアクセスログフィルタはクエリ文字列をマスクする。新しいログ呼び出しを追加する場合、生の SMILES を `extra=` で渡さないこと。
- **`/view/{id}` は推測不能だが認証は無い。** URL を知っている人なら誰でも分子を閲覧できるので、その閲覧に関しては URL 自体がシークレットだと考える。Phase 1 / 2 / 3 は機密データ向けには設計されていない (設計ブリーフ §1 を参照)。
- **原子数ゲート。** `AddHs` 後の `MAX_ATOMS=200` で最も明白な DoS ペイロード (キロ原子クラスの SMILES) は弾けるが、悪意ある呼び出し側が 199 原子の埋め込み再試行に RDKit の CPU を浪費させることは依然として可能。悪用が顕在化してきたら、Cloud Run の同時実行数上限 (`--concurrency`) かユーザー単位のレート制限を追加する。
- **Pre-commit フック。** このリポジトリは `hooks/pre-commit` と `hooks/commit-msg` を同梱しており、`.git-banned-patterns` (gitignore 済み) を読んで個人識別子の誤コミットを機械的に止める。新規 clone 時:

  ```bash
  cp .git-banned-patterns.example .git-banned-patterns
  # 好みで編集する
  git config core.hooksPath hooks
  ```
