# cbioportal-tile-server — Operations Runbook

## Overview

`cbioportal-slide-viewer` is a FastAPI tile server that streams IMPACT pathology whole-slide images (SVS) directly from Dell ECS S3 (`mskmind-bkt`) via `tiffslide`. Slide and patient metadata come from Databricks Unity Catalog. The service is deployed at `https://slides.cbioportal.org`.

---

## Architecture

```
Browser
  └─ HTTPS → slides.cbioportal.org
                └─ nginx ingress (rate-limiting, TLS termination)
                      └─ slide-viewer pods (×2, FastAPI)
                            ├─ Redis          — tile + patient cache
                            ├─ Dell ECS S3    — SVS files (mskmind-bkt)
                            └─ Databricks SQL — slide/patient metadata
```

---

## Deployment

### Prerequisites

1. **Kubernetes secret** — must exist before ArgoCD first sync. Use the names
   consumed by the application; do not put credentials in manifests:
   ```bash
   kubectl create secret generic slide-viewer-secrets \
     --from-literal=AWS_ACCESS_KEY_ID=<ecs-access-key> \
     --from-literal=AWS_SECRET_ACCESS_KEY=<ecs-secret-key> \
     --from-literal=DATABRICKS_HOST=https://msk-mode-prod.cloud.databricks.com \
     --from-literal=DATABRICKS_TOKEN=<pat> \
     --from-literal=WSI_AUTH_SECRET=<at-least-32-byte-secret> \
     --from-literal=REDIS_PASSWORD=<strong-random-password>
   ```

2. **GitHub Actions secret** — for GitOps image-tag pinning:
   - `K8S_DEPLOY_TOKEN`: PAT with `repo` write access to `msk-mind/knowledgesystems-k8s-deployment`

3. **DNS** — CNAME `slides.cbioportal.org` → cluster ingress ALB (infra team).

4. **ECR repository** — `cbioportal-slide-viewer` must exist in account `203403084713`; OIDC role `github-actions-ecr-push` must have `ecr:PutImage` on it.

### ArgoCD Sync

```bash
# Apply the ArgoCD Application (one-time):
kubectl apply -f argocd/aws/203403084713/clusters/cbioportal-prod/apps/argocd/slide-viewer.yaml

# ArgoCD will auto-sync the slide-viewer/ directory.
# Check sync status:
argocd app get slide-viewer
```

### CORS

Set `CORS_ORIGINS` to the internal MSK cBioPortal hosts that embed the viewer:
```yaml
CORS_ORIGINS: "https://cbioportal.mskcc.org,https://triage.cbioportal.mskcc.org"
```

### WSI capability authentication

Every route except `/health` and `/wsi/health` requires:

```text
Authorization: Bearer <short-lived-cbioportal-wsi-jwt>
```

The issuer and tile server must share `WSI_AUTH_SECRET`. Tokens must use
HS256, audience `WSI_AUTH_AUDIENCE` (default `cbioportal-wsi`), scope
`wsi:read`, a non-empty subject, and valid `iat`/`exp` claims. Keep the secret
at least 32 bytes and rotate it by updating the Kubernetes secret and restarting
the deployment. Set `WSI_AUTH_REQUIRED=true` in production; disabling it is
only appropriate for isolated local development.

The service deliberately does not use public caching for metadata. Tile and
thumbnail responses use private browser/proxy caching only; patient metadata,
slide metadata, and search responses use `private, no-store`. Never add a
shared/public cache in front of these endpoints.

Redis must be password-protected in shared environments. Set `REDIS_PASSWORD`
and ensure `REDIS_URL` includes the same credential. Redis contains cached
patient metadata as well as image data, so it is not a public service.

---

## CI/CD

Pushes to `main` or version tags trigger `.github/workflows/docker-publish.yml`:

1. Builds and pushes the Docker image to ECR with tags `sha-<7char>`, `latest`, and semver (on tags).
2. **`update-k8s-manifests` job** — commits the pinned `sha-<7char>` tag into `deployment.yaml` in `knowledgesystems-k8s-deployment`. ArgoCD detects the diff and rolls out automatically.

---

## Adding a New Study

When a cBioPortal study dataset is updated or a new IMPACT study is added, run the migration tool:

```bash
# From the cbioportal-slide-viewer repo root:
python tools/generate_wsi_clinical_attrs.py \
    --study-dir /path/to/private/automation_tool_datasets/<study_id>
python tools/generate_wsi_timepoint_clinical_attrs.py \
    --study-dir /path/to/private/automation_tool_datasets/<study_id>
python tools/generate_resource_patient.py \
    --study-dir /path/to/private/automation_tool_datasets/<study_id> \
    --base-url https://slides.cbioportal.org

# Preview all study changes without deleting or writing study files:
bash tools/migrate_all_studies.sh --dry-run

# Then remove legacy DSA resource files if present:
rm -f <study_dir>/data_resource_sample.txt
rm -f <study_dir>/meta_resource_sample.txt
```

The tool:
- Reads patient IDs from `data_clinical_sample.txt`
- Removes legacy WSI sample and timepoint clinical attributes from `data_clinical_sample.txt`
- Queries the canonical Databricks association table to find patients with pathology rows
- Writes `data_resource_patient.txt`, `meta_resource_patient.txt`, `data_resource_definition.txt`, `meta_resource_definition.txt`
- Writes `wsi_snapshot_manifest.json` with the canonical snapshot metadata used for the study refresh

After running, open a PR in the `private` repo and reload the study in cBioPortal.

**Batch migration** (all MSK studies):
```bash
bash tools/migrate_all_studies.sh
```

---

## Health Checks

```bash
# Liveness (pods should return 200):
curl https://slides.cbioportal.org/health

# Patient metadata (requires a short-lived capability token):
curl -H "Authorization: Bearer $WSI_TOKEN" \
  "https://slides.cbioportal.org/patient/P-0000001" | jq .patient_id

# Tile endpoint (also requires the capability token):
curl -H "Authorization: Bearer $WSI_TOKEN" -o /dev/null -w "%{http_code}" \
  "https://slides.cbioportal.org/tiles/<slide_id>/zxy/0/0/0"
```

---

## Monitoring

| Signal | Where |
|---|---|
| Pod logs | `kubectl logs -l app=slide-viewer --tail=100 -f` |
| Redis memory | `kubectl exec deploy/slide-viewer-redis -- redis-cli info memory` |
| Tile cache hit rate | `kubectl exec deploy/slide-viewer-redis -- redis-cli info stats \| grep keyspace` |
| Ingress rate-limit drops | Nginx ingress controller logs (`kubectl logs -n ingress-nginx`) |

---

## Common Issues

### 502 on `/patient/{id}`
- Databricks warehouse may be asleep (cold start ~30 s) — retry once.
- Check `DATABRICKS_TOKEN` is not expired: `kubectl get secret slide-viewer-secrets -o yaml`
- Check pod logs for `"Databricks query failed"` or `"timed out"`.

### 404 on tile endpoint
- Slide may not have a `slide_inventory` row for the given `image_id`.
- Verify: query `cdsi_eng_phi.pdm_base_tables.slide_inventory WHERE image_id = '<id>'` in Databricks.
- The ECS bucket key may have moved — check `slide_inventory.path`.

### Redis OOM
- Redis uses `allkeys-lru` and evicts old tiles automatically when it reaches its configured `maxmemory` (the local Compose default is 1 GB).
- If eviction rate is very high, consider increasing `SLIDE_VIEWER_REDIS_MAXMEMORY` in `redis.yaml`.

### Ingress rate-limit (HTTP 429)
- A single viewer loading a large WSI can briefly exceed 100 req/s.
- Increase `limit-rps` in `ingress.yaml` or whitelist the origin in nginx config.

---

## Secrets Rotation

### ECS credentials
```bash
kubectl create secret generic slide-viewer-secrets \
  --from-literal=AWS_ACCESS_KEY_ID=<new-key> \
  --from-literal=AWS_SECRET_ACCESS_KEY=<new-secret> \
  --from-literal=DATABRICKS_HOST=https://msk-mode-prod.cloud.databricks.com \
  --from-literal=DATABRICKS_TOKEN=<pat> \
  --from-literal=WSI_AUTH_SECRET=<at-least-32-byte-secret> \
  --from-literal=REDIS_PASSWORD=<strong-random-password> \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/slide-viewer
```

### Databricks PAT
1. Generate a new PAT in Databricks UI (Settings → Developer → Access Tokens).
2. Update the secret as above with the new `DATABRICKS_TOKEN`.
3. Restart the deployment.

### WSI capability and Redis secrets

Rotate `WSI_AUTH_SECRET` only in coordination with the cBioPortal capability
issuer: tokens signed with the previous secret become invalid after restart.
Rotate `REDIS_PASSWORD` together with the application `REDIS_URL` so cache
connectivity is restored. Neither secret should be printed in logs or committed
to the repository.

---

## Rollback

ArgoCD allows instant rollback to any prior synced revision:

```bash
argocd app history slide-viewer
argocd app rollback slide-viewer <revision>
```

Or directly edit `deployment.yaml` to a previous `sha-<7char>` tag and push.
