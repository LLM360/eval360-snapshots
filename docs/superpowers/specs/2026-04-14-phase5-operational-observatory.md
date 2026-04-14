# Phase 5: Operational Observatory + Promotion Gates — Design Spec

**Date:** 2026-04-14
**Status:** Approved

## Summary

Evolve the dashboard into an evaluation operating system: automated regression detection on ingest, promotion gates for checkpoint readiness, checkpoint diff views, activity feed, and optional webhooks.

## Schema

```sql
-- Alerts: regression/improvement/promotion notifications
CREATE TABLE IF NOT EXISTS alerts (
    id              SERIAL PRIMARY KEY,
    alert_type      TEXT NOT NULL CHECK (alert_type IN ('regression', 'improvement', 'promotion_ready')),
    model_id        TEXT NOT NULL REFERENCES models(model_id) ON DELETE CASCADE,
    checkpoint_id   TEXT NOT NULL,
    dataset_name    TEXT,
    severity        TEXT NOT NULL DEFAULT 'info'
                    CHECK (severity IN ('critical', 'warning', 'info')),
    message         TEXT NOT NULL,
    detail          JSONB DEFAULT '{}',
    acknowledged    BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_model ON alerts(model_id);
CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type);
CREATE INDEX IF NOT EXISTS idx_alerts_unack ON alerts(acknowledged) WHERE acknowledged = FALSE;

-- Promotion rules: criteria for checkpoint readiness
CREATE TABLE IF NOT EXISTS promotion_rules (
    id              SERIAL PRIMARY KEY,
    rule_name       TEXT NOT NULL UNIQUE,
    model_id        TEXT,
    suite_id        TEXT,
    min_scores      JSONB DEFAULT '{}',
    no_regressions  BOOLEAN DEFAULT TRUE,
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Activity log: audit trail of dashboard events
CREATE TABLE IF NOT EXISTS activity_log (
    id              SERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL,
    model_id        TEXT,
    checkpoint_id   TEXT,
    dataset_name    TEXT,
    summary         TEXT NOT NULL,
    detail          JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_model ON activity_log(model_id);

-- Webhooks: optional HTTP callbacks on events
CREATE TABLE IF NOT EXISTS webhooks (
    id              SERIAL PRIMARY KEY,
    url             TEXT NOT NULL,
    events          TEXT[] NOT NULL,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

## Regression Detection (on ingest)

After upserting eval results in `POST /api/ingest/eval-result`:

1. Find the **previous checkpoint** for the same model (by training_step ORDER DESC, skip current)
2. Get its primary metric for the same dataset
3. Run CI overlap significance test (reuse Phase 2 logic)
4. If `likely_real` and new < prev → alert `severity=critical`, type=`regression`
5. If `uncertain` and new < prev → alert `severity=warning`, type=`regression`
6. If `likely_real` and new > prev → alert `severity=info`, type=`improvement`
7. Log to activity_log regardless

## Promotion Gates

Rules define criteria. `GET /api/models/{id}/promotion-status` evaluates latest checkpoint against all applicable rules (model-specific + global).

Each rule checks:
- `min_scores`: latest checkpoint must meet minimum score per dataset
- `no_regressions`: no unacknowledged critical regressions for latest checkpoint
- `suite_id`: all datasets in suite must have completed results

Returns: `{checkpoint_id, rules: [{rule_name, passed, failures}], overall: "ready"|"blocked"|"no_rules"}`

## Checkpoint Diff

`GET /api/diff?checkpoint_a=X&checkpoint_b=Y`

No new tables. Joins eval_results for both checkpoints, computes per-dataset delta + significance.

## Activity Feed

`GET /api/activity?limit=50&offset=0&model_id=X`

Populated automatically during ingest and alert creation.

## Webhooks

`POST /api/admin/webhooks` to register. On alert creation, fire best-effort HTTP POST to matching webhooks.

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `GET /api/alerts` | GET | — | List alerts (filters: model_id, type, severity, acknowledged) |
| `POST /api/alerts/{id}/acknowledge` | POST | token | Acknowledge alert |
| `GET /api/activity` | GET | — | Activity feed |
| `GET /api/diff` | GET | — | Checkpoint diff |
| `GET /api/models/{id}/promotion-status` | GET | — | Promotion readiness |
| `POST /api/admin/promotion-rules` | POST | token | Create/update rule |
| `GET /api/admin/promotion-rules` | GET | — | List rules |
| `POST /api/admin/webhooks` | POST | token | Register webhook |

## Frontend

1. **Activity feed** — new tab with timeline of recent events
2. **Alert badges** — red count on model cards for unacknowledged regressions
3. **Checkpoint diff** — pick 2 checkpoints, see side-by-side table with significance
4. **Promotion badge** — green/red indicator on model detail page

## Files Changed

| File | Changes |
|------|---------|
| viewer/schema.sql | 4 new tables + indexes |
| viewer/db.py | insert_alert, insert_activity, query helpers, webhook helpers, promotion queries |
| viewer/server.py | 8 new endpoints, regression detection in ingest, webhook dispatch |
| viewer/mock_server.py | seed alerts/activity/rules, all new endpoints |
| viewer/index.html | activity tab, alert badges, diff view, promotion badge |
