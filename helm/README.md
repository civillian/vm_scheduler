# vm-scheduler Helm Chart

Deploys the VM power scheduler stack to Kubernetes (AKS) or OpenShift.

## Components

| Component | Description |
|-----------|-------------|
| `api` | FastAPI scheduling API — schedule registration, calendar management |
| `worker` | Celery workers — execute batch power operations |
| `beat` | Celery Beat — fires the every-minute collector tick via redbeat |
| `flower` | Task dashboard — optional, enabled by default |
| `postgresql` | Bundled Postgres subchart |
| `redis` | Bundled Redis subchart |

## Prerequisites

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm dependency update ./helm/vm-scheduler
```

## Installation

### AKS

```bash
helm install vm-scheduler ./helm/vm-scheduler \
  -f helm/values-aks.yaml \
  --set vault.token=your-vault-token \
  --namespace vm-scheduler \
  --create-namespace
```

### OpenShift

```bash
helm install vm-scheduler ./helm/vm-scheduler \
  -f helm/values-openshift.yaml \
  --set vault.token=your-vault-token \
  --set externalDatabase.password=your-db-password \
  --namespace vm-scheduler \
  --create-namespace
```

## OpenShift notes

- Set `openshift.enabled: true` — this drops the `securityContext` block
  entirely, allowing OpenShift's SCC to assign a random UID as required
  by the `restricted` SCC. Setting `runAsUser` will conflict with the
  default SCC and prevent pod scheduling.
- Set `ingress.enabled: false` and `route.enabled: true` — OpenShift uses
  Routes rather than Ingress resources.
- The `route.yaml` template includes a comment about HAProxy IP allowlisting
  for restricting POST/PUT/DELETE access at the route level.
- Ensure the application image is built with a non-root user (uid 1000)
  and that the Dockerfile does not use `USER root` after package installation.

## Beat replica count

Beat is hardcoded to `replicas: 1` in the template. Do not override this
to a higher value — redbeat's distributed lock prevents duplicate firing
if multiple Beat pods run, but a single replica is the correct and safe
default. If Beat crashes, the deployment controller restarts it; the
redbeat lock has a 10-minute TTL, so tick delivery resumes within
10 minutes of a crash at worst.

## Vault token management

Avoid passing `vault.token` as a Helm value (it ends up
in Kubernetes Secret in plaintext). Preferred alternatives:

- **Vault Agent Injector** — sidecar injects the token via annotations
- **External Secrets Operator** — syncs Vault secrets to Kubernetes Secrets
- **OpenShift Vault CSI Driver** — mounts secrets as files

## Upgrading

```bash
helm upgrade vm-scheduler ./helm/vm-scheduler \
  -f helm/values-aks.yaml \
  --set vault.token=your-vault-token
```

Workers perform a warm shutdown (completing in-flight tasks) before
terminating on rolling update — no power operations are interrupted
mid-execution during a deploy.
