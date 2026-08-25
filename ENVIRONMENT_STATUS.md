# Environment Status

## Kubernetes Clusters

| Name | Endpoint | Status | Nodes | Last Qualified |
|------|----------|--------|-------|----------------|
| prod-cluster-1 | https://k8s-prod-1.example.com | qualified | 12 | 2026-08-23T10:30:00+08:00 |
| test-cluster-1 | https://k8s-test-1.example.com | partial | 5 | 2026-08-22T15:20:00+08:00 |

## SSH Hosts

| Name | Endpoint | Status | Acceptance | Last Qualified |
|------|----------|--------|------------|----------------|
| deepseek-host-1 | ssh://192.168.1.100:22 | qualified | 18/20 | 2026-08-23T09:00:00+08:00 |
| deepseek-host-2 | ssh://192.168.1.101:22 | pending | 0/20 | |

## Image Registries

| Name | Endpoint | Status | Projects | Last Qualified |
|------|----------|--------|----------|----------------|
| harbor-main | https://harbor.example.com | qualified | 8 | 2026-08-23T08:00:00+08:00 |

## Model Gateways

| Name | Endpoint | Status | Models | Last Qualified |
|------|----------|--------|--------|----------------|
| openai-gateway | https://api.openai.example.com | qualified | 5 | 2026-08-23T11:00:00+08:00 |
| deepseek-gateway | https://api.deepseek.example.com | error | 0 | |
