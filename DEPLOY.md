# Phase 1 デプロイ手順

Molcast Slack ボットの初回デプロイ用チェックリスト。詳細なリファレンスは `README.md` の §2–§14 にある。本ファイルは「GCP プロジェクトを新規作成」から「Slack 上で `/mol CCO` を叩くとビューア URL が返ってくる」までを一本道で通すためのもの。`gcloud` CLI を入れた開発用 PC で上から順に実行する。研究室の常時稼働 PC は使わない。

PowerShell のスニペットは Windows ホスト前提で書いてある。macOS / Linux の場合は `$env:VAR` を `export VAR=...` に、バッククォートの行継続をバックスラッシュに置き換える。

## 0. 前提条件 (一度だけ)

- Google アカウント (大学の ELMS アカウントではなく個人の Gmail を使う。プロジェクトメモの注記を参照)。
- `gcloud` CLI のインストール。<https://cloud.google.com/sdk/docs/install#windows> から入手。最後の "Run gcloud init now?" は受諾しておく。
- Slack ワークスペースにカスタムアプリを入れられる管理者相当の権限 (もしくは承認申請を出せる体制)。

Phase 1 のトラフィックは無料枠に収まるはずだが、念のため <https://console.cloud.google.com/billing/budgets> で月額 ¥1000 の請求アラートを設定しておく。

## 1. GCP プロジェクトと請求アカウント

```powershell
$env:PROJECT_ID = "molcast-XXX-001"   # グローバル一意。XXX は適宜変更

gcloud auth login
gcloud projects create $env:PROJECT_ID --name="Molcast"
gcloud config set project $env:PROJECT_ID
```

請求アカウントの作成 (または流用) はコンソール経由のほうが楽。初回は特に。

1. <https://console.cloud.google.com/billing>
2. **Create account** → country `Japan`、currency `JPY`、type `Individual`。クレジットカードを登録する。新規アカウントには 90 日 / $300 のクレジットが付いて Phase 1 の PoC トラフィックは十分まかなえる。
3. 作成後、Billing ページに表示される **Account ID** (`01ABCD-23EFGH-45IJKL` 形式) を控える。

請求アカウントをプロジェクトに紐付ける:

```powershell
# ID が想定どおりか確認
gcloud billing accounts list

# 紐付け (実際の ID に差し替え)
gcloud billing projects link $env:PROJECT_ID `
    --billing-account=01ABCD-23EFGH-45IJKL

# 確認
gcloud billing projects describe $env:PROJECT_ID
# -> billingEnabled: true
```

`$env:PROJECT_ID` が空で返ってくる (シェルセッションで失われた) 場合は、`$env:PROJECT_ID = "..."` を再実行するか、`gcloud billing projects link` コマンドに ID を直接書く。

コンソール経由のほうが楽なら、<https://console.cloud.google.com/billing/projects> を開いて該当プロジェクトの行を探し、右端の **⋮** → **Change billing** でアカウントを選び **Set account** する。

## 2. API の有効化

```powershell
gcloud services enable run.googleapis.com `
                       firestore.googleapis.com `
                       secretmanager.googleapis.com `
                       artifactregistry.googleapis.com `
                       cloudbuild.googleapis.com
```

ステップ 1 が完了していないと `Billing must be enabled` で失敗する。

## 3. Firestore Native mode

ここはブラウザのほうが CLI より楽。

1. <https://console.cloud.google.com/firestore>
2. 左上のプロジェクトセレクタが `molcast-XXX-001` になっていることを確認 (別プロジェクトでもなく、空でもない)。
3. **Create database**
4. **Database ID は `(default)` のままにする** — 別の名前 (例: `molcast`) を入力すると後で `app/store.py` が見つけられず `firestore_save_failed` で詰む。
5. **Native mode** を選ぶ (Datastore mode ではない)。
6. Location `asia-northeast1` (Tokyo) — ステップ 5 の Cloud Run リージョンと一致させる必要がある。
7. Security rules は **Locked mode** (デフォルト拒否) を選択。Cloud Run からのアクセスは IAM で別途制御するのでこのルールは効かない。
8. **Create**。

確認:

```powershell
gcloud firestore databases list --project=$env:PROJECT_ID
# NAME 列が ".../databases/(default)" になっていれば OK
```

もし `molcast` など別名で作ってしまった場合は、データが入っていない段階で消して作り直す:

```powershell
gcloud firestore databases delete --database=molcast --project=$env:PROJECT_ID
gcloud firestore databases create --database="(default)" `
    --location=asia-northeast1 --type=firestore-native --project=$env:PROJECT_ID
```

コンソールが真っ白だったりプロジェクトセレクタにプロジェクトが見つからない場合は、本ファイル末尾のトラブルシュート節を参照。

## 4. Slack App と signing secret

signing secret を取得するため、先に Slack app を作る:

1. <https://api.slack.com/apps> → **Create New App** → *From scratch*
   - Name: `Molcast` (好みで可)
   - Workspace: 研究室のワークスペース
2. **OAuth & Permissions** → *Scopes* → *Bot Token Scopes*: `commands` と `chat:write` を追加。
3. **Basic Information** → **Signing Secret** をコピー (次のコマンドに貼る)。

slash command の設定はあえてステップ 6 まで遅らせる。Request URL に書く Cloud Run URL がステップ 5 を終えないと判明しないため。

signing secret を Secret Manager に格納する (シェル履歴に残さない)。**PowerShell の `|` パイプライン経由で `gcloud secrets ... --data-file=-` に渡すと末尾改行が混入して signing secret が 33 バイトで保存されてしまう**ため、一時ファイル経由で書き込む:

```powershell
$secret = Read-Host -AsSecureString "Slack signing secret"
$plain = [System.Net.NetworkCredential]::new("", $secret).Password
$plain = $plain -replace "[`r`n]", ""    # CR/LF を明示除去
$plain = $plain.Trim()
Write-Host "length = $($plain.Length)"   # 32 を確認

$tempPath = [System.IO.Path]::GetTempFileName()
try {
    [System.IO.File]::WriteAllText($tempPath, $plain)
    Write-Host "file bytes = $((Get-Item $tempPath).Length)"   # 32 を確認
    gcloud secrets create slack-signing-secret `
        --data-file=$tempPath --replication-policy=automatic
} finally {
    Remove-Item $tempPath -Force -ErrorAction SilentlyContinue
    $plain = $null
}
```

新規作成ではなくバージョン追加 (rotation) のときはサブコマンドを `versions add` に変える:

```powershell
# (上と同じ Read-Host + Trim + 一時ファイル書き込みの後)
gcloud secrets versions add slack-signing-secret --data-file=$tempPath
```

Secret Manager は値を置き換える操作を持たない (immutable な version の追加のみ)。`:latest` 参照は自動で新バージョンを指す。

保存値の検証 (Slack 側の Signing Secret と内部値が一致するか):

```powershell
$val = gcloud secrets versions access latest --secret=slack-signing-secret
Write-Host "char length = $($val.Length)"                                              # 32
Write-Host "utf8 bytes  = $([System.Text.Encoding]::UTF8.GetBytes($val).Length)"      # 32
```

両方 32 で、`$val` の冒頭数文字を Slack App の Basic Information にある Signing Secret と目視で一致確認する (全部を画面に出さない)。

Cloud Run のランタイムサービスアカウントに読み取り権限を付与:

```powershell
$projectNumber = gcloud projects describe $env:PROJECT_ID --format="value(projectNumber)"
$runSa = "${projectNumber}-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding slack-signing-secret `
    --member="serviceAccount:${runSa}" `
    --role="roles/secretmanager.secretAccessor"
```

## 5. Cloud Run へのデプロイ

リポジトリのルート (`D:\My_App\Molcast`) で:

```powershell
gcloud run deploy mol-slack-viewer `
    --source . `
    --project $env:PROJECT_ID `
    --region asia-northeast1 `
    --allow-unauthenticated `
    --set-env-vars "SLACK_RESPONSE_TYPE=ephemeral,FIRESTORE_COLLECTION=molecules,RETENTION_DAYS=7,MAX_ATOMS=200,OPSIN_BACKEND=local" `
    --set-secrets "SLACK_SIGNING_SECRET=slack-signing-secret:latest" `
    --cpu-throttling `
    --min-instances 0
```

`--cpu-throttling` は明示しているが Cloud Run のデフォルト挙動。**`--no-cpu-throttling` は使わない** — Phase 1 の `/slack/mol` は同期処理に書き換えてあるので不要、かつインスタンス active 中の継続的な CPU 課金が発生してしまう。

Cloud Build が 4–6 分動く (RDKit の pull)。完了すると次の行が出る:

```
Service URL: https://mol-slack-viewer-xxxxxxxx-an.a.run.app
```

ヘルスチェック:

```powershell
curl.exe https://mol-slack-viewer-xxxxxxxx-an.a.run.app/health
# -> {"status":"ok"}
```

PowerShell の `curl` は `Invoke-WebRequest` のエイリアスでオブジェクトを返してくる。Unix の curl と同じ動作が欲しいなら `curl.exe` と明示する。

コールドスタート直後の初回リクエストは RDKit の import 分で数秒かかる。

## 5.5. Artifact Registry の cleanup policy (初回のみ)

§5 で初めてデプロイすると、Cloud Build がリポジトリ `cloud-run-source-deploy` を `asia-northeast1` に自動作成する。このリポジトリは `--source` ビルドが作るイメージを溜め続けるため、放置すると Artifact Registry の Standard 無料枠 (0.5 GB-month) を超過する。

「常に最新 1 つだけ残す」ポリシーを一度設定すれば、以後の再デプロイで古い manifest が自動的に削除される。リポジトリ直下の `cleanup-policy.json` が定義ファイル。

```powershell
gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy `
    --project=$env:PROJECT_ID `
    --location=asia-northeast1 `
    --policy=cleanup-policy.json
```

確認:

```powershell
gcloud artifacts repositories describe cloud-run-source-deploy `
    --project=$env:PROJECT_ID --location=asia-northeast1
# cleanupPolicies の下に keep-most-recent-1 と delete-anything-else が並ぶ
# 末尾に Repository Size が表示される
```

ポリシー適用後、不要な manifest は即時に削除予約され、参照されなくなった blob は約 24 時間以内に Artifact Registry の GC で物理削除される。redeploy 直後はリポジトリサイズが一時的に倍増することがあるが、翌日には現行イメージ 1 個ぶん (Phase 1 で約 140 MB) まで落ち着く。

dry-run で挙動をプレビューしたい場合は `--policy=cleanup-policy.json --dry-run` を付けて呼ぶと、削除を抑止したままポリシーが登録される。Phase 3 で OPSIN (JRE + py2opsin) を入れたイメージサイズの実測値は、デプロイ後に上記の `gcloud artifacts repositories describe` の `Repository Size` から確認する。本ポリシーが効いていれば現行 1 個ぶんしか残らないので、無料枠 0.5 GB に収まるかどうかは Phase 3 のイメージ実サイズで決まる。

## 5.6. Cloud Tasks 経由の二段フロー (Phase 3 必須)

`/mol name:` 経路は cold start 時の JVM 起動コストで Slack の 3 秒 ack を超えるため、`/slack/mol` で Cloud Tasks に enqueue → ack 即返、`/internal/process` を Cloud Tasks が OIDC 認証付きで叩いて OPSIN + RDKit + Firestore + Slack `response_url` POST まで処理する二段構成にしている。本節はこのインフラの初回セットアップ。

### 5.6.1. Cloud Tasks API を有効化し Queue を作る

```powershell
gcloud services enable cloudtasks.googleapis.com

gcloud tasks queues create molcast-name-resolution `
    --location=asia-northeast1 `
    --max-attempts=5 --max-backoff=120s --min-backoff=2s `
    --max-dispatches-per-second=5 `
    --project=$env:PROJECT_ID
```

`asia-northeast1` は Cloud Run / Firestore と必ず揃える (リージョン間遅延を避ける)。`--max-attempts=5 --max-backoff=120s` で総リトライ時間は ~10 分以内に収まり、Slack の `response_url` 30 分有効期限の内側に入る。

### 5.6.2. Cloud Tasks が OIDC token を発行するための専用 SA を作る

```powershell
gcloud iam service-accounts create molcast-ct-invoker `
    --display-name="Molcast Cloud Tasks invoker" `
    --project=$env:PROJECT_ID

$ctSa = "molcast-ct-invoker@$($env:PROJECT_ID).iam.gserviceaccount.com"
```

### 5.6.3. Cloud Tasks の P4SA から `serviceAccountTokenCreator` を invoker SA に付与

これを忘れると Cloud Tasks が「OIDC token を発行する権限がない」状態になり `/internal/process` 呼び出しが 401 で詰む。`gcp-sa-cloudtasks` という名前の P4SA は API 有効化直後に自動作成される。

```powershell
$projectNumber = gcloud projects describe $env:PROJECT_ID --format="value(projectNumber)"
$ctP4Sa = "service-${projectNumber}@gcp-sa-cloudtasks.iam.gserviceaccount.com"

gcloud iam service-accounts add-iam-policy-binding $ctSa `
    --member="serviceAccount:${ctP4Sa}" `
    --role="roles/iam.serviceAccountTokenCreator" `
    --project=$env:PROJECT_ID
```

### 5.6.4. invoker SA に Cloud Run の `roles/run.invoker` を付与

Cloud Run の `allow-unauthenticated` は維持 (Slack が `/slack/mol` を allUsers として叩く必要があるため)。`/internal/process` 側はアプリ層 OIDC で守るので、Cloud Run IAM レベルでは invoker SA からの呼び出しが余分な 403 を出さないようにここで権限を渡しておく。

```powershell
gcloud run services add-iam-policy-binding mol-slack-viewer `
    --region=asia-northeast1 `
    --member="serviceAccount:${ctSa}" `
    --role="roles/run.invoker" `
    --project=$env:PROJECT_ID
```

### 5.6.5. Cloud Run の env を再デプロイで更新する

§5 の `gcloud run deploy` に以下の `--set-env-vars` を追加して再デプロイ:

```
TASKS_PROJECT_ID=<PROJECT_ID>,
TASKS_QUEUE_ID=molcast-name-resolution,
TASKS_LOCATION=asia-northeast1,
TASKS_INVOKER_SA=molcast-ct-invoker@<PROJECT_ID>.iam.gserviceaccount.com,
BASE_URL=https://mol-slack-viewer-xxxxxxxx-an.a.run.app
```

`BASE_URL` は初回 deploy では URL が確定していないので空のまま、§5 で URL が判明したらこの 5 行を埋めて再デプロイする手順になる (初回 + URL 反映の 2 回)。

### 5.6.6. Firestore idempotency コレクションの TTL ポリシー (任意)

`molecules_idempotency` コレクションに同 idempotency_key で 1 時間 (`IDEMPOTENCY_TTL_SECONDS` のデフォルト) 経過した古いドキュメントが残り続けるのを避けたい場合は、§9 の TTL 設定と同じ手順で `expires_at` フィールドをキーに TTL ポリシーを追加する。Phase 3 規模では遅延 (最大 24 時間) が出ても運用に影響なし。設定しないと idempotency 記録が永続するが容量も微々たる量なので一旦は未設定で良い。

### 5.6.7. スモークテスト (Cloud Tasks 経路)

```powershell
gcloud tasks queues describe molcast-name-resolution `
    --location=asia-northeast1 --project=$env:PROJECT_ID
```

state が `RUNNING` なら OK。Slack で `/mol name: hexafluorobenzene` を叩き、即時 ack「処理中です... 完了したら結果を投稿します。」が表示された後に「3D viewer generated: ...」が後追いで届くことを確認。

```powershell
gcloud tasks list --queue=molcast-name-resolution `
    --location=asia-northeast1 --project=$env:PROJECT_ID
```

待機中タスクが残っていれば dispatch が失敗している (OIDC / IAM のいずれかが未設定)。`gcloud run services logs read` で `oidc_*` / `idempotency_*` / `internal_process_*` の構造化ログを追う。

## 6. Slack slash command を Cloud Run に向ける

<https://api.slack.com/apps> に戻り、Molcast app を開く:

**Slash Commands** → **Create New Command**

| Field | Value |
|---|---|
| Command | `/mol` |
| Request URL | `https://mol-slack-viewer-xxxxxxxx-an.a.run.app/slack/mol` |
| Short Description | `SMILES や座標ファイルから 3D 分子ビューアを生成` |
| Usage Hint | `<SMILES> または name: <IUPAC>` |

Save する。左上の **Install App** でワークスペースにインストール (既にインストール済みなら *Reinstall*)。承認制のワークスペースだと管理者の承認待ちキューに入る。

## 7. 機能スモークテスト (本番確認)

任意の Slack チャンネルで:

### 7.1 SMILES 基本

```
/mol CCO
```

期待する挙動 (Phase 1 は同期処理なので「処理中です...」の ack は出ない、直接ビューア URL が返る):

- 数秒以内に ephemeral メッセージ `3D viewer generated: https://.../view/XXXXXXX` が表示される。
- URL を開くとエタノールが 3D で描画され、回転できる。
- 4 つの操作ボタン (Stick / Ball & Stick / Sphere / Reset View) が全部動く。

コールドスタート直後はインスタンス起動 + RDKit import で 3 秒近くかかる。運悪く Slack の ack 窓を超えた場合は `アプリが応答しなかった` と表示されるので、その場合は同じコマンドをもう一度叩く (2 回目は warm)。

### 7.2 立体化学の保持

```
/mol C/C=C/C
/mol C/C=C\C
```

両方のビューアを並べて開き、メチル基が反対側 (E) と同じ側 (Z) に配置されることを確認。

```
/mol C[C@H](N)C(=O)O
/mol C[C@@H](N)C(=O)O
```

L-Ala と D-Ala。N–CH3–COOH の配置が鏡像になっていることを確認。

### 7.3 エラー経路

```
/mol not_a_smiles
```

→ ephemeral で `SMILES の解釈に失敗しました。表記をご確認ください。` (スタックトレースは出ない)。

```
/mol CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC
```

→ `分子が大きすぎます (上限 200 原子)。...`

```
/mol
```

→ ビューアリンク付きの使い方ヒント。

```
/mol name: ethanol
/mol name: DMSO
/mol name: メタノール
```

→ 3 件とも即時 ack「処理中です... 完了したら結果を投稿します。」が ephemeral で出て、~1-2 秒以内に「3D viewer generated: ...」が後追いで届く。alias hit は `subprocess` 起動せず、構造化ログに `opsin_alias_hit` → `mol_created` の順で出る。

```
/mol name: hexafluorobenzene
```

→ 同じく即時 ack の後、Cloud Tasks の dispatch 経路で JVM 起動 + OPSIN local 解決 → ~5-15 秒後に「3D viewer generated: ...」が届く。Cloud Tasks 経由なので Slack 3 秒 ack に間に合わなくなる心配は無い。構造化ログは `tasks_dispatched` → `opsin_resolved`  → `mol_created` の順。

```
/mol name: not_a_real_compound_xyz
```

→ 即時 ack の後、~1-2 秒後に「OPSIN は体系名のみ対応です。慣用名・商品名は解釈できません。SMILES を直接入力してください: /mol <SMILES>」が後追いで届く。`response_url` POST + 構造化ログに `opsin_user_error`。

### 7.4 Firestore のレコード確認

GCP コンソール → Firestore → `molecules` コレクション。`/mol CCO` 実行で、設計ブリーフ §6.5 のフィールド (`id`, `smiles`, `molblock`, `created_at`, `expires_at`, `created_by`, `channel_id`, `input_name`) を持つドキュメントが 1 件追加されているはず。

### 7.5 ログ衛生チェック

```powershell
gcloud run services logs read mol-slack-viewer --region asia-northeast1 --limit 20
```

`mol_created` の JSON レコードを探す。**生の SMILES 文字列が記録されていない**こと、`smiles_len` と `molblock_len` だけが出ていることを確認する。

## 8. Phase 2 (座標ファイルドロップ) の sanity チェック

ブラウザで `https://<service-url>/view/` を開く。`.pdb` / `.sdf` / `.mol2` / `.xyz` / `.cube` のファイルをビューアペインにドラッグして描画されることを確認。`.cif` をドロップしてエラー領域に日本語の「未対応形式」メッセージが出ることを確認。

## 9. コールドスタート計測 (必要な場合のみ)

サービスを 10 分ほど放置してから `/mol CCO` を打ち、ack からビューア URL までの間隔を計測する。

```powershell
gcloud run services logs read mol-slack-viewer --region asia-northeast1 --limit 5
```

`elapsed_ms` が各 `mol_created` レコードに入っている。コールドスタート遅延が常時きつければ `--min-instances 1` でデプロイし直す。`README.md` §11 を参照。

## 10. 満足したら `main` に取り込む

ステップ 7.1–7.4 が納得いく結果になったら:

```powershell
git checkout main
git merge --no-ff feature/phase-1 -m "Merge Phase 1 SMILES-to-3D viewer"
git branch -d feature/phase-1
```

`--no-ff` を付けると `git log --graph` 上で Phase 1 の範囲が見えるまま残る。

---

## トラブルシュート

### "Firestore コンソールにプロジェクトが出てこない"

ありがちな原因:

1. **プロジェクトの選択ミス。** GCP コンソール左上のプロジェクトセレクタが Firestore の対象を決める。クリックして `molcast` で検索し、`molcast-XXX-001` を選ぶ。リロード。
2. **別の Google アカウント。** セレクタは今ログイン中のアカウントから見えるプロジェクトしか列挙しない。右上のアバターから、ステップ 1 で使った個人 Gmail に切り替える。
3. **Firestore API が未有効。** ステップ 2 で通常は片付くが、途中で止まると有効化されていないことがある。Firestore コンソールに **Create database** ではなく **Enable** ボタンが出ているはず。それを押すか、シェルから `gcloud services enable firestore.googleapis.com` を叩く。
4. **ブラウザキャッシュ。** プロジェクト作成直後はセレクタの反映が遅れることがある。サインアウト → サインインしてハードリロード。

### "`argument PROJECT_ID: Must be specified`"

PowerShell の `$env:PROJECT_ID` がこのセッションで空になっている。ウィンドウをまたぐと保持されない。次のどちらか:

```powershell
$env:PROJECT_ID = "molcast-XXX-001"
echo $env:PROJECT_ID   # 確認
```

…またはコマンドにプロジェクト ID を直書きする:

```powershell
gcloud billing projects link molcast-XXX-001 --billing-account=01ABCD-23EFGH-45IJKL
```

### API 有効化で "Billing must be enabled"

ステップ 1 で請求アカウントの紐付けができていない。`gcloud billing projects describe $env:PROJECT_ID` を実行し直し、`billingEnabled: true` になっているか確認する。

### Cloud Run のデプロイが "Building" で止まったように見える

Cloud Build が RDKit を pull している (初回は 4–6 分)。レイヤキャッシュが効くので 2 回目以降は速い。15 分を超えたら Cloud Build コンソール (<https://console.cloud.google.com/cloud-build/builds>) で詰まっているステップを確認し、再実行する。

### Slack が "アプリが応答しなかった" を返すが Cloud Run のログは問題なさそう

Slack の 3 秒 ack 窓には Slack→Cloud Run のネットワーク遅延と RDKit import が含まれる。`--min-instances 0` のコールドスタートはこの窓を超えうる。回避策としては、slash command の直前に `curl.exe /health` をウォームアップで叩く、もしくは `--min-instances 1` に切り替える (`README.md` §11)。

### Slack 側で ack だけ来てビューア URL が返ってこない

Phase 1 の同期版なら本来発生しないはずだが、もし発生しているなら設計が二段フローのままになっている可能性。`app/main.py` の `/slack/mol` ハンドラが `_process_smiles_sync` を直接 `JSONResponse` で返す形になっているか確認。`BackgroundTasks` を残したまま運用すると Cloud Run の cpu-throttling で停止する。

### `firestore_save_failed` が出続ける

Firestore database 名が `(default)` 以外で作られている可能性が最も多い。

```powershell
gcloud firestore databases list --project=$env:PROJECT_ID
```

NAME 列が `.../databases/(default)` でなければステップ 3 末尾の削除+作り直し手順で `(default)` に揃える。

他の原因:

- Cloud Run のサービスアカウントに Firestore 書き込み権限がない (`roles/editor` または `roles/datastore.user` のどちらかが必要)。確認:

  ```powershell
  $projectNumber = gcloud projects describe $env:PROJECT_ID --format="value(projectNumber)"
  gcloud projects get-iam-policy $env:PROJECT_ID `
      --flatten="bindings[].members" `
      --filter="bindings.members:serviceAccount:${projectNumber}-compute@developer.gserviceaccount.com" `
      --format="value(bindings.role)"
  ```

- Firestore database のリージョンが Cloud Run と違う (両方とも `asia-northeast1`)。

ERROR 級のスタックトレースは `gcloud run services logs read` だと省略されることがあるので、Cloud Logging コンソール (<https://console.cloud.google.com/logs>) で severity フィルタを `>= ERROR` にして見るのが手早い。

### `/view/{id}` が "分子データが見つかりません" を返す

その ID の Firestore ドキュメントが、そもそも作られていない (Cloud Run 側で保存前に例外が出ている。ログで `firestore_save_failed` を確認) か、削除されている (TTL ポリシーが有効なら `expires_at` から最大 24 時間遅れで自動削除される)。

### Slack の Signing Secret 不一致 (403 が連続)

`POST 403 /slack/mol` が連続するなら署名検証で弾かれている。Secret Manager の保存値が末尾改行つきで 33 バイトになっていないか確認:

```powershell
$val = gcloud secrets versions access latest --secret=slack-signing-secret
Write-Host "char length = $($val.Length)"                                              # 32 が正解
Write-Host "utf8 bytes  = $([System.Text.Encoding]::UTF8.GetBytes($val).Length)"      # 32 が正解
```

33 以上なら、ステップ 4 の **一時ファイル経由** の手順でバージョン追加で書き直し、`gcloud run deploy` で再デプロイして新値を読み込ませる。
