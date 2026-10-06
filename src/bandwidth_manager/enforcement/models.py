"""Records for one tc transaction. enforced is true only after Linux accepts it."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TcCommand:
    """One argv array. ignore_missing is only for qdisc delete during cleanup."""

    argv: tuple[str, ...]
    ignore_missing: bool = False


@dataclass(frozen=True)
class EnforcementResult:
    """Outcome of apply, remove, or clear.

    enforced is true only when dry_run is false and every command exited 0.
    A dry run never sets enforced.
    """

    enforced: bool
    dry_run: bool
    commands: tuple[tuple[str, ...], ...]
    errors: tuple[str, ...] = ()
    rolled_back: bool = False
