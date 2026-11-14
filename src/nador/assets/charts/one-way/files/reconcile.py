"""Reconcile disposable one-way probe pods from the monitoring ConfigMap."""
import hashlib
import ipaddress
import json
import logging
import os
import time

from kubernetes import client, config


SELECTOR = "nador-one-way-managed=true"
MAPPER = "http://ue-mapper-api:8080"
COLLECTOR = "http://nador-one-way-collector:55393"


def anchor(api, target):
    selector = ",".join(f"{k}={v}" for k, v in target["selector"].items())
    pods = api.list_namespaced_pod(target["namespace"], label_selector=selector).items
    ready = [
        (pod, status)
        for pod in pods if not pod.metadata.deletion_timestamp and pod.spec.node_name
        for status in pod.status.container_statuses or []
        if status.name == target["container"] and status.ready and status.container_id
    ]
    if len(ready) != 1:
        raise ValueError(f'{target["component"]}: expected one ready target, found {len(ready)}')
    pod, status = ready[0]
    interface = target.get("n3_interface") or target["interface"]
    networks = json.loads((pod.metadata.annotations or {}).get("k8s.v1.cni.cncf.io/network-status", "[]"))
    ips = [ip for item in networks if item.get("interface") == interface for ip in item.get("ips", [])]
    if not ips and interface == "eth0" and pod.status.pod_ip:
        ips = [pod.status.pod_ip]
    ipv4 = [str(addr) for ip in ips if (addr := ipaddress.ip_interface(ip).ip).version == 4]
    if len(ipv4) != 1:
        raise ValueError(f'{target["component"]}: cannot resolve one IPv4 address on {interface}')
    return {"interface": interface, "ip": ipv4[0], "node": pod.spec.node_name,
            "container_id": status.container_id.split("://", 1)[-1]}


def pod_spec(role, settings, owner_uid, command, *, target=None, metrics_port=None):
    name = f"nador-one-way-{role}"
    fingerprint = hashlib.sha256(json.dumps([settings["image"], command, target], sort_keys=True).encode()).hexdigest()
    annotations = {"nador-one-way-config": fingerprint}
    if metrics_port:
        annotations.update({"prometheus.io/scrape": "true", "prometheus.io/port": str(metrics_port)})
    container = {
        "name": role, "image": settings["image"], "imagePullPolicy": "IfNotPresent",
        "command": command,
        "env": [{"name": "NADOR_ONEWAY_TOKEN", "valueFrom": {
            "secretKeyRef": {"name": "nador-one-way-token", "key": "token"}}}],
    }
    spec = {"restartPolicy": "Always", "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 10, "containers": [container]}
    if target:
        spec.update(nodeName=target["node"], hostPID=True, tolerations=[{"operator": "Exists"}])
        container["securityContext"] = {"privileged": True}
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "labels": {"nador-one-way-managed": "true", "nador-one-way-role": role},
                     "annotations": annotations, "ownerReferences": [{
                         "apiVersion": "v1", "kind": "ConfigMap", "name": "nador-one-way", "uid": owner_uid}]},
        "spec": spec,
    }


def desired_pods(api, settings, targets, owner):
    desired = {}
    n3, upf = settings["n3"], settings["upf_transit"]
    if not n3["enabled"] and not upf["enabled"]:
        return desired
    if not settings.get("image"):
        raise ValueError("Set one_way.image before enabling probes")
    if upf["enabled"] and (not upf.get("peer") or not settings["test_port"]):
        raise ValueError("UPF transit requires peer and test_port")
    if n3["enabled"]:
        cmd = ["python3", "/app/oneway.py", "collector", "--mapper-url", MAPPER]
        if n3["log_samples"]:
            cmd.append("--log-samples")
        desired["collector"] = pod_spec("collector", settings, owner, cmd, metrics_port=55393)
    anchors = {role: anchor(api, targets[role]) for role in ("gnb", "upf")}
    endpoints = ["--gnb", anchors["gnb"]["ip"], "--upf", anchors["upf"]["ip"]]
    sample = ["--sample", str(settings["sample"])]
    if n3["enabled"]:
        for role, target in anchors.items():
            cmd = ["python3", "/app/target_process.py", target["container_id"], "/app/oneway.py",
                   "agent", "--role", role, "--url", COLLECTOR, "--interface", target["interface"],
                   "--test-port", str(settings["test_port"]), *sample, *endpoints]
            desired[role] = pod_spec(role, settings, owner, cmd, target=target)
    if upf["enabled"]:
        target = anchors["upf"]
        cmd = ["python3", "/app/target_process.py", target["container_id"], "/app/upf_transit.py",
               "--n3", target["interface"], "--n6", targets["upf"].get("n6_interface") or target["interface"],
               "--bind", "0.0.0.0", "--peer", upf["peer"], "--port", str(settings["test_port"]),
               "--mapper-url", MAPPER, *sample, *endpoints]
        desired["transit"] = pod_spec("transit", settings, owner, cmd, target=target, metrics_port=55437)
    return desired


def reconcile(api, namespace):
    cm = api.read_namespaced_config_map("nador-one-way", namespace)
    settings, targets = (json.loads(cm.data[key]) for key in ("settings.json", "targets.json"))
    try:
        desired = desired_pods(api, settings, targets, cm.metadata.uid)
    except ValueError as exc:
        logging.warning("One-way probes waiting for valid targets/settings: %s", exc)
        desired = {}
    current = api.list_namespaced_pod(namespace, label_selector=SELECTOR).items
    for pod in current:
        role = pod.metadata.labels["nador-one-way-role"]
        expected = desired.pop(role, None)
        if pod.metadata.deletion_timestamp:
            continue
        current_hash = (pod.metadata.annotations or {}).get("nador-one-way-config")
        if not expected or expected["metadata"]["annotations"]["nador-one-way-config"] != current_hash:
            api.delete_namespaced_pod(pod.metadata.name, namespace, body={
                "preconditions": {"uid": pod.metadata.uid},
            })
    for body in desired.values():
        api.create_namespaced_pod(namespace, body)


def main():
    logging.basicConfig(level=logging.INFO)
    config.load_incluster_config()
    api = client.CoreV1Api()
    while True:
        try:
            reconcile(api, os.environ["NAMESPACE"])
        except Exception:
            logging.exception("One-way reconciliation failed")
        time.sleep(3)


if __name__ == "__main__":
    main()
