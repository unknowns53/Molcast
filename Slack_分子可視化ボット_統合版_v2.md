# Slack 分子可視化ボット 統合設計ブリーフ v2（Claude Code 引き継ぎ用）

研究室 Slack 上で、生成プログラム（機械学習・生成モデル等）が出力した仮想的低分子の SMILES、座標ファイル、IUPAC 名を、ブラウザで対話的に操作できる 3D 分子モデルに変換する内部ツールを構築する。本書は **確定した設計判断 / 段階計画 / 未検証項目** を分けて記述する。Claude Code はこれを入力として実装計画書を作成すること。確定済みの制約を再導出したり覆したりしないこと。

実装路線は Cloud Run + FastAPI + Firestore + RDKit + 3Dmol.js の Web サービス型である。研究室内 PoC レベルの内部ツールであり、機密性は前提としない。ただし基本的な健全性（Slack 署名検証、原子数上限、スタックトレース非露出、ログ汚染防止）は維持する。

---

## §1 目的とユースケース

研究室メンバーが Slack に分子を渡すと、ブラウザで回転・拡大できる対話的な 3D 分子モデルを得られるようにする。クイック可視化が用途であり、発表用の厳密構造や物性は別途 RDKit / Gaussian / GROMACS で作る前提。

主用途。

- 生成プログラム（機械学習や生成モデル）で出力された仮想低分子の SMILES を、研究室メンバー間で素早く 3D 共有する。
- 文献中の慣用名・IUPAC 名で議論する場面で「名前 → 3D」を即座に行う。
- 生成プログラムが吐く xyz / sdf / mol2 を、ビューアの D&D で直接描画する。

機密性は前提ではない。研究室内 PoC レベルの内部ツールである。ただし以下の基本セキュリティ・運用衛生は維持する。

- Slack Signing Secret による HMAC-SHA256 署名検証。
- 原子数上限による DoS 的な重い処理の遮断。
- 例外スタックトレースを Slack / HTML に露出しない。
- ログ・構造化ログに SMILES / MolBlock の本体を出さない（ID とサイズのみ）。
- `/view/{id}` のランダム ID URL により、URL を知らない第三者の偶発閲覧を防ぐ。

## §2 制約と前提

- Cloud Run + FastAPI + Firestore による Web サービス型構成を採用する。常時起動のローカル PC やローカル GPU には依存しない。
- Python 3.11、RDKit、3Dmol.js、`google-cloud-firestore` を採用する。Java は OPSIN JAR を Phase 3 で同梱する目的でのみ使う（multi-stage Docker）。
- スコープは研究室内利用。ワークスペースが限られるので、外部公開・課金・SLA は考えない。
- 本書および成果物（コード、コメント、コミットメッセージ、パッケージメタデータ）には個人情報（氏名・所属詳細・会話用呼称を含む）を一切書かない。`hooks/` 配下に pre-commit / commit-msg を仕込み、`.git-banned-patterns`（gitignore 済み）の禁止語を機械的に弾く。
- Firestore は GCP プロジェクト内で利用する。TTL は 7 日（環境変数 `RETENTION_DAYS` で変更可）。
- 直接 `main` にコミットしない。`feature/<topic>` ブランチで作業し、ユーザー検証完了後に `main` に畳む。

## §3 スコープと段階

### §3.1 フェーズ計画

| フェーズ | 入力 | 中核実装 | 完了条件 |
|---|---|---|---|
| 1 | SMILES | FastAPI + RDKit + Firestore + 3Dmol.js | `/mol CCO` で URL が返り、URL を踏むと 3D が描画される。代表分子で E/Z, R/S 立体が保持される。 |
| 2 | 座標ファイル | ビューア HTML の D&D 受け口（FileReader） | `/view/{id}` および素のビューア URL（`/view/`、`/`）で pdb / sdf / mol2 / xyz / cube を D&D 描画できる。 |
| 3 | IUPAC 名 | 慣用名静的 mapping + 同梱 OPSIN JAR（multi-stage Docker） | `/mol name: <名前>` で SMILES 化 → Phase 1 経路に合流。慣用名（DMSO / DMF / THF など研究室常用 50-100 個）は OPSIN を経由せず mapping から返す。 |

### §3.2 Out of scope

- 構造式画像 OCSR（DECIMER / MolScribe）。GPU 不要・シンプル化のため除外。
- 配座網羅、エネルギー最小化の厳密化、自由エネルギー計算、機械学習物性予測。
- マルチユーザー向け公開サービス化、認証基盤、課金。
- 構造データの長期永続化、検索インデックス化、バージョン管理。Firestore TTL=7 日で自動失効する設計とする。

## §4 全体アーキテクチャ

### §4.1 全体図

```
Slack
  │ POST /slack/mol (application/x-www-form-urlencoded)
  │ X-Slack-Request-Timestamp, X-Slack-Signature
  ▼
┌─────────────────────────────────────────────────────────────┐
│ Cloud Run (asia-northeast1)                                  │
│  FastAPI + Uvicorn                                           │
│   ├── /health                                                │
│   ├── /slack/mol                                             │
│   │     [同期パート: 3 秒以内に 200 ack]                       │
│   │     ├ Slack 署名検証 (HMAC-SHA256)                        │
│   │     ├ text 解析 (SMILES / name: / 引数なし)              │
│   │     └ BackgroundTasks にジョブ投入 → 即 ack を返す        │
│   │     [非同期パート]                                         │
│   │     ├ Phase 3: 慣用名 mapping → OPSIN JAR → SMILES      │
│   │     ├ RDKit ETKDGv3 + MMFF/UFF → MolBlock              │
│   │     ├ Firestore save (id, molblock, expires_at, ...)    │
│   │     └ response_url へ最終結果を POST                       │
│   ├── /view/{id}                                             │
│   │     ├ Firestore get                                      │
│   │     ├ expires_at チェック                                  │
│   │     └ HTML を動的生成 (3Dmol.js + MolBlock 埋め込み)      │
│   └── /view/ , /  (素のビューア、D&D 待ち UI)                  │
│                                                               │
│  同梱: RDKit (pip), OPSIN JAR (multi-stage で stage1 から)    │
└─────────────────────────────────────────────────────────────┘
       │
       ▼
   Firestore (id → smiles, molblock, created_at, expires_at, ...)
       │
       ▼
   利用者ブラウザ
       └ /view/{id} を GET → 3Dmol.js で描画
         (または素のビューア URL で座標ファイル D&D)
```

### §4.2 状態モデル

- Cloud Run はステートレス。`id → MolBlock` の保存は Firestore に置く。
- TTL は `expires_at` フィールドで管理。アクセス時にコード側で期限切れを判定し、期限切れ HTML を返す。Firestore の TTL ポリシーは後から有効化できるようフィールドだけ用意する。
- `/view/{id}` のランダム ID は推測困難なものを発行する（`secrets.token_urlsafe(16)` 相当）。

## §5 エンドポイント仕様

### §5.1 `GET /health`

ヘルスチェック用。

```json
{"status": "ok"}
```

### §5.2 `POST /slack/mol`

Slack slash command 用エンドポイント。`application/x-www-form-urlencoded` で送られてくる。

主なパラメータ。

- `text`: slash command の引数。SMILES、`name: <IUPAC>`、または空。
- `user_name`, `user_id`, `channel_id`, `team_id`, `response_url`.

**処理フロー（ack 即返 + 後追い投稿パターン）**。Cloud Run は同期処理で 3 秒制約に間に合わない経路が存在する（cold start、Phase 3 の OPSIN JVM 起動、RDKit Embed リトライ）。そのため次の二段構えを **本流フロー** として採用する。

同期パート（3 秒以内に必ず ack を返す）:

1. raw body を取得（`await request.body()`）。
2. Slack 署名検証（§6.4）。失敗時 403。
3. form を読み、`text` を分類:
   - 空: usage を ephemeral で即返。
   - `name:` 接頭辞: Phase 3 の名前経路へ。Phase 1/2 着手時は「Phase 3 で対応予定です」を即返。
   - それ以外: SMILES として扱う。
4. `response_url` を抽出し、入力（SMILES または name）と合わせて `BackgroundTasks` にジョブを投入する。
5. ack（受付メッセージ）を ephemeral で即返す。例: `"処理中です... 完了したら結果をこのスレッドに投稿します。"`。

非同期パート（`BackgroundTasks` 内）:

1. 名前経路なら `iupac_to_smiles(name, backend=settings.OPSIN_BACKEND)` を呼び SMILES を得る。
2. `generate_3d_molblock(smiles)` を呼ぶ（§6.1）。
3. ランダム ID を発行し Firestore に保存（§6.5）。
4. `response_url` に POST で最終結果を投稿する（成功時は viewer URL、失敗時はユーザフレンドリーエラー）。
5. **`response_url` の制約**: 投稿後 30 分有効、最大 5 回まで。失敗時の再送リトライは最大 2 回（指数バックオフ）に制限する。

**`response_url` への POST ペイロード例**（成功時）:

```json
{
  "response_type": "ephemeral",
  "replace_original": false,
  "text": "3D viewer generated: https://<service-url>/view/<random_id>"
}
```

失敗時（`MoleculeGenerationError` の文言を流用）:

```json
{
  "response_type": "ephemeral",
  "replace_original": false,
  "text": "SMILES の解釈に失敗しました。表記をご確認ください。"
}
```

`response_type` は環境変数 `SLACK_RESPONSE_TYPE` で切替可能（デフォルト `ephemeral`）。

非同期パートが軽量（慣用名 mapping ヒット、小分子 SMILES）で 3 秒以内に確実に終わる場合でも、フロー単純化のため一律で `response_url` 経由とする。例外的に同期返答を許可する設計分岐は持たない。

### §5.3 `GET /view/{mol_id}`

3Dmol.js 入り HTML を動的に生成して返す。HTML ファイルはディスクに保存しない。

処理内容。

1. Firestore から `mol_id` を読む。
2. 存在しなければ 404 HTML を返す（§7.3）。
3. `expires_at < now()` なら期限切れ HTML を返す（§7.3）。
4. MolBlock を `json.dumps` で JS リテラルとして HTML に埋め込み、ページ読み込み時に `viewer.addModel(molblock, "mol")` を呼ぶ。

### §5.4 `GET /view/` および `GET /`

`mol_id` を持たない素のビューア URL。`render_viewer_html(smiles=None, molblock=None, mol_id=None)` を呼び、座標ファイル D&D 待ち UI を返す（§7.2）。`main.py` で `/view/{mol_id}` とは別ハンドラとして定義する。

## §6 化学処理

### §6.1 RDKit パイプライン

ローカル RDKit による 3D 生成。関数シグネチャ。

```python
class MoleculeGenerationError(Exception):
    """ユーザー向けエラーに翻訳する独自例外。"""

EMBED_SEEDS: tuple[int, ...] = (0xf00d, 0xbeef, 0x1234)

def generate_3d_molblock(
    smiles: str,
    max_atoms: int = 200,
) -> str:
    ...
```

処理手順。

1. `mol = Chem.MolFromSmiles(smiles)`。`None` なら `MoleculeGenerationError("SMILES の解釈に失敗しました")`。
2. `mol = Chem.AddHs(mol)`。
3. **H 付加後の原子数が `max_atoms` を超える**なら `MoleculeGenerationError("分子が大きすぎます (上限 {max_atoms} 原子)")`。
4. Embed リトライ（3 回固定、`EMBED_SEEDS` を順に消費）:
   - 試行 0: `params = AllChem.ETKDGv3(); params.randomSeed = EMBED_SEEDS[0]; params.useRandomCoords = False`
   - 試行 1: `params.randomSeed = EMBED_SEEDS[1]; params.useRandomCoords = True`
   - 試行 2: `params.randomSeed = EMBED_SEEDS[2]; params.useRandomCoords = True; params.maxAttempts = 200`
   - 戻り値が -1 のあいだ次の試行に進む。全試行失敗なら `MoleculeGenerationError("3D 構造の生成に失敗しました")`。
   - リトライ戦略は **3 回固定** とする。可変化のための環境変数は持たない（4 回目以降のパラメータが未定義になる潜在的バグを避ける）。
5. MMFF→UFF フォールバック:
   - `props = AllChem.MMFFGetMoleculeProperties(mol)` が `None` なら MMFF 不可。
   - MMFF 可なら `AllChem.MMFFOptimizeMolecule(mol, maxIters=200)` を呼ぶ。戻り値が 1（未収束）でもエラーにせず継続。
   - MMFF 不可なら `AllChem.UFFOptimizeMolecule(mol, maxIters=200)` を呼ぶ。
6. `Chem.MolToMolBlock(mol)` を返す。改行コードは LF に統一する（3Dmol.js の MOL/SDF パーサは LF と CRLF の両方を許容するが、回帰テストの安定性のため LF 固定）。

立体化学（E/Z, R/S）は `MolFromSmiles` 後に保たれる前提。代表分子で実測し、保持が壊れる入力クラスがあれば回帰テストに加える（§12）。

ユーザフレンドリーエラー文言（日本語）の例。

- 「SMILES の解釈に失敗しました。表記をご確認ください。」
- 「3D 構造の生成に失敗しました。立体的に困難な構造の可能性があります。」
- 「分子が大きすぎます (上限 200 原子)。座標ファイルを直接ビューアに D&D してください。」

### §6.2 座標ファイル経路（Phase 2）

- 3Dmol.js ネイティブ対応形式（`pdb`, `sdf`, `mol2`, `xyz`, `cube`）はビューアの FileReader でそのまま読み、`viewer.addModel(text, format)` に渡す（§7）。
- CIF はネイティブ非対応。利用者の手元で OpenBabel CLI（`obabel input.cif -O output.xyz`）を実行して xyz/pdb に変換する手順を README に記載する。サーバ側での自動変換はスコープ外。
- ボット側（FastAPI 側）の改修は不要。ビューア HTML に D&D ハンドラを追加するだけで完結する。

### §6.3 IUPAC 名経路（Phase 3）

入力解釈の優先順位は次の通り。**慣用名 mapping を OPSIN の前段に必ず配置する**。OPSIN は体系名しか解釈できないので、混合溶媒系を扱う研究室では慣用名のカバーが必須となる。

1. 慣用名静的 mapping（`app/opsin_aliases.json`、研究室常用 50-100 個）: methanol / ethanol / DMSO / DMF / THF / toluene / acetonitrile / 主要モノマー等を SMILES に直結。
2. **同梱 OPSIN JAR**（Java）を `subprocess` 経由で呼ぶ。タイムアウト 10 秒（`subprocess.run(timeout=10)`）。py2opsin を使うか、直接 `java -jar` を起動するかは §6.3.1 で決める。
3. タイムアウトまたは JAR 起動エラー時:
   - `OPSIN_BACKEND=local_only`: そのまま失敗（`MoleculeGenerationError`）。
   - `OPSIN_BACKEND=local`（デフォルト）: EBI OPSIN Web へ自動フォールバック（HTTP タイムアウト 5 秒）。
   - `OPSIN_BACKEND=web`: 最初から EBI OPSIN Web のみを呼ぶ（JAR を起動しない、開発時用）。
4. すべて失敗時: 「OPSIN は体系名のみ対応です。慣用名・商品名は解釈できません。SMILES を直接入力してください: `/mol <SMILES>`」を `response_url` 経由で返す。

代替案として EBI OPSIN Web サービス（https://opsin.ch.cam.ac.uk/）を HTTP で呼ぶ経路を `OPSIN_BACKEND=web` で残す。外部サービス down 時の挙動が読みづらいが、ローカル開発で JRE を入れたくない場合のデフォルトとして有用。

### §6.3.1 OPSIN 呼び出しの実装判断

- **第一候補**: `py2opsin`（pip パッケージ）。OPSIN JAR を pip インストール時に同梱でき、Python から関数呼び出しで使えるため subprocess 管理を省略できる。`requirements.txt` に `py2opsin` を追加する。
- **代替**: `java -jar /opt/opsin/opsin.jar -o smi` を `subprocess.run` で叩く。py2opsin が動かない場合のフォールバック。Dockerfile に JAR を別途配置する経路は §11.1 に残す。
- いずれの経路でも JRE は Docker イメージに必要。`subprocess` 呼び出しには必ずタイムアウトを設定する（10 秒）。

### §6.4 Slack 署名検証

環境変数 `SLACK_SIGNING_SECRET` を使う。

検証に使う材料。

- HTTP ヘッダ `X-Slack-Request-Timestamp`
- HTTP ヘッダ `X-Slack-Signature`
- raw body（FastAPI では `await request.body()` で取得）

検証手順。

1. timestamp が現在時刻から **5 分以上** ずれていれば拒否（replay 防止）。
2. base string を作る: `v0:<timestamp>:<raw_body>`
3. `SLACK_SIGNING_SECRET` で HMAC-SHA256 を計算する。
4. `v0=<hex_digest>` を組み立てる。
5. `hmac.compare_digest` で `X-Slack-Signature` と比較する（timing-safe）。
6. 不一致なら 403 を返す。

関数シグネチャ。

```python
def verify_slack_request(
    signing_secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    tolerance_seconds: int = 300,
) -> bool:
    ...
```

### §6.5 Firestore スキーマ

コレクション名は環境変数 `FIRESTORE_COLLECTION` で指定（デフォルト `molecules`）。

ドキュメントフィールド一覧。

| フィールド | 型 | 内容 |
|---|---|---|
| `id` | string（doc ID） | `secrets.token_urlsafe(16)` |
| `smiles` | string | 入力 SMILES。名前経路の場合は SMILES 化後の結果 |
| `input_name` | string \| null | 名前経路の場合の元入力。SMILES 直接入力なら null |
| `molblock` | string | RDKit 生成 MolBlock（LF 改行） |
| `created_at` | timestamp | サーバ時刻 |
| `expires_at` | timestamp | `created_at + RETENTION_DAYS days` |
| `created_by` | string \| null | Slack `user_id`。プライバシー保護のため `user_name` は保存しない |
| `channel_id` | string \| null | Slack `channel_id` |

`expires_at` は Firestore TTL ポリシーで自動削除可能なフィールドとして用意する。初期実装では TTL ポリシー設定は README 手順に留め、コード側で `expires_at < now()` を都度判定する。

ID は `secrets.token_urlsafe(16)` を採用する（URL セーフ、推測困難）。

関数シグネチャ。

```python
def save_molecule(
    mol_id: str,
    smiles: str,
    molblock: str,
    input_name: str | None,
    created_by: str | None,
    channel_id: str | None,
) -> None:
    ...

def get_molecule(mol_id: str) -> dict | None:
    ...
```

`get_molecule` は **期限切れの場合 `None` ではなく `{"expired": True}` を返す** ことで、`/view/{id}` 側で 404 と期限切れを区別できるようにする。

### §6.6 構造化ログ

`MoleculeGenerationError` のメッセージはユーザに返してよいが、内部ログ・構造化ログには **SMILES 本体・MolBlock 本体を出さない**。出してよいのは以下:

- `mol_id`（Firestore のドキュメント ID）
- `created_by`（Slack user_id）
- `len(smiles)`, `len(molblock)`, `num_atoms`
- 処理時間、エラー種別（`MoleculeGenerationError` の型名）

実装上は `logging` の `extra` 引数で構造化フィールドを渡し、formatter で JSON 化する。FastAPI のミドルウェアで request body をログに流す挙動はデフォルトで無効化する。

## §7 ビューア仕様

`/view/{id}`、`/view/`、`/` で動的生成する HTML 1 枚で、SMILES 経路と座標ファイル D&D 経路の両方を扱う。

### §7.1 レイアウトとスタイル

- ページタイトル: `Molecule Viewer` 固定（タブタイトルに分子由来文字列を入れない）。
- 上部に SMILES を表示する。長い SMILES でも崩れないように `word-break: break-all` で折り返す。MolBlock 経路で SMILES が無いケース（D&D 経路）では非表示。
- 3D viewer 領域: 横幅 `100%`、高さ `80vh`。
- デフォルト表示: stick、H 原子表示可。
- 操作ボタン（viewer 領域の上または下）:
  - `Stick`: `viewer.setStyle({}, {stick:{}}); viewer.render();`
  - `Ball & Stick`: `viewer.setStyle({}, {stick:{radius:0.15}, sphere:{scale:0.25}}); viewer.render();`
  - `Sphere`: `viewer.setStyle({}, {sphere:{}}); viewer.render();`
  - `Reset View`: `viewer.zoomTo(); viewer.render();`
- モバイル最小対応: `viewport` meta、ボタンタッチサイズ 44px 以上。

### §7.2 データ受け渡し

- `/view/{id}` で訪問された場合: Firestore から取得した MolBlock を `json.dumps(molblock)` で安全に JS 文字列としてサーバ側 HTML に埋め込み、ページ読み込み時に `viewer.addModel(molblock, "mol")` を呼ぶ。
- 素のビューア URL（`/view/` または `/`）で訪問された場合: MolBlock を空にし、点線枠 + 「ファイルをここにドロップ」UI を表示する。
- 座標ファイル D&D 経路: ブラウザの FileReader で読み、拡張子から format を判定して `viewer.addModel(text, format)` に渡す。サポート形式は `pdb`, `sdf`, `mol2`, `xyz`, `cube`。

MolBlock を JS に渡す経路はすべて **テキストとしての受け渡し** で完結（`json.dumps` でエスケープ → JS リテラルとして埋め込み → `viewer.addModel(text, "mol")`）。バッククォートや特殊文字、`</script>` 部分文字列で壊れないようにする。

3Dmol.js は CDN から読み込む（`<script src="https://3dmol.csb.pitt.edu/build/3Dmol-min.js">` 等）。研究室内 PoC レベルなので SRI 必須化は行わない。

### §7.3 エラー UI

スタックトレースは出さず、平易な日本語で表示する（`<div role="alert">` で同一 DOM 領域に出す）。

- 404（Firestore に該当 ID が無い）: 「分子データが見つかりません。URL をご確認ください。」
- 期限切れ（`expires_at < now()`）: 「この 3D ビューアは期限切れです（発行から {RETENTION_DAYS} 日）。`/mol` で再生成してください。」
- MolBlock パース失敗（3Dmol.js の `addModel` 例外）: 「分子データの解釈に失敗しました。SMILES から再生成してください。」
- ファイル読込失敗（D&D 経路、未対応形式）: 「ファイル形式に対応していません。pdb / sdf / mol2 / xyz / cube または OpenBabel で変換してください。」

## §8 Slack 配管とコマンド設計

### §8.1 コマンド体系

- `/mol <SMILES>`: 既定経路。
- `/mol name: <IUPAC 名 または 慣用名>`: 名前経路（Phase 3）。接頭辞 `name:` で自動判別の不安定さを回避する。
- 引数なし `/mol`: **素のビューア URL（`/view/`）を ephemeral で返す**。座標ファイル D&D 用の導線とする。
- 空入力時メッセージ例: 「使い方: `/mol <SMILES>` または `/mol name: <IUPAC 名>`。座標ファイルを描画するには <{BASE_URL}/view/|こちら> を開いてドラッグ&ドロップしてください。」

### §8.2 応答フロー（ack 即返 + 後追い投稿）

§5.2 の二段構えに対応:

1. raw body 取得 → Slack 署名検証 → form 解析（同期）。
2. text を分類し、SMILES / 名前 / 空を判定（同期）。
3. **空・即時返答可能なケース以外は `BackgroundTasks` 投入 + ack 即返**（同期、ここまで 3 秒以内）。
4. 非同期パートで RDKit / OPSIN を実行（Phase 3 では OPSIN JVM 起動が加算される）。
5. 非同期パートで Firestore 保存 → `response_url` へ最終結果 POST。
6. 失敗時: `MoleculeGenerationError` の文言を `response_url` 経由で ephemeral 投稿。スタックトレースは表に出さない。
7. `response_url` の制約: 投稿後 30 分有効、最大 5 回。リトライは最大 2 回（指数バックオフ）に制限。

### §8.3 メッセージ整形例

ack（同期、即返）:

```
処理中です... 完了したら結果を投稿します。
```

成功時（非同期、`response_url` 経由）:

```
3D viewer generated: https://<service-url>/view/<random_id>
```

引数なし（D&D 導線、即返）:

```
使い方: /mol <SMILES> または /mol name: <IUPAC 名>
座標ファイルは https://<service-url>/view/ にアクセスしてドラッグ&ドロップ
```

## §9 実装スケルトンとファイル構成

```
mol-slack-viewer/
  app/
    main.py                  # FastAPI エントリ (/health, /slack/mol, /view/{id}, /view/, /)
    rdkit_utils.py           # generate_3d_molblock, MoleculeGenerationError, EMBED_SEEDS
    opsin_utils.py           # iupac_to_smiles (Phase 3)
    opsin_aliases.json       # 慣用名 → SMILES の静的 mapping
    slack_verify.py          # Slack 署名検証
    slack_dispatch.py        # response_url への後追い POST (リトライ含む)
    store.py                 # Firestore save_molecule / get_molecule
    templates.py             # render_viewer_html / render_not_found_html / render_expired_html
    config.py                # 環境変数読み込み (pydantic-settings 等)
    logging_config.py        # 構造化ログ設定 (SMILES/MolBlock マスク)
  tests/
    test_rdkit_utils.py
    test_slack_verify.py
    test_stereochemistry.py  # E/Z, R/S 保持の回帰テスト
    test_templates.py
    test_opsin_utils.py      # mapping ヒット時 / OPSIN 呼び出しモック
    fixtures/
      representative_smiles.json
  hooks/                     # pre-commit / commit-msg (本名・呼称フィルタ)
  requirements.txt
  Dockerfile
  .dockerignore
  .gitignore
  .env.example
  README.md
  deploy.sh
```

`.gitignore` には `.env`、`__pycache__/`、`*.log`、`.git-banned-patterns` を含める。

### §9.1 `app/main.py`

FastAPI アプリ本体。次の 4 エンドポイントを定義する。

- `GET /health`
- `POST /slack/mol`
- `GET /view/{mol_id}`
- `GET /view/`（および `GET /` を同じハンドラに）

Slack エンドポイントでは raw body が必要なので、`await request.body()` を取得して署名検証に使い、その後 form を読む。`BackgroundTasks` を FastAPI から DI で受け取り、非同期ジョブを投入する。

### §9.2 `app/rdkit_utils.py`

§6.1 の `generate_3d_molblock`、`MoleculeGenerationError`、`EMBED_SEEDS` を実装する。

### §9.3 `app/slack_verify.py`

§6.4 の `verify_slack_request` を実装する。

### §9.4 `app/slack_dispatch.py`

`response_url` への後追い POST を担当。`httpx` 等で実装し、5xx / ConnectionError に対して指数バックオフで最大 2 回リトライ。

```python
def post_to_response_url(
    response_url: str,
    payload: dict,
    *,
    timeout: float = 5.0,
    max_retries: int = 2,
) -> None:
    ...
```

### §9.5 `app/store.py`

§6.5 の `save_molecule` / `get_molecule` を実装する。`google-cloud-firestore` の `Client` を使う。期限切れは `{"expired": True}` を返して templates 側で表示分岐できるようにする。

### §9.6 `app/templates.py`

HTML 生成関数。MolBlock を JS に埋め込むときは `json.dumps(molblock)` を使う。

```python
def render_viewer_html(
    smiles: str | None,
    molblock: str | None,
    mol_id: str | None,
) -> str:
    ...

def render_not_found_html() -> str:
    ...

def render_expired_html(retention_days: int) -> str:
    ...
```

`render_viewer_html` は `molblock=None, mol_id=None` で呼ばれた場合に D&D 待ち UI を返す。

### §9.7 `app/opsin_utils.py`（Phase 3）

慣用名 mapping → OPSIN backend の二段構え。シグネチャは §6.3 と一貫させる。

```python
from typing import Literal

OpsinBackend = Literal["local", "local_only", "web"]

def iupac_to_smiles(
    name: str,
    backend: OpsinBackend = "local",
    *,
    subprocess_timeout: float = 10.0,
    web_timeout: float = 5.0,
) -> str:
    """1. opsin_aliases.json を引く。
    2. ヒットしなければ backend に応じて OPSIN を呼ぶ。
       - local      : py2opsin (subprocess) を試す → 失敗時 EBI Web へフォールバック
       - local_only : py2opsin (subprocess) のみ。失敗で MoleculeGenerationError
       - web        : EBI OPSIN Web のみ
    3. どちらも失敗時は MoleculeGenerationError。
    """
```

`backend` のデフォルト値は `config.py` 経由で環境変数 `OPSIN_BACKEND` から注入する。`main.py` 側では `iupac_to_smiles(name, backend=settings.OPSIN_BACKEND)` の形で呼ぶ。

### §9.8 `app/logging_config.py`

§6.6 の構造化ログ。`logging.config.dictConfig` で JSON formatter を設定し、`record.msg` に SMILES / MolBlock を入れない運用を強制する（コードレビュー時にチェック）。FastAPI / Uvicorn のアクセスログから `?` 以降のクエリ文字列をマスクする設定を入れる。

## §10 環境変数

| 変数 | 必須/任意 | デフォルト | 用途 |
|---|---|---|---|
| `SLACK_SIGNING_SECRET` | 必須 | - | Slack 署名検証用 |
| `SLACK_RESPONSE_TYPE` | 任意 | `ephemeral` | `ephemeral` / `in_channel` |
| `BASE_URL` | 任意 | リクエスト URL から推定 | `/view/{id}` URL 生成に使う |
| `FIRESTORE_COLLECTION` | 任意 | `molecules` | Firestore コレクション名 |
| `RETENTION_DAYS` | 任意 | `7` | `expires_at` の TTL |
| `MAX_ATOMS` | 任意 | `200` | H 付加後判定の原子数上限 |
| `OPSIN_BACKEND` | 任意 | `local` | `local` / `local_only` / `web` |
| `OPSIN_JAR_PATH` | 任意 | `/opt/opsin/opsin.jar` | py2opsin 不使用時のフォールバック用パス |
| `OPSIN_WEB_URL` | 任意 | `https://opsin.ch.cam.ac.uk/opsin/` | EBI OPSIN Web のエンドポイント |
| `LOG_LEVEL` | 任意 | `INFO` | ログレベル |
| `PORT` | - | Cloud Run が自動設定 | Uvicorn 起動ポート |

`SLACK_SIGNING_SECRET` は Cloud Run の Secret Manager から注入し、`deploy.sh` には平文で書かない。

`EMBED_MAX_RETRIES` は **環境変数として持たない**（§6.1 リトライは 3 回固定）。

## §11 Dockerfile と Cloud Run デプロイ

### §11.1 Dockerfile（multi-stage）

Phase 3 で OPSIN JAR を同梱するため、最初から multi-stage 構成で書く。stage 1 で JAR を取得し、stage 2 で Python + JRE をベースに JAR をコピーする。

**OPSIN JAR の取得は未検証**（§13 #3）。Dockerfile 例ではプレースホルダ変数で書き、README で実在 artifact 名・URL に差し替える運用とする。`py2opsin` を採用する場合（§6.3.1 第一候補）は JAR 取得 stage そのものを撤去し、JRE のみ stage 2 に入れる。

骨格（py2opsin 採用版、推奨）:

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.11-slim

# OPSIN を py2opsin 経由で呼ぶので JRE のみ必要
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jre-headless ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
```

骨格（subprocess + 別配置 JAR 版、py2opsin が使えない場合のフォールバック、multi-stage）:

```dockerfile
# syntax=docker/dockerfile:1

# ===== stage 1: OPSIN JAR を取得 =====
FROM eclipse-temurin:17-jre AS opsin-fetch
ARG OPSIN_RELEASE_URL=https://example.invalid/PLACEHOLDER-replace-with-verified-url
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && mkdir -p /opt/opsin \
    && curl -fsSL -o /opt/opsin/opsin.jar "${OPSIN_RELEASE_URL}"

# ===== stage 2: ランタイム =====
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jre-headless ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=opsin-fetch /opt/opsin/opsin.jar /opt/opsin/opsin.jar

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

ENV PYTHONUNBUFFERED=1 \
    OPSIN_JAR_PATH=/opt/opsin/opsin.jar
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
```

`OPSIN_RELEASE_URL` は README で実在の Release artifact URL（`opsin-cli-X.Y.jar` の形式は OPSIN リリースごとに変動する可能性あり、§13 #3 で実機検証）に差し替える。検証前に CI に乗せない。

`.dockerignore` には `tests/`、`__pycache__`、`.env`、`.git/`、`README.md`、`deploy.sh` を含める。

### §11.2 `requirements.txt`

```
fastapi
uvicorn[standard]
google-cloud-firestore
python-multipart
httpx
rdkit
py2opsin
pydantic-settings
```

`py2opsin` は Phase 3 で使う。Phase 1 着手時点では import を遅延（関数内 import）にして、Phase 1 のテストでは依存させない実装でもよい。

### §11.3 `deploy.sh`

Google Cloud CLI を使ったデプロイスクリプト。`SLACK_SIGNING_SECRET` は Secret Manager 経由。`--min-instances` のデフォルトは **0**（課金最小化）とし、cold start 実測（§13 #2）後に必要なら README の手順で 1 に切り替える。

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="your-project-id"
REGION="asia-northeast1"
SERVICE_NAME="mol-slack-viewer"

gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --allow-unauthenticated \
  --set-env-vars "SLACK_RESPONSE_TYPE=ephemeral,FIRESTORE_COLLECTION=molecules,RETENTION_DAYS=7,MAX_ATOMS=200,OPSIN_BACKEND=local" \
  --set-secrets "SLACK_SIGNING_SECRET=slack-signing-secret:latest" \
  --min-instances 0
```

cold start が常時 3 秒を超えるようなら、README の「運用チューニング」節で `--min-instances 1` への切替手順と、概算月額（Cloud Run の vCPU 時間・メモリ単価から算出、目安として月 USD 数〜十数ドル）を明記する。

### §11.4 Slack App 設定（README に書く）

- Slack API で新規 App 作成。
- Slash Commands を有効化。command は `/mol`、Request URL は `https://<cloud-run-url>/slack/mol`。
- Signing Secret を取得し、Secret Manager（`slack-signing-secret`）に登録した上で Cloud Run から `--set-secrets` で注入。
- スコープ: `commands`, `chat:write`。
- ワークスペースにインストール。Require approved apps が ON のワークスペースでは管理者承認が必要。

### §11.5 Firestore 有効化（README に書く）

- GCP コンソールで Firestore（Native mode）を有効化し、`asia-northeast1` を選ぶ（Cloud Run と揃える）。
- TTL ポリシーは初期は未設定で良い（コード側で都度判定）。後から `expires_at` フィールドを TTL field として設定可能。

## §12 テスト

`pytest` で以下を必須化。

### §12.1 `test_rdkit_utils.py`

- `CCO` から MolBlock が生成できる。
- 不正 SMILES（例 `"not_a_smiles"`）で `MoleculeGenerationError`。
- `MAX_ATOMS` 超過で `MoleculeGenerationError`。
- MMFF→UFF フォールバック判定（MMFF 不可な分子で UFF が呼ばれること）。

### §12.2 `test_slack_verify.py`

- 正しい署名で `True`。
- 間違った署名で `False`。
- 古い timestamp（5 分超）で `False`。
- 未来 timestamp（5 分超）で `False`。

### §12.3 `test_stereochemistry.py`（回帰テスト）

- `C/C=C/C` (E) と `C/C=C\C` (Z) で MolBlock の座標が異なること。
- `C[C@H](N)C(=O)O` (L-Ala) と `C[C@@H](N)C(=O)O` (D-Ala) で座標が異なること。
- 立体記述子が `Chem.MolToSmiles(Chem.MolFromMolBlock(generated))` で保持されること。

### §12.4 `test_templates.py`

- `render_viewer_html` の MolBlock 埋め込みで、特殊文字（バッククォート、改行、`</script>`）を含む MolBlock が壊れずに JS リテラルとして埋め込まれる。
- `molblock=None, mol_id=None` で呼ぶと D&D 待ち UI を含む HTML が返る。

### §12.5 `test_opsin_utils.py`

- mapping ヒット時（`"DMSO"` → `"CS(=O)C"` 等）に外部呼び出しが発生しないこと（mock で確認）。
- `backend="web"` 指定で Web エンドポイントが呼ばれること（mock）。
- `backend="local"` 指定で py2opsin タイムアウト時に Web へフォールバックすること（mock）。

Firestore 連携は Cloud 上でしか動かないため、unit テストではモック（`unittest.mock.patch`）で疎通確認し、結合確認は手動とする。

## §13 未検証項目

1. **RDKit pip パッケージの Cloud Run 動作確認**。`rdkit` パッケージが Python 3.11 + Linux スリムイメージで動くか。不安定なら micromamba/conda へ切替。
2. **Cloud Run cold start の実測**。RDKit import 時間と（Phase 3 で）JVM 起動時間が、ack 即返フローに影響するか（後追い投稿側の遅延として顕在化）。`--min-instances 1` に倒すかの判断材料。
3. **OPSIN JAR / py2opsin の動作確認**。py2opsin の最新版が Cloud Run 環境の JRE と組み合わせて動作するか。動かない場合は OPSIN GitHub Releases の実在 artifact URL を pin して `OPSIN_RELEASE_URL` に差し込む。
4. **慣用名 mapping の網羅範囲**。研究室で扱う溶媒・モノマー・配位子の常用名 50-100 個を抽出し、初期 JSON を用意する。
5. **Firestore TTL ポリシーの自動削除**。`expires_at` フィールドを TTL field として設定する手順と、削除タイミング（最大 24 時間遅延）の挙動確認。
6. **Slack 内製アプリ承認設定**。Require approved apps が ON ならオーナー承認が必要。インストール試行で切り分ける。
7. **`response_type` のデフォルト**。`ephemeral` を採用しているが、生成物を全員と共有したいユースケースが多ければ `in_channel` に倒す判断もある。運用フィードバック後に再評価。
8. **`response_url` 投稿失敗時の挙動**。最大 2 回リトライで救済できないケースの頻度を運用ログから確認。
9. **Cloud Run リージョン**。`asia-northeast1` を採用しているが、Firestore のリージョンと揃える必要がある。

## §14 README 章立て（必須項目）

`README.md` には次の章を順に立てる。各章は初心者でも Cloud Run へデプロイできる粒度で書く。

1. 概要（目的、ユースケース、使えるコマンド一覧）
2. Slack App 作成手順（§11.4 を展開）
3. Slash command 作成手順
4. 必要な環境変数（§10 の表を貼る）
5. ローカル実行方法（`uvicorn app.main:app --reload`、Firestore エミュレータの起動含む）
6. Phase 3 ローカル開発の前提（host に Temurin 17 等の JRE を入れる手順、または `OPSIN_BACKEND=web` を開発時デフォルトにする選択肢）
7. Docker ビルド方法（`docker build`、`docker run` 例、`OPSIN_RELEASE_URL` の取り扱い）
8. Cloud Run へのデプロイ手順（`deploy.sh` の実行、Secret Manager への `SLACK_SIGNING_SECRET` 登録）
9. Firestore 有効化手順（§11.5 を展開）と TTL ポリシー設定
10. 使用例（`/mol CCO`、`/mol name: ethanol`、座標ファイル D&D の流れ）
11. 運用チューニング（cold start 実測、`--min-instances` の切替、概算月額）
12. セキュリティ上の注意（Signing Secret 管理、ログに SMILES を出さない運用、`/view/{id}` はランダム ID なので URL 共有時の注意）

## §15 参考リンク

- 3Dmol.js（対話的 3D 描画、対応形式 pdb/sdf/mol2/xyz/cube）, https://3dmol.csb.pitt.edu/
- RDKit（ETKDGv3 と MMFF/UFF 最適化）, https://www.rdkit.org/
- OPSIN（IUPAC 名から構造への変換、Java）, https://github.com/dan2097/opsin
- OPSIN Web サービス（EBI、代替経路）, https://opsin.ch.cam.ac.uk/
- py2opsin（OPSIN の Python ラッパー）, https://github.com/JacksonBurns/py2opsin
- OpenBabel（CIF → xyz/pdb 変換用途）, https://github.com/openbabel/openbabel
- Greg Landrum, Adding some chemistry to Slack（RDKit 作者による Slack 連携の参照実装）, https://medium.com/@greg.landrum_t5/adding-some-chemistry-to-slack-73d506ed91de
- 参考の最小実装 smilesbot, https://github.com/sunhwan/smilesbot
- Bolt for Python アプリ構築ガイド, https://docs.slack.dev/tools/bolt-python/building-an-app/
- Slack `response_url` の有効期限と回数制限, https://docs.slack.dev/interactivity/handling-user-interaction#message_responses
- Slack 署名検証（Verifying requests from Slack）, https://api.slack.com/authentication/verifying-requests-from-slack
- FastAPI（Request body、BackgroundTasks）, https://fastapi.tiangolo.com/
- Google Cloud Run（Python）, https://cloud.google.com/run/docs/quickstarts/build-and-deploy/deploy-python-service
- Google Cloud Firestore（TTL policies）, https://cloud.google.com/firestore/docs/ttl
- Cloud Run Secret Manager 統合, https://cloud.google.com/run/docs/configuring/secrets
