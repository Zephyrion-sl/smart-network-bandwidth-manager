"""Run tc argv arrays. shell=True is never used.

A command runs only when its device is on the allowlist and, unless the
host namespace was explicitly enabled, only inside the configured netns.
"""

import logging
import shutil
import subprocess
from collections.abc import Callable, Sequence

from bandwidth_manager.enforcement.models import TcCommand

logger = logging.getLogger(__name__)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

_FORBIDDEN = set(";|&$`<>\n\r\\")
_MISSING = ("No such file", "Cannot find device", "not found")


class SafetyError(ValueError):
    """The command would touch something outside the configured lab."""


class TcExecutor:
    def __init__(
        self,
        *,
        allowlist: frozenset[str],
        namespace: str,
        dry_run: bool = False,
        runner: Runner | None = None,
    ) -> None:
        if not allowlist:
            raise ValueError("interface allowlist is empty")
        self._allowlist = allowlist
        self._namespace = namespace
        self.dry_run = dry_run
        self._runner = runner or run_command

    def execute(self, commands: Sequence[TcCommand]) -> tuple[bool, tuple[str, ...]]:
        """Return (ok, errors). ok is false in dry-run: nothing was enforced."""

        errors: list[str] = []
        for command in commands:
            self._check(command.argv)
            if self.dry_run:
                logger.info("dry-run: %s", " ".join(command.argv))
                continue
            try:
                completed = self._runner(command.argv)
            except (OSError, subprocess.TimeoutExpired, SafetyError) as exc:
                logger.error("tc command failed to start: %s", exc)
                errors.append(str(exc))
                break
            if completed.returncode == 0:
                continue
            detail = (completed.stderr or completed.stdout or "").strip()
            if command.ignore_missing and any(phrase in detail for phrase in _MISSING):
                logger.info("qdisc already absent on %s", " ".join(command.argv))
                continue
            logger.error("tc exited %s: %s", completed.returncode, detail)
            errors.append(detail or f"exit {completed.returncode}")
            break
        if self.dry_run:
            return False, tuple(errors)
        return not errors, tuple(errors)

    def query(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
        """Run one read-only argv. None means dry-run, so there is nothing to read."""

        self._check(argv)
        if self.dry_run:
            return None
        return self._runner(argv)

    def _check(self, argv: Sequence[str]) -> None:
        if not argv or len(argv) > 40:
            raise SafetyError("refusing to run an empty or oversized command")
        for arg in argv:
            if not isinstance(arg, str) or arg == "" or any(char in arg for char in _FORBIDDEN):
                raise SafetyError(f"refusing unsafe argument: {arg!r}")
        if argv[0] != "ip" or argv[1:4] != ("netns", "exec", self._namespace) or argv[4] != "tc":
            raise SafetyError("refusing tc outside the configured network namespace")
        devices = _devices(argv)
        if not devices:
            raise SafetyError("tc command does not name a device")
        outside = devices - self._allowlist
        if outside:
            raise SafetyError(f"refusing interface outside the lab allowlist: {sorted(outside)}")


def _devices(argv: Sequence[str]) -> set[str]:
    devices: set[str] = set()
    for index, arg in enumerate(argv):
        if arg == "dev" and index + 1 < len(argv):
            devices.add(argv[index + 1])
    return devices


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Execute one argv array. The shell is not invoked."""

    binary = shutil.which(argv[0])
    if binary is None:
        raise SafetyError(f"required command not found: {argv[0]}")
    return subprocess.run(
        [binary, *argv[1:]],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
        timeout=10,
    )
