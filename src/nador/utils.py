"""Resolve model components and prepare monitoring Helm values."""

from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from orchestrator.models.provisioning import ProvisionableNode
from orchestrator.utils.ansible import get_ansible_inventory_line, run_ansible_role
from orchestrator.utils.k8s import build_k8s_safe
from orchestrator.utils.utils import deep_merge

from nador.models import DEFAULT_IMAGES, DeployedMonitoringComponent, MonitoringModel
from nador.ran_metrics import ocudu_sources

ASSETS = Path(__file__).resolve().parent / "assets"
CHARTS = ASSETS / "charts"
REMOTE_CHARTS = "/tmp/monitoring-charts"
VALUES_TEMPLATES = Environment(
    loader=FileSystemLoader(ASSETS / "templates"),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


# Resolve components into namespaces, pod selectors and capture settings.
def resolve_targets(model: MonitoringModel) -> list[dict]:
    """Resolve stable selectors from the same release naming used by HelmManagement."""
    result = []
    for target in model.monitoring.targets:
        components = model.ues if target.category == "ue" else getattr(model, target.category).components
        if target.component not in components:
            raise ValueError(f"Unknown {target.category} component {target.component!r}")
        component = components[target.component]
        namespace = getattr(component, "namespace", None)
        if not namespace:
            raise ValueError(f"{target.component}: namespace is unresolved; pass the latest deployed manager")
        if target.role:
            expected = {"amf": {"amf"}, "smf": {"smf"}, "upf": {"upf"}, "gnb": {"gnb", "cu", "cu_up"}}
            if component.type not in expected[target.role]:
                raise ValueError(f"{target.component}: {component.type} cannot be a {target.role} packet target")
        selector = target.selector or {"app": build_k8s_safe(f"{component.implementation}-{target.component}")}
        result.append(
            target.model_dump()
            | {
                "namespace": namespace,
                "implementation": str(component.implementation),
                "selector": selector,
                "label_selectors": [f"{key}={value}" for key, value in selector.items()],
            }
        )
    return result


# Render this chart's dynamic values, then apply explicit user overrides.
def _render_chart_values(
    component: DeployedMonitoringComponent,
    model: MonitoringModel,
    targets: list[dict],
    components: dict[str, DeployedMonitoringComponent],
) -> dict:
    """Render Helm values from chart defaults, deployment settings and user overrides."""
    chart = component.chart_ref.rsplit("/", 1)[-1]
    defaults = ASSETS / "defaults" / f"{chart}.yaml" if component.chart_version else CHARTS / chart / "values.yaml"
    node_selector = component.node_selector
    if component.node_name:
        node_selector = {"kubernetes.io/hostname": component.node_name}
    packet_targets = {target["role"]: target for target in targets if target["role"] in ("gnb", "upf")}
    template = VALUES_TEMPLATES.get_template(f"{chart}.yaml.j2")
    extended_targets = [
        target | {"kind": str(getattr(model, target["category"]).components[target["component"]].type)}
        for target in targets if target["category"] in ("ran", "core")
    ]
    if model.monitoring.extended.active:
        existing = {(target["category"], target["component"]) for target in extended_targets}
        for category in ("ran", "core"):
            for name, nf in getattr(model, category).components.items():
                if (category, name) not in existing and nf.namespace:
                    extended_targets.append({
                        "category": category, "component": name, "namespace": nf.namespace, "kind": str(nf.type),
                        "selector": {"app": build_k8s_safe(f"{nf.implementation}-{name}")},
                    })
    from nador.probe_catalog import probe_values

    probe_name = (component.release_name or "").removeprefix("probe-")
    selected = model.monitoring.extended.probe_targets.get(probe_name)
    if selected:
        available = {f'{t["category"]}/{t["component"]}' for t in extended_targets}
        if set(selected) - available:
            raise ValueError(f"Unresolved probe targets for {probe_name}: {sorted(set(selected) - available)}")
        extended_targets = [t for t in extended_targets if f'{t["category"]}/{t["component"]}' in selected]
    extended_config = (
        probe_values(model.monitoring.extended, probe_name)
        if model.monitoring.extended.probe_images and (component.release_name or "").startswith("probe-")
        else model.monitoring.extended.model_dump(mode="json") | {
            "node_enabled": model.monitoring.extended.node_enabled,
            "service_enabled": model.monitoring.extended.service_enabled,
        }
    )
    rendered = template.render(
        component=component,
        components=components,
        monitoring=model.monitoring,
        model=model,
        targets=targets,
        node_selector=node_selector,
        target_namespaces=sorted({model.monitoring.namespace} | {target["namespace"] for target in targets}),
        ocudu_sources=ocudu_sources(model),
        extended_targets=extended_targets,
        extended_config=extended_config,
        packet_targets=packet_targets,
        packet_namespaces=sorted({target["namespace"] for target in targets if target["role"] in ("gnb", "upf")}),
    )
    values = deep_merge(yaml.safe_load(defaults.read_text()) or {}, yaml.safe_load(rendered))
    return deep_merge(values, component.advanced_config)


# Resolve enabled component models once, then build values directly for each chart.
def resolve_components(model: MonitoringModel, targets: list[dict]) -> dict[str, DeployedMonitoringComponent]:
    """Build enabled monitoring components and their Helm values."""
    monitoring = model.monitoring
    defaults = yaml.safe_load((ASSETS / "components.yaml").read_text())
    if monitoring.core_kpis and str(model.core.implementation) != "open5gs":
        raise ValueError("Core KPI calculator requires Open5GS")
    if monitoring.ran_kpis and str(model.ran.implementation) not in {"oai", "ocudu"}:
        raise ValueError("RAN KPIs support OAI and OCUDU")
    enabled = {"integration", "prometheus", "grafana"}
    if monitoring.one_way.image:
        enabled.update(("one-way", "redis", "ue-mapper-api"))
    if monitoring.extended.node_enabled or monitoring.extended.service_enabled:
        if monitoring.extended.probe_images:
            from nador.probe_catalog import NODE_PROBES, enabled_probes

            for kind in enabled_probes(monitoring.extended):
                name = f"probe-{kind}"
                workload = "node" if kind in NODE_PROBES else "service"
                defaults["components"][name] = {
                    "chart_ref": "extended-probes",
                    "pod_selector": [f"app={name}-extended-{workload}"],
                }
                enabled.add(name)
        else:
            enabled.add("extended-probes")
    for check in monitoring.extended.reachability:
        if check.source_component and check.source_component not in getattr(model, check.source_category).components:
            raise ValueError(f"Unknown source NF for extended check {check.name}: {check.source_component}")
    if monitoring.loki:
        enabled.update(("loki", "promtail"))
    if monitoring.latency or monitoring.throughput or monitoring.observability.active:
        enabled.update(("redis", "ue-mapper-api", "monitoring-controller"))
        if monitoring.packet_pairing:
            enabled.add("packet-pairer")
    if monitoring.core_kpis:
        enabled.add("kpi-calculator-open5gs")
    if monitoring.ran_kpis:
        enabled.add("ocudu-metrics" if str(model.ran.implementation) == "ocudu" else "kpi-calculator-oai")
    components = {}
    for name, definition in defaults["components"].items():
        # The controller also needs the pairer image when pairing is disabled.
        if name not in enabled and not ((monitoring.latency or monitoring.throughput or monitoring.observability.active) and name == "packet-pairer"):
            continue
        data = (
            {"release_name": name, "chart_ref": name, "pod_selector": ["app=" + name]}
            | defaults["defaults"]
            | definition
        )
        if name in DEFAULT_IMAGES["components"]:
            data["image"] = DEFAULT_IMAGES["components"][name].copy()
        if name == "one-way":
            data["image"] = DEFAULT_IMAGES["components"]["monitoring-controller"].copy()
        if name in monitoring.components:
            data = deep_merge(
                data, monitoring.components[name].model_dump(mode="json", exclude_unset=True, exclude_none=True)
            )
        component = DeployedMonitoringComponent.model_validate(data)
        if name == "ue-mapper-api" and monitoring.observability.mapper_coverage:
            image = monitoring.observability.mapper_image
            repository, separator, tag = image.rpartition(":")
            if not separator or not repository or not tag or "/" in tag or "@" in image:
                raise ValueError("mapper_image must use an explicit repository:tag")
            component.image.repository = repository
            component.image.image_tag = tag
        component.namespace = monitoring.namespace
        components[name] = component
    for name in enabled:
        component = components[name]
        component.chart_values = _render_chart_values(component, model, targets, components)
        if not component.chart_version:
            component.chart_ref = f"{REMOTE_CHARTS}/{Path(component.chart_ref).name}"
    return {name: component for name, component in components.items() if name in enabled}


# Execute monitoring's chart role without changing the orchestrator's 5G executor.
async def run_monitoring_component_action(
    *,
    model: MonitoringModel,
    component: DeployedMonitoringComponent,
    component_name: str,
    node: tuple[str, ProvisionableNode],
    action: str,
    command: list[str] | None = None,
    quiet: bool = False,
    containers: list[str] | None = None,
    since_seconds: int = 3600,
) -> str | dict[str, str] | None:
    """Run a component action and record whether its Helm release is installed."""
    monitoring = model.monitoring
    component.namespace = monitoring.namespace
    ansible = model.params.ansible
    node_name, node_data = node
    if action in ("get_logs", "run_cmd") and not component.pod_selector:
        raise ValueError(f"Monitoring component {component_name!r} has no workload")
    if action == "run_cmd" and not command:
        raise ValueError("A command is required for run_cmd")

    output_key = {
        "get_logs": "logs",
        "run_cmd": "command_output",
    }.get(action)
    result = await run_ansible_role(
        role="monitoring/component",
        hosts=node_name,
        inventory=get_ansible_inventory_line(node_name, node_data),
        become=True,
        inputs={
            "helm_action": action,
            "component": component.model_dump(mode="json", exclude_none=True),
            "chart_name": component.release_name or component_name,
            "kubeconfig": monitoring.kubeconfig,
            "timeout_seconds": monitoring.operation_timeout_seconds,
            "since_seconds": since_seconds,
            "containers": containers or [],
            "command": command,
        },
        output_keys=[output_key] if output_key else None,
        runner_path=f"{ansible.runner_path}/monitoring/{component_name}/{action}",
        roles_paths=[str(ASSETS / "roles"), *ansible.roles_paths],
        quiet=quiet,
    )

    if action in ("install", "uninstall"):
        component.is_installed = action == "install"
    elif output_key:
        return result[output_key]
    return None
