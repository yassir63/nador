"""Monitoring deployment lifecycle and workload logs."""

from pathlib import Path
from typing import Any, Literal

from orchestrator.deployment.base import BaseManagement
from orchestrator.deployment.helm import HelmManagement
from orchestrator.utils.ansible import get_ansible_inventory_line, run_ansible_role

from nador.models import MonitoringDeployment, MonitoringModel
from nador.one_way import OneWaySettings
from nador.utils import (
    ASSETS,
    resolve_components,
    resolve_targets,
    run_monitoring_component_action,
)


class MonitoringManagement(HelmManagement):
    """Deploy monitoring components and attachment sniffers."""

    # Take an extension-owned snapshot of an existing orchestrator deployment.
    def __init__(self, preceding: BaseManagement, *, config: MonitoringDeployment | None = None) -> None:
        super().__init__(preceding=preceding)
        self.deployment_model = MonitoringModel.model_validate(self.deployment_model.model_dump(mode="python"))
        if config is not None:
            self.deployment_model.monitoring = config.model_copy(deep=True)

    # Resolve monitoring targets and assemble the Helm installation inputs.
    def plan(self) -> dict[str, Any]:
        """Resolve targets and chart values without contacting deployment hosts."""
        monitoring = self.deployment_model.monitoring
        if monitoring.throughput and not monitoring.targets:
            raise ValueError("Throughput requires AMF, SMF and UPF packet targets")
        node_name, _ = self._get_deployment_node(None)
        targets = resolve_targets(self.deployment_model)
        monitoring.components = resolve_components(self.deployment_model, targets)
        return {
            "control_node": node_name,
            "targets": targets,
        }

    # Resolve configuration and prepare the Kubernetes node and Helm repositories.
    async def prepare(self, *, quiet: bool = False) -> None:
        """Resolve chart values, label the monitoring node and add Helm repositories."""
        self.plan()
        await self._run_tasks("prepare", quiet=quiet)

    # Run monitoring charts in installation order, reversing it for removal.
    async def run_action(
        self,
        action: str,
        *,
        components: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, str | dict[str, str]]:
        """Run a Helm action on selected monitoring components."""
        available = self.deployment_model.monitoring.components
        names = available if components is None else components
        unknown = set(names) - available.keys()
        if unknown:
            raise ValueError(f"Unknown monitoring components: {sorted(unknown)}")
        selected = [(name, available[name]) for name in names]
        if components is None and action in ("get_logs", "run_cmd"):
            selected = [
                (name, component) for name, component in selected if component.is_installed and component.pod_selector
            ]
        if action == "uninstall":
            selected.reverse()
        node = self._get_deployment_node(None)
        output = {}
        for name, component in selected:
            result = await run_monitoring_component_action(
                model=self.deployment_model,
                component=component,
                component_name=name,
                node=node,
                action=action,
                **kwargs,
            )
            if result is not None:
                output[name] = result
        return output

    # Execute a monitoring Ansible action using the selected control node.
    async def _run_tasks(
        self,
        action: Literal["prepare", "inject_sniffers", "set_one_way"],
        *,
        targets: list[dict] | None = None,
        settings: dict | None = None,
        quiet: bool = False,
    ) -> None:
        """Run preparation or sniffer tasks using the Kubernetes master's inventory."""
        monitoring = self.deployment_model.monitoring
        node_name, control_node = self._get_deployment_node(None)
        await run_ansible_role(
            # The role selects prepare.yml or inject_sniffers.yml from the action.
            role="monitoring/stack",
            hosts=node_name,
            inventory=get_ansible_inventory_line(node_name, control_node),
            # Node preparation and sniffer injection run with elevated privileges.
            become=True,
            # Pass configuration and capture targets to the Ansible tasks.
            inputs={
                "monitoring_action": action,
                "monitoring_control_node": node_name,
                "monitoring_config": monitoring.model_dump(mode="json", exclude={"components"}),
                "monitoring_targets": targets or [],
                "one_way_settings": settings,
            },
            extra_params={"name": "Monitoring"},
            runner_path=str(Path(self.deployment_model.params.ansible.runner_path) / "monitoring"),
            roles_paths=[str(ASSETS / "roles"), *self.deployment_model.params.ansible.roles_paths],
            quiet=quiet,
        )

    async def set_one_way(
        self, *, n3: bool | None = None, upf_transit: bool | None = None,
        log_samples: bool | None = None, quiet: bool = False,
    ) -> None:
        """Set the running controller's desired state without reinstalling the stack."""
        monitoring = self.deployment_model.monitoring
        data = monitoring.one_way.model_dump(mode="json")
        if not data["image"]:
            raise ValueError("Provision the one-way controller with one_way.image first")
        changes = {}
        for name, values in (
            ("n3", {"enabled": n3, "log_samples": log_samples}),
            ("upf_transit", {"enabled": upf_transit}),
        ):
            patch = {key: value for key, value in values.items() if value is not None}
            if patch:
                changes[name] = patch
                data[name].update(patch)
        if changes:
            planned = OneWaySettings.model_validate(data)
            await self._run_tasks("set_one_way", settings=changes, quiet=quiet)
            monitoring.one_way = planned

    # Install monitoring components, then inject and start attachment sniffers.
    async def deploy(self, *, quiet: bool = False) -> None:
        """Deploy monitoring and wait for its components and sniffers."""
        plan = self.plan()
        await self._run_tasks("prepare", quiet=quiet)
        await self.install(quiet=quiet)
        if (self.deployment_model.monitoring.latency or self.deployment_model.monitoring.throughput
                or self.deployment_model.monitoring.observability.active
                or self.deployment_model.monitoring.one_way.image):
            await self._run_tasks("inject_sniffers", targets=plan["targets"], quiet=quiet)

    # Uninstall monitoring releases in reverse installation order.
    async def remove(self, *, quiet: bool = False) -> None:
        """Remove monitoring using the current deployment configuration."""
        await self.uninstall(quiet=quiet)
