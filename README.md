# Molcast — Slack molecule visualization bot

Internal Slack tool for visualising small molecules in 3D, directly from
generated SMILES, IUPAC names, or coordinate files. Built on Cloud Run +
FastAPI + Firestore + RDKit + 3Dmol.js. The full design brief lives in
`Slack_分子可視化ボット_統合版_v2.md`; this README describes how to run,
deploy, and operate the service.

The repository is currently at the end of **Phase 1** (SMILES → 3D
viewer). Phase 2 (coordinate file drag-and-drop) is already exercised
by the viewer JavaScript — no server-side work is required to enable
it. Phase 3 (IUPAC / trivial-name parsing via OPSIN) is scaffolded
(`app/opsin_aliases.json`, `py2opsin` in `requirements.txt`, JRE in
`Dockerfile`) but `app/opsin_utils.py` is not yet implemented.

## 1. Overview

### What it does

- `/mol <SMILES>` → 3D MolBlock is generated server-side; the bot replies
  with `https://<service>/view/<random-id>`. Opening the URL renders the
  molecule with 3Dmol.js (stick / ball-and-stick / sphere views).
- `/mol` (no argument) → bot replies with a link to the bare viewer
  page (`/view/`) where any `pdb` / `sdf` / `mol2` / `xyz` / `cube` file
  can be dropped to render it in-browser.
- `/mol name: <IUPAC name>` → planned for Phase 3.

### What it is NOT for

Quick visual sanity-check only. Conformer searches, energy minimisation
beyond a single MMFF/UFF pass, DFT, MD, and OCSR are explicitly out of
scope; use RDKit / Gaussian / GROMACS for those.

## 2. Slack App setup

1. Open <https://api.slack.com/apps> and click **Create New App** →
   *From scratch*. Name it (e.g. `Molcast`) and pick the target
   workspace.
2. Under **OAuth & Permissions** → *Scopes* → *Bot Token Scopes*, add
   `commands` and `chat:write`.
3. Under **Basic Information**, copy the **Signing Secret**. Store it
   in Secret Manager — see §8.
4. Install the app to the workspace. If *Require approved apps* is on,
   the workspace admin must approve it first.

## 3. Slash command setup

Under **Slash Commands** → *Create New Command*:

| Field | Value |
|---|---|
| Command | `/mol` |
| Request URL | `https://<cloud-run-url>/slack/mol` |
| Short description | `SMILES や座標ファイルから 3D 分子ビューアを生成` |
| Usage hint | `<SMILES> または name: <IUPAC>` |

Save. Re-install the app to the workspace to pick up the new command.

## 4. Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SLACK_SIGNING_SECRET` | yes | — | Slack request signing (HMAC-SHA256) |
| `SLACK_RESPONSE_TYPE` | no | `ephemeral` | `ephemeral` or `in_channel` |
| `BASE_URL` | no | from request | Used for the `/view/{id}` link |
| `FIRESTORE_COLLECTION` | no | `molecules` | Firestore collection name |
| `RETENTION_DAYS` | no | `7` | `/view/{id}` TTL |
| `MAX_ATOMS` | no | `200` | Atom-count gate after `AddHs` |
| `OPSIN_BACKEND` | no | `local` | Phase 3: `local` / `local_only` / `web` |
| `OPSIN_JAR_PATH` | no | `/opt/opsin/opsin.jar` | Phase 3 fallback path |
| `OPSIN_WEB_URL` | no | `https://opsin.ch.cam.ac.uk/opsin/` | Phase 3 EBI endpoint |
| `LOG_LEVEL` | no | `INFO` | Python `logging` level |
| `PORT` | — | Cloud Run injects | Uvicorn bind port |

`EMBED_MAX_RETRIES` is intentionally NOT exposed; the embed retry budget
is fixed at three because each attempt uses its own ETKDGv3 parameter
set (see `app/rdkit_utils.py` and §6.1 of the design brief).

## 5. Local development

```bash
python -m venv .venv
. .venv/bin/activate           # or: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env

# Run unit tests (no Firestore required — store is mocked):
pytest

# Run the service. Without a Firestore credential the /view/{id} and
# /slack/mol POST endpoints will fail at the Firestore call; the
# /health, /view/, /, and viewer endpoints with manual D&D work.
uvicorn app.main:app --reload
```

To exercise the Firestore-backed paths locally, run the Firestore
emulator alongside (after `gcloud components install
cloud-firestore-emulator`):

```bash
gcloud emulators firestore start --host-port=localhost:8088
# In another shell:
export FIRESTORE_EMULATOR_HOST=localhost:8088
uvicorn app.main:app --reload
```

`rdkit` on Windows: the pip wheel works for Python 3.11 (the runtime
matches the Docker image). If pip install fails on your platform, the
Cloud Run build path (`gcloud run deploy --source .`) is the
authoritative way to get a working environment.

## 6. Phase 3 local development

`app/opsin_utils.py` is not yet implemented. When it lands, OPSIN can be
exercised in three modes (`OPSIN_BACKEND`):

- `local` — `py2opsin` first, EBI Web fallback. Default for production.
- `local_only` — `py2opsin` only. Use in CI to catch a missing JRE.
- `web` — EBI Web only. Set this for local development if you do not
  want to install a JRE.

On the host, install Temurin 17 (or any JRE 11+) for the `local` /
`local_only` modes:

```bash
# macOS
brew install --cask temurin@17
# Debian/Ubuntu
sudo apt install default-jre-headless
# Windows: download Temurin from https://adoptium.net/
```

The Dockerfile already includes `default-jre-headless`, so the Cloud
Run image does not need any host-side JRE.

## 7. Docker build

```bash
docker build -t molcast .

# Local smoke test (without Firestore credentials):
docker run --rm -p 8080:8080 \
    -e SLACK_SIGNING_SECRET=dummy \
    -e GOOGLE_APPLICATION_CREDENTIALS=/dev/null \
    molcast
# Open http://localhost:8080/health
```

The image is single-stage (Python + JRE). Phase 3 will not require a
rebuild path change — see `Dockerfile` for the rationale.

## 8. Cloud Run deployment

1. Create the project and enable APIs:

   ```bash
   gcloud config set project YOUR_PROJECT_ID
   gcloud services enable run.googleapis.com \
       firestore.googleapis.com \
       secretmanager.googleapis.com \
       artifactregistry.googleapis.com
   ```

2. Store the Slack signing secret in Secret Manager:

   ```bash
   echo -n "$(read -s SS; echo "$SS")" | \
       gcloud secrets create slack-signing-secret \
           --data-file=- --replication-policy=automatic
   ```

3. Deploy:

   ```bash
   PROJECT_ID=YOUR_PROJECT_ID ./deploy.sh
   ```

4. Note the printed service URL and paste it (with `/slack/mol`
   appended) into the Slack command's *Request URL* field.

## 9. Firestore enablement and TTL

1. In the GCP console, **Firestore → Native mode**, choose region
   `asia-northeast1` (same as Cloud Run). Native mode is required.
2. The code uses `expires_at` for per-request expiry checks, so a TTL
   policy is optional. To enable automatic deletion (with up to 24 h
   delay):

   - **Firestore → TTL → Add policy**
   - Collection: `molecules`
   - Field: `expires_at`

   Until the TTL policy is enabled, expired documents linger but
   `/view/{id}` correctly returns the expired page (the code rejects
   them via the `{"expired": True}` sentinel).

## 10. Usage examples

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

# (Phase 3, not yet implemented)
/mol name: ethanol
  → name: 経路は Phase 3 で対応予定です。
```

For coordinate files (Phase 2), open `/view/` in a browser and drop the
file onto the viewer pane. Supported formats: `pdb`, `sdf`, `mol2`,
`xyz`, `cube`. For CIF, convert at the desk with OpenBabel:

```bash
obabel input.cif -O output.xyz
```

## 11. Operations and tuning

### Cold start

Without `--min-instances 1`, Cloud Run will spin instances down to 0
after a few minutes of idle. The first request after a cold spell pays
the RDKit import (~1 s) plus instance startup (~1–2 s) — well inside
the 3-second Slack `ack` budget thanks to the two-stage flow (ack
synchronously, post the viewer URL via `response_url` afterwards).

If users start reporting late results (>5 s for the viewer URL to
appear), measure cold-start frequency in the Cloud Run logs and, if
warranted, switch to:

```bash
MIN_INSTANCES=1 PROJECT_ID=... ./deploy.sh
```

`--min-instances 1` keeps one instance warm 24/7 — the steady-state
cost is roughly the price of one always-on small Cloud Run instance
(see Cloud Run pricing for `asia-northeast1` vCPU/memory rates).

### Logging

Logs are structured JSON on stdout — Cloud Logging picks them up
automatically. Allowed structured fields are listed in
`app/logging_config.py`; **SMILES bodies and MolBlocks are never logged
in raw form**, only sizes / counts / identifiers.

### `response_url` retries

The async path retries the `response_url` POST twice (exponential
back-off) on 5xx or network errors. Persistent failures land in the
logs under `response_url POST failed after retries` — query Cloud
Logging for that string to spot Slack outages.

## 12. Security notes

- **Signing secret hygiene.** Never put the raw secret in `deploy.sh`,
  `.env`, or any committed file. It lives in Secret Manager and is
  injected at runtime via `--set-secrets` (see `deploy.sh`).
- **No log leakage.** The structured logger only emits whitelisted
  fields. The uvicorn access-log filter masks query strings. If you
  add a new log call, do not pass raw SMILES via `extra=`.
- **`/view/{id}` is unguessable but not authenticated.** Anyone with
  the URL can see the molecule, so treat the URL itself as the secret
  for that view. Phase 1 / 2 / 3 are not designed for confidential
  data (see §1 of the design brief).
- **Atom-count gate.** `MAX_ATOMS=200` after `AddHs` blocks the most
  obvious DoS payload (a kilo-atom SMILES); a malicious caller can
  still spend RDKit CPU on a 199-atom embed retry. If misuse becomes
  visible, add a Cloud Run concurrency cap (`--concurrency`) or
  per-user rate limit.
- **Pre-commit hooks.** This repository ships `hooks/pre-commit` and
  `hooks/commit-msg` which read `.git-banned-patterns` (gitignored) to
  block accidental leakage of personal identifiers. On a fresh clone:

  ```bash
  cp .git-banned-patterns.example .git-banned-patterns
  # edit to taste
  git config core.hooksPath hooks
  ```
