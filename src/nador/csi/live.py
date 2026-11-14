"""Live CSI view (streamer + Streamlit visualizer v9.0) on a node other than the gNB node."""

import asyncio
import atexit
import shlex
import socket
import subprocess
from pathlib import Path

from orchestrator.deployment.base import BaseManagement
from orchestrator.utils.ansible import get_ansible_inventory_line, run_ansible_role

from nador.csi.collection import ROLES, CsiCollector

LIVE_ROOT = "/root/csi_live"  # on the live node


class CsiLiveView:
    """
    Follow the CSI log of a gNB from live_node (e.g. the monitoring node) and show it in Streamlit.

    Only an sshd session and "tail -F" at the lowest priority run on the gNB node. Views of several
    components (gnb, du1, ...) can share live_node: each has its own directory, key and forced command,
    and needs its own port. The view listens
    on 127.0.0.1:port of live_node; open_tunnel() forwards it to the notebook host, like the Grafana
    tunnel of the example notebooks (tunnel_command() gives the same ssh command for a terminal).
    The live copy is for display only: collection still reads the gNB node file.
    """

    def __init__(
        self,
        manager: BaseManagement,
        live_node: str,
        component: str = "gnb",
        *,
        ran_address: str | None = None,
        port: int = 8501,
        window_s: int = 120,
        refresh_s: int = 10,
        max_mb: int = 1024,
        visualizer_src: str = "",
        visualizer_ref: str = "csi-v3.1",
        cpu_list: str = "",
    ) -> None:
        source = CsiCollector(manager, component)  # same checks: CSI enabled, gNB node known
        model = manager.deployment_model
        if live_node not in model.nodes:
            raise ValueError(f"Unknown deployment node {live_node!r}")
        if live_node == source.node_name:
            raise ValueError("The live view must not run on the gNB node")
        self.source = source
        self.live_node = live_node
        self.live = model.nodes[live_node].model_copy(deep=True)
        self.ansible = model.params.ansible.model_copy(deep=True)
        ran_ssh = source.node.ssh_info
        output_dir, file_name = source.src_file.rsplit("/", 1)
        self.port = port
        self.tunnel: subprocess.Popen | None = None
        # Tunnel stderr goes to a file, not a pipe: nobody reads a pipe of a tunnel open for hours.
        self.tunnel_log = Path(self.ansible.runner_path).expanduser() / "csi" / "live" / component / "tunnel.log"
        self.inputs = {
            "csi_ran_host": source.node_name,
            # Address of the gNB node as reached from the live node (same as from the jump host by default)
            "csi_live_ran_address": ran_address or ran_ssh.hostname or source.node_name,
            "csi_live_ran_user": ran_ssh.user,
            "csi_output_dir": output_dir,
            "csi_file_name": file_name,
            "csi_cpu_list": cpu_list,
            "csi_live_port": port,
            "csi_live_window_s": window_s,
            "csi_live_refresh_s": refresh_s,
            "csi_live_max_mb": max_mb,
            "csi_live_visualizer_src": str(Path(visualizer_src).expanduser().resolve()) if visualizer_src else "",
            "csi_live_visualizer_ref": visualizer_ref,
            # One directory, key and forced command per component; one venv for all of them
            "csi_live_dir": f"{LIVE_ROOT}/{component}",
            "csi_live_venv": f"{LIVE_ROOT}/venv",
            "csi_live_source_script": f"/usr/local/bin/csi_live_source_{component}.sh",
            "csi_live_key_comment": f"csi-live-{component}@{live_node}",
        }

    async def _run(self, action: str, quiet: bool) -> dict:
        inventory = "\n".join(
            get_ansible_inventory_line(name, node)
            for name, node in ((self.live_node, self.live), (self.source.node_name, self.source.node))
        )
        return await run_ansible_role(
            role="csi/live",
            runner_path=str(Path(self.ansible.runner_path) / "csi" / "live" / self.source.component),
            inputs=self.inputs | {"csi_live_action": action},
            output_keys=["csi_live_status"] if action == "status" else None,
            roles_paths=[str(ROLES), *self.ansible.roles_paths],
            hosts=self.live_node,
            inventory=inventory,
            become=True,
            quiet=quiet,
        )

    async def start(self, *, quiet: bool = True) -> str:
        """(Re)start the streamer and the viewer, then open the tunnel; return the local URL."""
        await self._run("start", quiet)
        return await self.open_tunnel()

    async def status(self, *, quiet: bool = True) -> str:
        """Processes, size and age of the live copy, last log lines."""
        result = await self._run("status", quiet)
        return "\n".join(result["csi_live_status"])

    async def stop(self, *, quiet: bool = True) -> None:
        """Close the tunnel, stop both processes, revoke the live key and remove the live copy."""
        await self.close_tunnel()
        await self._run("stop", quiet)

    def _tunnel_argv(self, local_port: int) -> list[str]:
        ssh = self.live.ssh_info
        host = ssh.hostname or self.live_node
        # Same options as the Grafana tunnel of the example notebooks: a dedicated connection, no
        # multiplexing (the first -o value wins in ssh, before those of common_args).
        command = ["ssh", "-N", "-T", "-S", "none"]
        for option in (
            "ControlMaster=no",
            "ControlPersist=no",
            "BatchMode=yes",
            "ExitOnForwardFailure=yes",
            "ConnectTimeout=10",
        ):
            command += ["-o", option]
        command += shlex.split(ssh.common_args or "")
        if ssh.private_key_file:
            command += ["-i", str(Path(ssh.private_key_file).expanduser())]
        command += ["-p", str(ssh.port), "-L", f"127.0.0.1:{local_port}:127.0.0.1:{self.port}", f"{ssh.user}@{host}"]
        return command

    def tunnel_command(self, local_port: int | None = None) -> str:
        """The ssh command of open_tunnel(), to run in a terminal instead."""
        return shlex.join(self._tunnel_argv(local_port or self.port))

    async def open_tunnel(self, local_port: int | None = None, timeout_s: float = 20) -> str:
        """Forward 127.0.0.1:local_port of the notebook host to the viewer; return its URL."""
        await self.close_tunnel()
        local_port = local_port or self.port
        # A busy port would make the readiness check below connect to whatever already listens there.
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", local_port))
            except OSError as exc:
                raise RuntimeError(f"Local port {local_port} is already in use; pass local_port=") from exc
        log = self.tunnel_log
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as stderr:  # noqa: ASYNC230 (opened once, inherited by ssh)
            process = subprocess.Popen(  # noqa: ASYNC220, S603 (argv from the deployment model, no shell)
                self._tunnel_argv(local_port), stdout=subprocess.DEVNULL, stderr=stderr
            )
        atexit.register(process.terminate)  # close it when the kernel exits normally
        self.tunnel = process
        async with asyncio.timeout(timeout_s):
            while True:
                if process.poll() is not None:
                    self.tunnel = None
                    error = log.read_text(errors="replace").strip()  # noqa: ASYNC240
                    raise RuntimeError(f"CSI live tunnel failed: {error}")
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", local_port)
                except OSError:
                    await asyncio.sleep(0.2)
                    continue
                writer.close()
                await writer.wait_closed()
                return f"http://127.0.0.1:{local_port}"

    async def close_tunnel(self) -> None:
        """Close the tunnel opened by open_tunnel(), if any."""
        if self.tunnel is not None and self.tunnel.poll() is None:
            self.tunnel.terminate()
            await asyncio.to_thread(self.tunnel.wait, 5)
        self.tunnel = None
