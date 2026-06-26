# Molcast

研究室の Slack から小分子の 3D 構造を秒で出す。SMILES でも IUPAC 名でも、複数を並べてトラジェクトリ的に切り替えても OK。座標ファイル (`.pdb` / `.sdf` / `.mol2` / `.xyz` / `.cube`) はブラウザに D&D で読ませる。

## 5 秒で試す

Slack で:

```
/mol CCO
```

エタノールの 3D ビューア URL が ephemeral で返ってくる。クリックして開けば、stick / ball & stick / sphere の切替、回転、原子ラベル表示、PNG 保存、SMILES コピーまで一通り使える。

## コマンドの形

### 単体 — SMILES そのまま

```
/mol CCO
/mol C/C=C/C                 # E/Z 立体化学保持
/mol C[C@H](N)C(=O)O         # L-Ala
/mol CC(C)NC(=O)C=C          # NIPAM
```

### 単体 — 名前から (`name:` プレフィクス)

```
/mol name: DMSO              # エイリアス即解決
/mol name: NIPAM             # 同上
/mol name: hexafluorobenzene # OPSIN がパース → ~5-15 秒で返る
/mol name: メタノール        # 日本語のかな表記も alias 登録あり
```

### 複数構造を並べる — トラジェクトリ

`;` 区切りで N 構造を 1 つのビューアに乗せる。ビューア下部の `◀ 1/3 ▶` で送り戻り、矢印キー (`←` / `→`) も効く。

```
/mol CCO ; CC(C)O ; CC(C)(C)O                # 第一・第二・第三アルコール比較
/mol name: NIPAM ; name: NIPMAM ; name: NVCL # 温度応答性モノマー 3 種
/mol CCO ; name: DMSO ; CC#N                 # SMILES と name 混在 OK
```

各フレームに失敗があっても、ナビ上は `✗` 印で残るのでどこで詰まったかが見える (他のフレームの順序は崩れない)。サーバ側の上限は 20 フレーム。超えると先頭 20 個だけ生成して「破棄しました」の警告が末尾に付く。

### 引数なし — ヘルプ + 座標ファイル D&D

```
/mol
```

使い方の短文と、座標ファイル D&D 用のビューア URL (`/view/`) が返ってくる。そのページに `.pdb` / `.sdf` / `.mol2` / `.xyz` / `.cube` ファイルをドロップするとそのまま描画される。CIF は手元で OpenBabel で変換する:

```bash
obabel input.cif -O output.xyz
```

## フラグ

スラッシュコマンドの末尾 (or 途中) にトークンとして付ける。順不同。

| フラグ | 効果 |
|---|---|
| `--public` | 結果をチャンネル全員に投稿 (`response_type=in_channel`)。デフォルトは ephemeral (自分にしか見えない) |
| `--label` | ビューアを開いた時点で原子ラベル表示 ON。後からビューア上のボタンで切り替えてもよい |
| `--no-3d` | 3Dmol.js の代わりに RDKit の 2D 描画 (SVG inline) を返す。Slack でサクッと構造式画像を共有したいとき向け |

複数同時 OK:

```
/mol CCO ; CC(C)O --public --label --no-3d
```

このフラグセットは全フレーム共通で適用される。

## ビューアのボタン

| ボタン | 動作 |
|---|---|
| Stick | 棒モデル表示 |
| Ball & Stick | 球棒モデル (デフォルト) |
| Sphere | 球モデル (van der Waals 半径風) |
| Reset | 初期視点に戻す (位置・回転・ズーム全部) |
| Labels | 原子記号を表示 ON/OFF。Shift+クリックで H も含めるモードに切替 (デフォルトは H 省略) |
| Rotate | y 軸まわりの自動回転 ON/OFF |
| Copy SMILES | 現在フレームの SMILES をクリップボードへ |
| Save PNG | 現在フレームの viewer 画像を `molcast-<id>-<frame>.png` で DL |
| ◀ / ▶ | フレーム送り (トラジェクトリ時のみ表示)。`←` / `→` キーでも切替可 |

右上の半透明オーバーレイに **分子式 / 分子量 / SMILES** が常時出る。フレーム切替で内容も更新される。

## URL クエリ

ビューア URL 自体に直接付けても効く。Slack からのリンクと同じ場所に飛ぶ:

| クエリ | 効果 |
|---|---|
| `?label=1` | 開いた時点で原子ラベル ON (= `--label`) |
| `?rotate=1` | 開いた時点で自動回転 ON |
| `?mode=2d` | 2D SVG 表示に切り替え (= `--no-3d`) |
| `?frame=3` | 指定フレームから開始 (1-indexed、トラジェクトリ時のみ) |

組み合わせ可: `?label=1&rotate=1&frame=2`

## エイリアス

`name:` 経路で OPSIN を呼ばずに即解決される慣用名・略号は `app/opsin_aliases.json` にカテゴリ分けで 100 件強登録されている:

| カテゴリ | 例 |
|---|---|
| solvent | water / H2O / 水, methanol / MeOH / メタノール, DMSO, THF, DMF, NMP, HFIP, D2O, ... |
| vinyl_monomer | styrene, MMA, NIPAM, NIPMAM, DMAA, DEAA, NVP, NVCL, MEO2MA, EGDMA, ... |
| sugar | glucose, fructose, sucrose, trehalose, sorbitol, cellobiose, ... |
| host_compound | 12-crown-4 / 15-crown-5 / 18-crown-6, α/β/γ-CD, [2.2.2]cryptand, ... |
| initiator | AIBN, KPS, SPS, APS, V-50 |
| buffer | Tris, HEPES, MES |
| salt | NaCl, KCl, LiCl, NaBr |

エイリアスは英名・日本語名 (カナ)・略号・別記法など複数を 1 SMILES に紐付けてある。引いて出てこなければ OPSIN local → EBI OPSIN Web の順にフォールバックして体系名としてパースを試みる。

不足を感じたら `app/opsin_aliases.json` に PR で追加 (RDKit round-trip テストが CI で走るので、不正な SMILES は弾かれる)。

## エラーメッセージと対処

| メッセージ | 状況 |
|---|---|
| `SMILES の解釈に失敗しました。表記をご確認ください。` | RDKit が SMILES をパースできない。typo の可能性大 |
| `分子が大きすぎます (上限 200 原子)。座標ファイルを直接ビューアに D&D してください。` | `AddHs` 後の重原子+H 合計 >200。大きい分子は座標ファイル経由で |
| `3D 構造の生成に失敗しました。立体的に困難な構造の可能性があります。` | ETKDGv3 が 3 回試して embed できなかった。マクロ環や混雑した cage 系で起こる。座標ファイル D&D 推奨 |
| `OPSIN は体系名のみ対応です。慣用名・商品名は解釈できません。SMILES を直接入力するか、登録済みエイリアスをお使いください。` | `name:` で渡した文字列を OPSIN がパース不能。具体例 3 行付きで返ってくるのでそれを参照 |
| `保存に失敗しました。しばらく待ってから再度お試しください。` | Firestore 書き込み失敗。一時的な GCP 不調が多い。再投入で大抵通る |
| `アプリが応答しなかった` (Slack 側表示) | コールドスタート + 3 秒 ack 窓超過。同じコマンドを 2 回目に叩けば warm インスタンスで通る |

## 制限事項

- **ビューア URL の有効期限は 7 日**。過ぎると "この 3D ビューアは期限切れです" のページに切り替わる。必要なら `/mol` を叩き直して再生成する
- **`/view/{id}` は推測不能だが認証なし**。URL を知っている人なら誰でも見える。機密データ向けではない
- **同期パス (SMILES 単体・SMILES のみのトラジェクトリ) は Slack 3 秒 ack 窓に依存**。コールドスタートで超えうる。`name:` 含む場合は Cloud Tasks の二段化で 3 秒制約から外れる
- **2D 描画 (`--no-3d`)** は 600×480 の SVG、立体化学のくさび結合表示はあるが H は省略
- **配座探索・MMFF/UFF 最適化を超えた構造**は出ない。可視化ツールであって計算ツールではない。本格的にやるなら RDKit / Gaussian / GROMACS

## さらに知る

- ビューア URL / Slack コマンド / Firestore TTL の細部 → `Slack_分子可視化ボット_統合版_v2.md` (設計ブリーフ)
- Cloud Run へのデプロイ手順 → `DEPLOY.md`
- ローカル開発・テスト・環境変数・OPSIN バックエンド切替 → `DEVELOPMENT.md`
- alias の生 SMILES → `app/opsin_aliases.json`
