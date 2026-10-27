# Trusted registry to CCM Service ACL

`service-acl-sync` refreshes one existing LKE LoadBalancer Service annotation from a validated Trusted Network Registry v1 snapshot. It changes only `service.beta.kubernetes.io/linode-loadbalancer-firewall-acl`; Linode CCM owns the resulting Cloud Firewall and its provider rules. It never calls the Linode API.

The default is a read-only plan. `--execute` permits one guarded annotation replacement. Supply exact `--service-namespace`, `--service-name`, and `--service-uid`, or the corresponding `[service-acl-sync]` TOML fields. The Object Storage source uses the existing `--registry-endpoint-url`, `--registry-bucket`, `--registry-object-key`, and optional `--registry-region` fields. For example:

```sh
linode-image-lab --config /protected/service-acl-sync.toml service-acl-sync
linode-image-lab --config /protected/service-acl-sync.toml service-acl-sync --execute
```

The pod needs `LINODE_OBJ_ACCESS_KEY` and `LINODE_OBJ_SECRET_KEY` from a protected Object Storage read credential Secret. Kubernetes API access uses its mounted ServiceAccount token and CA plus `KUBERNETES_SERVICE_HOST` and `KUBERNETES_SERVICE_PORT`. The LKE repository owns the ServiceAccount, named-Service get/patch RBAC, Secret delivery, Job or CronJob, and Argo deployment; none are installed by this repository.

Before a patch, the command requires a fresh validated registry with at least one active CIDR; the exact Service UID, `LoadBalancer` type, and one TCP port 443; an existing valid CCM ACL annotation; and absence of the `firewall-id` annotation. It compares canonical source CIDRs and does nothing if they match. A patch uses `application/json-patch+json` against `GET /api/v1/namespaces/{namespace}/services/{name}`:

```json
[
  {"op":"test","path":"/metadata/uid","value":"<expected UID>"},
  {"op":"test","path":"/metadata/resourceVersion","value":"<observed version>"},
  {"op":"test","path":"/metadata/annotations/service.beta.kubernetes.io~1linode-loadbalancer-firewall-acl","value":"<observed annotation>"},
  {"op":"replace","path":"/metadata/annotations/service.beta.kubernetes.io~1linode-loadbalancer-firewall-acl","value":"{\"allowList\":{\"ipv4\":[...],\"ipv6\":[...]}}"}
]
```

The command re-fetches the registry just before patch and re-reads the exact UID and annotation afterward. Stale or invalid registry input, changed Service identity, denied access, conflicts, malformed responses, and uncertain writes fail without a permissive fallback. A successful annotation readback confirms the Kubernetes value, not CCM convergence; inspect the CCM-managed firewall separately when operating the deployment. Routine output and errors omit CIDRs, credentials, request bodies, and Service identifiers.

The current `Dockerfile` includes the source package. Build a candidate Linux image from the reviewed source commit and verify `service-acl-sync --help` in that image. Publication of a new digest and deployment are separate owner-controlled steps; the existing `.github/workflows/publish-firewall-consumer.yml` is pinned to an earlier source commit and is not an adapter publication path.

Provider references: [Kubernetes JSON Patch API](https://kubernetes.io/docs/reference/using-api/api-concepts/#patch-operations), [Kubernetes Service API](https://kubernetes.io/docs/reference/kubernetes-api/core/service-v1/), and [Linode CCM Service annotation reference](https://github.com/linode/linode-cloud-controller-manager/blob/main/docs/configuration/annotations.md).
