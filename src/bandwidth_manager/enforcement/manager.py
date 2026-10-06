"""Apply an allocation by rebuilding the HTB policy on the lab interfaces.

The manager keeps the last installed policy. Applying it again emits the same
tc replace commands. It does not append another filter or class.
"""

import json
import logging
import subprocess
from collections.abc import Mapping, Sequence

from bandwidth_manager.allocation.models import Allocation
from bandwidth_manager.enforcement.commands import TcCommandBuilder
from bandwidth_manager.enforcement.config import QosConfig
from bandwidth_manager.enforcement.executor import SafetyError, TcExecutor
from bandwidth_manager.enforcement.models import EnforcementResult, TcCommand

logger = logging.getLogger(__name__)


class QoSManager:
    def __init__(self, config: QosConfig, *, executor: TcExecutor, builder: TcCommandBuilder) -> None:
        self._config = config
        self._executor = executor
        self._builder = builder
        self._policy: dict[str, int] = {}
        self._known = {client.id for client in config.clients}

    def apply_limit(self, client: str, bandwidth: int) -> EnforcementResult:
        self._require_client(client)
        self._require_bandwidth(bandwidth)
        updated = dict(self._policy)
        updated[client] = bandwidth
        return self._commit(updated)

    def remove_limit(self, client: str) -> EnforcementResult:
        self._require_client(client)
        updated = dict(self._policy)
        updated.pop(client, None)
        return self._commit(updated)

    def apply_allocation(self, allocation: Sequence[Allocation] | Mapping[str, int]) -> EnforcementResult:
        if isinstance(allocation, str):
            raise TypeError("allocation must be a mapping or a sequence of Allocation rows")
        policy: dict[str, int] = {}
        if isinstance(allocation, Mapping):
            items = allocation.items()
        else:
            items = ((row.client_id, row.allocated_bandwidth) for row in allocation)
        for client_id, bandwidth in items:
            self._require_client(client_id)
            if not isinstance(bandwidth, int) or isinstance(bandwidth, bool) or bandwidth < 0:
                raise ValueError(f"bandwidth for {client_id} must be a non-negative int")
            if bandwidth == 0:
                continue
            self._require_bandwidth(bandwidth)
            policy[client_id] = bandwidth
        return self._commit(policy)

    def read_installed(self) -> dict[str, int] | None:
        """Rates currently on the lab HTB classes.

        None means the kernel did not give a parseable answer, including
        dry-run. An empty dict means the show commands succeeded and no
        client class is installed.
        """

        found: dict[str, int] = {}
        saw_output = False
        for command in self._builder.class_show_commands():
            try:
                completed = self._executor.query(command.argv)
            except (OSError, SafetyError, subprocess.TimeoutExpired) as exc:
                logger.error("tc class show failed to start: %s", exc)
                return None
            if completed is None:
                return None
            if completed.returncode != 0:
                logger.error("tc class show exited %s: %s", completed.returncode, completed.stderr)
                return None
            text = (completed.stdout or "").strip()
            if not text:
                return None
            try:
                rows = json.loads(text)
            except json.JSONDecodeError:
                logger.error("tc class show did not return JSON")
                return None
            if not isinstance(rows, list):
                return None
            saw_output = True
            for row in rows:
                client_id, rate = _client_rate(row, self._config)
                if client_id is None or rate is None:
                    continue
                previous = found.get(client_id)
                found[client_id] = rate if previous is None else min(previous, rate)
        if not saw_output:
            return None
        return found

    def clear_all_rules(self) -> EnforcementResult:
        commands = self._builder.clear_commands()
        ok, errors = self._run(commands)
        enforced = ok and not self._executor.dry_run
        if enforced:
            self._policy = {}
            logger.info("cleared HTB qdiscs on %s", ", ".join(self._builder.interfaces()))
        return EnforcementResult(
            enforced=enforced,
            dry_run=self._executor.dry_run,
            commands=tuple(command.argv for command in commands),
            errors=errors,
        )

    def _commit(self, policy: dict[str, int]) -> EnforcementResult:
        commands = self._builder.build(policy)
        ok, errors = self._run(commands)
        if self._executor.dry_run:
            return EnforcementResult(
                enforced=False,
                dry_run=True,
                commands=tuple(command.argv for command in commands),
                errors=errors,
            )
        if ok:
            self._policy = policy
            logger.info("enforced HTB policy for %s", ", ".join(sorted(policy)) or "(no clients)")
            return EnforcementResult(
                enforced=True,
                dry_run=False,
                commands=tuple(command.argv for command in commands),
            )
        rolled_back = self._rollback()
        logger.error("HTB apply failed; rolled_back=%s", rolled_back)
        return EnforcementResult(
            enforced=False,
            dry_run=False,
            commands=tuple(command.argv for command in commands),
            errors=errors,
            rolled_back=rolled_back,
        )

    def _rollback(self) -> bool:
        # Drop every lab qdisc first so a partially installed tree cannot remain,
        # then restore the last policy that Linux accepted.
        commands = list(self._builder.clear_commands())
        if self._policy:
            commands.extend(self._builder.build(self._policy))
        ok, _errors = self._run(commands)
        return ok

    def _run(self, commands: Sequence[TcCommand]) -> tuple[bool, tuple[str, ...]]:
        try:
            return self._executor.execute(commands)
        except SafetyError as exc:
            logger.error("refusing enforcement command: %s", exc)
            return False, (str(exc),)

    def _require_client(self, client: str) -> None:
        if client not in self._known:
            raise ValueError(f"unknown client: {client}")

    def _require_bandwidth(self, bandwidth: int) -> None:
        if not isinstance(bandwidth, int) or isinstance(bandwidth, bool):
            raise TypeError("bandwidth must be an int number of bits per second")
        if bandwidth <= 0 or bandwidth > self._config.max_bandwidth_bps:
            raise ValueError(
                f"bandwidth must be between 1 and {self._config.max_bandwidth_bps} bits per second"
            )


def manager_from_config(config: QosConfig, *, runner=None, dry_run: bool | None = None) -> QoSManager:
    effective_dry_run = config.dry_run if dry_run is None else dry_run
    builder = TcCommandBuilder(
        namespace=config.namespace,
        client_addresses={client.id: client.ipv4 for client in config.clients},
        download_interface={client.id: client.download_interface for client in config.clients},
        upload_interface={client.id: client.upload_interface for client in config.clients},
        class_minor={client.id: client.class_minor for client in config.clients},
        default_class_bps=config.default_class_bps,
    )
    executor = TcExecutor(
        allowlist=config.allowlist(),
        namespace=config.namespace,
        dry_run=effective_dry_run,
        runner=runner,
    )
    return QoSManager(config, executor=executor, builder=builder)


def _client_rate(row: object, config: QosConfig) -> tuple[str | None, int | None]:
    if not isinstance(row, dict) or row.get("kind") != "htb":
        return None, None
    handle = row.get("handle")
    if not isinstance(handle, str) or ":" not in handle:
        return None, None
    try:
        minor = int(handle.split(":")[-1], 16)
    except ValueError:
        return None, None
    client_id = next((client.id for client in config.clients if client.class_minor == minor), None)
    if client_id is None:
        return None, None
    options = row.get("options")
    if not isinstance(options, dict):
        return client_id, None
    return client_id, _bits(options.get("rate"))


def _bits(value: object) -> int | None:
    """tc JSON rate. An integer is already bits per second. kbit is 1000 bits."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    units = (("kbit", 1_000), ("mbit", 1_000_000), ("gbit", 1_000_000_000), ("bit", 1))
    for suffix, scale in units:
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            try:
                rate = float(number) * scale
            except ValueError:
                return None
            return int(rate) if rate > 0 else None
    return None
