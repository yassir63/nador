# On-demand one-way probes

The original RTT probes are independent of the two one-way measurements.
Omit `one_way` to deploy no one-way infrastructure. To install an idle
controller, set the explicit probe image and leave both switches false.
Use a published tagged image or digest built from the nador-images
`n3-oneway/` source, including the container namespace launcher.

```yaml
one_way:
  image: ghcr.io/yassir63/nador-n3-oneway:extended-ae03044296e26e92d5d4b3a03a87ca89471bdaa8
  sample: 16
  test_port: 5202
  n3:
    enabled: false
    log_samples: false
  upf_transit:
    enabled: false
    peer: 192.0.2.10
```

For a private GHCR package, set `one_way.image_pull_secret` to an
existing registry pull Secret in the monitoring namespace.
In `monitoring.yaml`, configure exactly one gNB or CU-UP and one UPF
packet target, plus AMF and SMF targets for the UE mapper. Set each
target's `n3_interface` if it differs from its packet interface. The
controller uses Multus status for secondary addresses and the pod IPv4
for eth0. Set the UPF's `n6_interface` explicitly when N3 and N6 do
not share an interface. For UPF transit, use the real traffic peer IPv4
and its TCP/UDP service port. The two modes share an image, not a
lifecycle. For different traffic experiments, set `test_port` explicitly.

The monitoring framework renders the initial state from the YAML. On a
running cluster, authorized operators can update the controller ConfigMap
without redeploying the 5G stack, for example:

```python
await monitoring.set_one_way(n3=True, log_samples=True)
await monitoring.set_one_way(n3=False, upf_transit=True)
```

The second call assumes that the UPF transit peer and port were configured.
The equivalent Kubernetes operation for N3 is:

```bash
ns=monitoring
kubectl -n "$ns" get configmap nador-one-way -o json |
  jq '.data["settings.json"] |= (fromjson | .n3.enabled = true | tojson)' |
  kubectl replace -f -
```

Replace `.n3.enabled = true` with `.n3.enabled = false` to stop N3,
or use `.upf_transit.enabled` to change UPF transit independently.
Use `.n3.log_samples` for per-packet clock uncertainty logs. Runtime
ConfigMap edits are overridden by the next YAML-driven Helm upgrade.

The controller starts separate pods on the target nodes, outside the NF
pods, and deletes them when disabled. Node observers require privileged
host PID access and a supported Linux cgroup layout, TC and eBPF. Their
normal termination detaches their own filters. A forced kill or node
failure may require checking TC state before re-enabling. Use a trusted
experimental cluster and restrict access to the monitoring endpoints.
