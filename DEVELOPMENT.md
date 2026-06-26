# 開発者向けノート

Molcast (Slack 分子可視化ボット) のローカル開発・テスト・運用チューニング。Cloud Run へのデプロイ手順自体は `DEPLOY.md`、機能の使い方は `README.md`、システム設計の詳細は `Slack_分子可視化ボット_統合版_v2.md` を参照。

## 1. リポジトリ構成

```
app/                FastAPI アプリ本体
  main.py           エンドポイント (/slack/mol, /internal/process, /view/{id})
  store.py          Firestore wrapper (trajectory schema, idempotency)
  templates.py      3D viewer / 2D viewer HTML レンダリング
  rdkit_utils.py    SMILES → 3D MolBlock、formula/MW、2D SVG
  opsin_utils.py    alias JSON + OPSIN local + EBI Web のフォールバック
  opsin_aliases.json 慣用名 → SMILES の静的マッピング
  slack_verify.py   Slack 署名検証 (HMAC-SHA256)
  slack_dispatch.py response_url POST (Phase 3 二段フロー用)
  tasks_dispatch.py Cloud Tasks enqueue (trajectory schema v2)
  dev_dispatch.py   DEV_INLINE_NAME_RESOLUTION の inline 実装
  oidc_verify.py    Cloud Tasks OIDC token 検証
  config.py         pydantic-settings によるenv var バインディング
  logging_config.py 構造化ログ + SMILES 漏洩ガード
tests/              pytest (外部依存は全 mock; CI で GCP creds 不要)
dev/
  render_viewer_sample.py   uvicorn なしでビューア HTML を生成 + ブラウザで開く
hooks/              pre-commit / commit-msg (個人情報の混入を機械的に止める)
Dockerfile          python:3.11-slim + JRE + RDKit Draw native libs
cloudbuild.yaml     --cache-from でレイヤ再利用する build パイプライン
deploy.sh           cloudbuild.yaml → Cloud Run deploy --image の 2 段
cleanup-policy.json Artifact Registry の "最新 1 image だけ残す" ポリシー
```

## 2. 環境変数

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SLACK_SIGNING_SECRET` | yes | — | Slack リクエスト署名 (HMAC-SHA256) |
| `SLACK_RESPONSE_TYPE` | no | `ephemeral` | デフォルトの response 公開範囲 (`--public` で override) |
| `BASE_URL` | no | リクエストから取得 | `/view/{id}` リンクに使う固定 URL |
| `FIRESTORE_COLLECTION` | no | `molecules` | Firestore のコレクション名 |
| `RETENTION_DAYS` | no | `7` | `/view/{id}` の TTL |
| `MAX_ATOMS` | no | `200` | `AddHs` 後の原子数ゲート |
| `OPSIN_BACKEND` | no | `local` | `local` / `local_only` / `web` |
| `OPSIN_JAR_PATH` | no | `/opt/opsin/opsin.jar` | OPSIN JAR の明示パス (py2opsin 同梱 JAR を上書き) |
| `OPSIN_WEB_URL` | no | `https://opsin.ch.cam.ac.uk/opsin/` | EBI OPSIN Web エンドポイント |
| `TASKS_PROJECT_ID` | name: 経路で必須 | 空 | Cloud Tasks queue を持つ GCP プロジェクト |
| `TASKS_QUEUE_ID` | name: 経路で必須 | 空 | 例: `molcast-name-resolution` |
| `TASKS_LOCATION` | no | `asia-northeast1` | Cloud Run と同一リージョン |
| `TASKS_INVOKER_SA` | name: 経路で必須 | 空 | Cloud Tasks が OIDC token を発行する SA |
| `INTERNAL_PROCESS_PATH` | no | `/internal/process` | Cloud Tasks が叩く内部エンドポイント |
| `IDEMPOTENCY_COLLECTION` | no | `molecules_idempotency` | at-least-once 配信のデデュープ用コレクション |
| `IDEMPOTENCY_TTL_SECONDS` | no | `3600` | 同上、TTL ポリシーで自動 expire させたい場合の参考値 |
| `LOG_LEVEL` | no | `INFO` | Python `logging` のレベル |
| `PORT` | — | Cloud Run が注入 | uvicorn のバインドポート |
| `DEV_SKIP_SIGNATURE_VERIFICATION` | no | `false` | ローカル開発専用。Slack 署名検証をスキップ。**Cloud Run に設定するな** |
| `DEV_INLINE_NAME_RESOLUTION` | no | `false` | ローカル開発専用。`name:` を Cloud Tasks ではなくスレッドで inline 実行。**Cloud Run に設定するな** |

`EMBED_MAX_RETRIES` は意図的に固定 (3 回)。各試行は独自の ETKDGv3 パラメータセットを使うため、4 回目に意味のあるパラメータが残らない。`app/rdkit_utils.py` 参照。

## 3. ローカル開発

```bash
python -m venv .venv
. .venv/Scripts/activate           # Windows; mac/linux は .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env

# 単体テスト (Firestore は不要 — store はモック化、GCP creds 不要)
pytest

# サービス起動。Firestore のクレデンシャル無しでも /health, /view/, /
# (drop-zone のみの bare viewer) は動く。/slack/mol POST と /view/{id}
# は Firestore 呼び出しで落ちる。
uvicorn app.main:app --reload
```

Firestore エミュレータを使うなら:

```bash
gcloud components install cloud-firestore-emulator
gcloud emulators firestore start --host-port=localhost:8088
# 別シェルで
export FIRESTORE_EMULATOR_HOST=localhost:8088
uvicorn app.main:app --reload
```

### Windows での RDKit

`rdkit` の Python 3.11 pip wheel は動作する (Docker イメージのランタイムと合わせている)。pip install が手元の環境で失敗するなら、Cloud Run のビルド経路 (`gcloud run deploy --source .` または `bash ./deploy.sh`) が動作する環境を作る方が早い (RDKit 周りのトラブルシュートに時間を溶かさない)。

### `dev/render_viewer_sample.py` でビューアを単体プレビュー

uvicorn も Firestore も不要、ファイル 1 枚を browser で開いて UI を iterate するため。

```bash
# 単一フレーム
.venv/Scripts/python dev/render_viewer_sample.py --smiles "CCO" --open

# トラジェクトリ
.venv/Scripts/python dev/render_viewer_sample.py \
    --smiles "CCO ; CC(C)O ; CC(C)(C)O" --open

# 2D モード
.venv/Scripts/python dev/render_viewer_sample.py \
    --smiles "CCO ; CC(C)O" --mode 2d --open

# 空 viewer (drop-zone のみ)
.venv/Scripts/python dev/render_viewer_sample.py --bare --open
```

`app/templates.py` を編集して保存 → 上記コマンド再実行で秒で iterate できる。

## 4. HTTPS トンネル経由で Slack から実機テスト

文言・viewer 装飾・alias 追加など軽い iteration のために `bash ./deploy.sh` を毎回叩く (1 サイクル 3-8 分) と磨きが進まない。`uvicorn --reload` + HTTPS トンネル + 2 つの dev フラグで、ローカル uvicorn を Slack のスラッシュコマンドから秒で叩ける。

`.env` に dev フラグを足す (`.env` は gitignore 済み):

```
DEV_SKIP_SIGNATURE_VERIFICATION=true
DEV_INLINE_NAME_RESOLUTION=true
OPSIN_BACKEND=web        # JRE をローカルに入れないなら web 経由が楽
SLACK_RESPONSE_TYPE=ephemeral
BASE_URL=                # トンネル URL は起動時に決まるので空でよい
```

手順:

1. uvicorn をローカルで起動 (リクエスト毎に reload):

   ```powershell
   .venv\Scripts\activate
   uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
   ```

   起動ログに `dev_flags_active` の WARNING が出ているのを確認 (出ていなければフラグが効いていない)。

2. 別シェルで cloudflared で HTTPS トンネルを張る (<https://github.com/cloudflare/cloudflared/releases> の Windows バイナリを PATH に置く):

   ```powershell
   cloudflared tunnel --url http://localhost:8080
   ```

   ターミナルに `https://<random>.trycloudflare.com` の URL が出る。サインアップ不要の quick tunnel。

3. Slack App の **Slash Commands → /mol** を編集し、*Request URL* を `https://<random>.trycloudflare.com/slack/mol` に一時的に書き換える。Save。Slack 側の再インストールは不要。

4. Slack から `/mol CCO` などを投げると、ローカル uvicorn でハンドルされる。`app/*.py` を編集して保存すると uvicorn が即 reload するので、次のリクエストは新しいコードで処理される。

5. テストが終わったら Slack の Request URL を本番に戻す。

caveat:

- 同じ Slack ワークスペースの他者が `/mol` を使いたい時間帯はこの手順を避ける (Request URL が一時的に自分のローカルを向く)。磨き専用ワークスペースを別途用意するのが安全
- Firestore は本物 (asia-northeast1 のプロジェクト) に書き込みに行くので、ローカルで生成した分子も本番 `molecules` コレクションに残る。気になるなら Firestore エミュレータを並走 (§3)
- `DEV_*` フラグは Cloud Run 側 env vars に**絶対に**含めない。`deploy.sh` も `DEV_*` を含まない設計

## 5. OPSIN バックエンド

`OPSIN_BACKEND` で 3 モード:

- `local` — `app/opsin_aliases.json` を引いた後、py2opsin 同梱の OPSIN JAR を `subprocess.run` (timeout 10 秒) → 失敗で EBI Web (timeout 5 秒) フォールバック。本番デフォルト
- `local_only` — alias → ローカル OPSIN。Web フォールバック無し。CI で JRE 欠落を確実に検知したい時
- `web` — alias → EBI Web のみ。`py2opsin` の import 自体をスキップするので、手元に JRE を入れたくないローカル開発で便利

ホスト側で `local` / `local_only` を動かしたいなら Temurin 17 (もしくは JRE 11+) を入れる:

```bash
# macOS
brew install --cask temurin@17
# Debian/Ubuntu
sudo apt install default-jre-headless
# Windows: https://adoptium.net/ から Temurin をダウンロード
```

`tests/test_opsin_utils.py` は `subprocess.run` と `httpx.get` を mock するので、通常の `pytest` は JRE 不要。Cloud Tasks 二段フローの実機確認は `DEPLOY.md §5.6` 参照。

## 6. Docker ビルド

```bash
docker build -t molcast .

# ローカルスモークテスト (Firestore creds 無し)
docker run --rm -p 8080:8080 \
    -e SLACK_SIGNING_SECRET=dummy \
    -e GOOGLE_APPLICATION_CREDENTIALS=/dev/null \
    molcast
# http://localhost:8080/health
```

イメージはシングルステージ (`python:3.11-slim` + RDKit + py2opsin + 同梱 JRE + Draw 用 native libs)。Draw が使う libxrender / libxext / libexpat / libfontconfig / libfreetype / libcairo を明示的に apt 入れている点に注意 — slim image にはどれも入ってないので、これらが欠けると container 起動時に `ImportError: libXrender.so.1: cannot open shared object file` の連鎖で落ちる。

## 7. Slack ハンドラのフロー

経路ごとに 2 種類:

- **SMILES-only**: `/slack/mol` を**同期処理**で完結。RDKit embed + Firestore で 3 秒 ack 窓内に収まる。`process_and_save_frames` がそのまま走る
- **`name:` を含む**: **二段フロー**。`/slack/mol` で Cloud Tasks に enqueue + ack 即返 → `/internal/process` を Cloud Tasks が OIDC token 付きで叩く → OPSIN + RDKit + Firestore + Slack `response_url` POST

Cloud Run の `cpu-throttling=always` (デフォルト) は FastAPI の `BackgroundTasks` を ack 返却直後に絞ってしまうため、ack 後の重い処理は同一インスタンス内では実行できない。`name:` 経路は OPSIN JVM 起動 (1-2 秒) が乗るので 3 秒 ack 内には収めない。そこで Cloud Tasks にタスクを「投げ直す」形で重い処理を許容できる。

重複処理対策は二段防衛:

- (a) Cloud Tasks の deterministic task name (`sha256(response_url)[:32]`) で Slack のスラッシュコマンドリトライを CT 側で弾く
- (b) Firestore `molecules_idempotency` コレクションへの `create()` で CT の at-least-once 内部リトライ (worker クラッシュ後など) を弾く

Cloud Tasks payload schema は v2 (current):

```json
{
  "schema_version": 2,
  "kind": "trajectory",
  "segments": [{"kind": "smiles"|"name", "payload": "..."}],
  "was_capped": false,
  "flags": {"public": false, "label": false, "no_3d": false},
  "response_url": "...",
  "user_id": null,
  "channel_id": null,
  "base_url": "...",
  "idempotency_key": "..."
}
```

## 8. Firestore document schema (trajectory)

```
{
  "id":          str,
  "frames":      list[ Frame ],
  "flags":       {"public": bool, "label": bool, "no_3d": bool},
  "created_at":  datetime,
  "expires_at":  datetime,
  "created_by":  str | null,
  "channel_id":  str | null,
}

Frame = {
  "kind":      "smiles" | "name",
  "input":     str,            # 元の /mol トークン
  "smiles":    str | null,     # 解決後 SMILES
  "molblock":  str | null,     # 3D MolBlock
  "error":     str | null,     # フレーム単位のエラーメッセージ
}
```

レガシー single-mol document (frames フィールド無し) は `app.store.get_molecule` が単一フレーム trajectory に正規化して返すので、ビューア側はスキーマ分岐を持たない。

## 9. 運用とチューニング

### コールドスタート

`app/main.py` の `_lifespan` は startup 時に `generate_3d_molblock("CCO")` を一度走らせ、`rdkit.Chem.AllChem` の import と ETKDG パラメータキャッシュを温めている。Cloud Run は startup probe 完了まで traffic を流さないので、ユーザの初回 `/mol CCO` はこのコストを払い済みの状態で着信する。warm-up は通常 2-4 秒、コンテナ起動全体で 5-8 秒。

それでも初回が辛い場合の選択肢 (`DEPLOY.md §9` 参照):

- `--cpu-boost` — 起動時 CPU を一時倍加、warm-up 時間半減、定常課金は変わらず
- `--min-instances=1` — idle 中も 1 インスタンス常駐、月 ~¥400 程度、Slack 3 秒 ack 窓を確実に守る
- Cloud Scheduler で 5 分おきに `/health` を叩く — 機能的には min-instances=1 と同等、Scheduler 無料枠で済む

### ロギング

ログは stdout への構造化 JSON で、Cloud Logging が自動で拾う。許可フィールドは `app/logging_config.py` に列挙。**SMILES 本体と MolBlock は記録されない**、記録するのはサイズ・件数・識別子だけ。

ERROR レベルの例外スタックは `gcloud run services logs read` だと省略されることがあるので、追うときは Cloud Logging コンソール (<https://console.cloud.google.com/logs>) で severity フィルタ `>= ERROR` を使う。

### `response_url` のリトライ

`app/slack_dispatch.py` は 5xx またはネットワークエラー時に 2 回 (指数バックオフ) 再試行する。継続失敗時はログに `response_url POST failed after retries` として残るので、Cloud Logging でこの文字列を grep する。

## 10. セキュリティに関するメモ

- **Signing secret の扱い**: 生のシークレットを `deploy.sh`、`.env`、コミット対象のファイルに置かない。Secret Manager に置き、`--set-secrets` でランタイム注入する
- **ログ漏洩なし**: 構造化ロガーはホワイトリスト化されたフィールドのみ出力。新規ログ呼び出しで生 SMILES を `extra=` に渡さないこと
- **`/view/{id}` は推測不能だが認証無し**: URL を知っている人なら誰でも閲覧できる。機密データ向けには設計されていない
- **原子数ゲート**: `AddHs` 後 `MAX_ATOMS=200` で明白な DoS ペイロード (キロ原子クラスの SMILES) を弾く。199 原子の embed 連続リクエストで CPU を浪費させる悪用が顕在化したら、Cloud Run の `--concurrency` 上限かユーザ単位レート制限を追加する
- **Pre-commit フック**: リポジトリは `hooks/pre-commit` と `hooks/commit-msg` を同梱し、`.git-banned-patterns` (gitignore 済み) を読んで個人識別子の誤コミットを止める。新規 clone 時:

  ```bash
  cp .git-banned-patterns.example .git-banned-patterns
  # 好みで編集する
  git config core.hooksPath hooks
  ```
