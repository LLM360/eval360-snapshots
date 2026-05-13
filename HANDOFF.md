# eval360-snapshots — Handoff Guide

Everything a new maintainer needs to keep the eval dashboard running.

---

## 1. What this is

**eval360-snapshots** is the eval counterpart to [rl360-snapshots](https://github.com/LLM360/rl360-snapshots). It is a FastAPI dashboard that receives scores from Eval360-V2, stores them in Postgres, and serves a single-page web UI at `https://dashboard.llm360.ai/eval360`.

---

## 2. Infrastructure overview

```
Eval360-V2 (Slurm cluster)
   └─ POST /eval360/api/ingest/eval-result
         │
         ▼
    Cloudflare DNS proxy
    dashboard.llm360.ai
         │
         ▼
    EC2 instance  ──  nginx (routes by path prefix)
         ├── /eval360/  →  eval360-viewer  (port 11003)   ← this repo
         └── /rl360/    →  rl360-viewer    (port 11001)
         │
         ▼
    AWS RDS (Postgres)
    <RDS_HOSTNAME>
    Database: eval360
```

---

## 3. AWS access

### EC2 instance

| Field | Value |
|-------|-------|
| Region | us-east-1 |
| Name | `rl-infra` (shared with rl360) |
| OS user | `ubuntu` (or `ec2-user` depending on AMI) |
| SSH key | [TODO: key pair name — check AWS console EC2 > Key Pairs] |

```bash
ssh -i /path/to/key.pem ubuntu@<EC2_PUBLIC_IP>
```

> EC2 public IP: [TODO — get from AWS Console or `aws ec2 describe-instances --filters "Name=tag:Name,Values=rl-infra"`]

### RDS instance

| Field | Value |
|-------|-------|
| Hostname | `<RDS_HOSTNAME>` — share securely |
| Port | 5432 |
| Database | `eval360` |
| Credentials | Managed by AWS Secrets Manager (see §4) |

### IAM

The EC2 instance role must have `secretsmanager:GetSecretValue` on the RDS secret.  
Secret name: `<RDS_SECRET_NAME>` — share securely (env var `RDS_SECRET_NAME` to override).  

### Cloudflare

- Zone: `llm360.ai`
- Record: `dashboard.llm360.ai` → EC2 public IP (proxied)
- Zero Trust / Access application: `dashboard.llm360.ai/eval360/api/ingest` → **Bypass, Include Everyone**  
  (Required so Eval360-V2 on Slurm can POST without SSO.)

---

## 4. Secrets & credentials

### RDS password — AWS Secrets Manager

The RDS master password is managed by AWS Secrets Manager and **rotates automatically** (every ~90 days or as configured). The server reads credentials at startup and auto-refreshes on rotation (see §9 for the full story).

To read the current secret manually:
```bash
aws secretsmanager get-secret-value \
  --secret-id "$RDS_SECRET_NAME" \
  --region us-east-1 \
  --query SecretString --output text | python3 -m json.tool
```

### Ingest token

Bearer token for ingest and admin endpoints. Stored on EC2 at:
```
~/.config/eval360/env
```

To regenerate:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# Update ~/.config/eval360/env on EC2
# Update EVAL360_DASHBOARD_TOKEN in /mnt/weka/shrd/k2m/eval360/dashboard.env on Weka
systemctl --user restart eval360-viewer
```

### Shared config on Weka

Eval360-V2 reads the dashboard URL and token from:
```
/mnt/weka/shrd/k2m/eval360/dashboard.env
```
Contents:
```bash
EVAL360_DASHBOARD_URL=https://dashboard.llm360.ai/eval360
EVAL360_DASHBOARD_TOKEN=<token>
```

---

## 5. Service management (EC2)

```bash
# Status
systemctl --user status eval360-viewer

# Live logs
journalctl --user -u eval360-viewer -f

# Restart
systemctl --user restart eval360-viewer

# Stop / start
systemctl --user stop eval360-viewer
systemctl --user start eval360-viewer
```

Service file: `~/.config/systemd/user/eval360-viewer.service`  
Environment file: `~/.config/eval360/env`  
Venv: `~/.config/eval360/venv/`

---

## 6. Deployment (pushing changes)

```bash
# On EC2 — deploy a branch
git fetch
git checkout origin/<branch-name> --detach
systemctl --user restart eval360-viewer

# Or, to track master
git pull origin master
systemctl --user restart eval360-viewer
```

Verify Python syntax before deploying:
```bash
python -c "import ast; ast.parse(open('viewer/server.py').read()); print('OK')"
```

---

## 7. Database

### Connect

```bash
# From EC2 (using env file)
source ~/.config/eval360/env
psql $DATABASE_URL

# Or manually
psql "postgresql://<user>:<pass>@<RDS_HOSTNAME>:5432/eval360?sslmode=require"
```

### Apply schema changes

Schema changes are **additive** (new tables / ALTER TABLE appended to end of `schema.sql`):
```bash
source ~/.config/eval360/env
psql $DATABASE_URL -f viewer/schema.sql
```

Never modify existing `CREATE TABLE` statements — use `ALTER TABLE` at the end.

### Key tables

| Table | Purpose |
|-------|---------|
| `models` | Model families |
| `checkpoints` | Per-step (or baseline) evaluable units |
| `eval_results` | Per-(checkpoint, dataset, metric) scores |
| `eval_runs` | Evaluation run provenance |
| `example_results` | Per-example correctness for drill-down |
| `alerts`, `promotion_rules`, `activity_log`, `webhooks` | Phase 5 operational features |

---

## 8. Nginx config (on EC2)

The nginx config routes `/eval360/` to port 11003 and `/rl360/` to port 11001.  
Config location: `/etc/nginx/sites-available/dashboard` (or similar — check `nginx -T | grep eval360`).

After changes: `sudo nginx -t && sudo systemctl reload nginx`

---

## 9. Known issue: RDS credential rotation (key rotation error)

### What happens

AWS Secrets Manager rotates the RDS master password periodically. The old behavior was:
1. Server starts, fetches credentials from Secrets Manager once, builds the connection pool DSN.
2. AWS rotates the secret (new password in Secrets Manager, old connections eventually dropped).
3. The pool tries to open new connections using the stale password → `InvalidPasswordError`.
4. Dashboard starts returning 500 errors. **Manual service restart** was the only fix.

### Fix (committed — `viewer/db.py`)

`db.py` now auto-recovers from rotation events without a restart:

- `init_pool()` tracks whether Secrets Manager was used and records the pool creation timestamp.
- All four query helpers (`fetch`, `fetchrow`, `fetchval`, `execute`) catch `InvalidPasswordError` / `InvalidAuthorizationSpecificationError`.
- On auth failure they call `_refresh_pool_on_rotation()`, which:
  - Acquires an `asyncio.Lock` (one refresh at a time)
  - Applies a **30-second cooldown** (so concurrent failures don't all trigger re-fetches)
  - Re-fetches credentials from Secrets Manager
  - Closes the stale pool and creates a fresh one
- The failing query is then retried once with the new pool.
- If `DATABASE_URL` is set (static DSN), the refresh is a no-op — only the Secrets Manager path triggers it.

### If the dashboard is still broken after this fix is deployed

```bash
# Force a restart to re-fetch credentials immediately
systemctl --user restart eval360-viewer

# Confirm the new password round-trips
aws secretsmanager get-secret-value \
  --secret-id "$RDS_SECRET_NAME" \
  --region us-east-1 \
  --query SecretString --output text | python3 -c "import json,sys; s=json.load(sys.stdin); print(s['username'])"
```

---

## 10. Monitoring / debugging

```bash
# Recent errors
journalctl --user -u eval360-viewer --since "1 hour ago" | grep -i "error\|warn\|exception"

# Check pool refresh happened
journalctl --user -u eval360-viewer | grep "Secrets Manager"

# API health
curl https://dashboard.llm360.ai/eval360/api/models

# Ingest test
curl -X POST https://dashboard.llm360.ai/eval360/api/ingest/eval-result \
  -H "Authorization: Bearer $EVAL360_DASHBOARD_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"checkpoint_id":"test","model_id":"test","dataset_name":"test","metric_name":"accuracy","metric_value":0.5}'
```

---

## 11. Key files

| File | Purpose |
|------|---------|
| `viewer/server.py` | FastAPI app — all endpoints |
| `viewer/db.py` | asyncpg pool, query helpers, credential rotation logic |
| `viewer/schema.sql` | Postgres DDL (append migrations to end) |
| `viewer/index.html` | Full SPA frontend (~2400 lines, no build step) |
| `viewer/mock_server.py` | Standalone demo with fake data (no Postgres) |
| `viewer/backfill.py` | CLI to import `_scores.yaml` files from past eval runs |
| `viewer/systemd/eval360-viewer.service` | systemd unit file template |
| `viewer/systemd/env.example` | Environment variable template |
| `docs/BACKFILL_GUIDE.md` | How to bulk-ingest historical results |

---

## 12. Contacts / related repos

| Resource | Link |
|----------|------|
| This repo | `https://github.com/LLM360/eval360-snapshots` |
| RL dashboard (shares EC2) | `https://github.com/LLM360/rl360-snapshots` |
| Eval framework | `https://github.com/LLM360/Eval360-V2` |
| Dashboard URL | `https://dashboard.llm360.ai/eval360` |
| Previous maintainer | varad0309@gmail.com |
