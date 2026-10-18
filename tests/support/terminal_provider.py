"""In-process terminal-provider double for daemon integration tests."""

from __future__ import annotations

from dataclasses import dataclass

from theater.daemon.server import Daemon
from theater.models import ProviderRecord, TerminalBindingRecord, now


@dataclass(slots=True)
class FakeProviderTerminal:
    terminal_id: str
    occupant_id: str
    command: str
    cwd: str
    process_id: int
    incarnation: str = "incarnation-1"


class FakeTerminalProvider:
    """In-process callback provider for retained daemon integration tests."""

    provider_id = "provider-fixture"
    selector = "tmux"
    generation = 1
    server_identity = "fixture-provider-server"

    def __init__(self) -> None:
        self.creations: list[dict[str, object]] = []
        self.deliveries: list[tuple[str, str]] = []
        self.interruptions: list[str] = []
        self.terminations: list[str] = []
        self.terminals: list[FakeProviderTerminal] = []
        self.presence: dict[str, str] = {}
        self.screens: dict[str, str | None] = {}
        self._next = 0
        self._report_revision = 1

    @property
    def terminal_ids(self) -> list[str]:
        return [terminal.terminal_id for terminal in self.terminals]

    def install(self, daemon: Daemon) -> None:
        daemon._test_terminal_provider = self
        if daemon.store.providers.get(self.provider_id) is None:
            timestamp = now()
            with daemon.store.write_unit() as unit:
                daemon.store.providers.register(
                    ProviderRecord(
                        provider_id=self.provider_id,
                        selector=self.selector,
                        kind="test",
                        credential_verifier="f" * 64,
                        configuration_version=1,
                        capabilities=("terminal-provider.v1",),
                        limits={"pending_callbacks": 32},
                        generation=self.generation,
                        last_report_revision=0,
                        created_at=timestamp,
                        updated_at=timestamp,
                    ),
                    connection=unit.connection,
                )
        daemon.terminal_service.connections.health = lambda _provider_id: "online"
        daemon.terminal_service.connections.is_current = lambda provider_id, generation: (
            provider_id == self.provider_id and generation == self.generation
        )
        daemon.terminal_service.connections.request = self.request
        daemon.terminal_service.connections.renew = lambda *_args: None
        daemon.terminal_service.connections.mark_online = lambda *_args: None

    def bind(
        self,
        daemon: Daemon,
        participant_id: str,
        *,
        command: str = "vibe",
        terminal_id: str | None = None,
    ) -> str:
        terminal = self.add_terminal(
            terminal_id=terminal_id,
            participant_id=participant_id,
            command=command,
        )
        timestamp = now()
        with daemon.store.write_unit() as unit:
            daemon.store.terminal_bindings.bind(
                TerminalBindingRecord(
                    participant_id=participant_id,
                    provider_id=self.provider_id,
                    provider_generation=self.generation,
                    terminal_id=terminal.terminal_id,
                    terminal_incarnation=terminal.incarnation,
                    occupant_evidence={"occupant_id": participant_id, "harness": command},
                    process_facts={
                        "pid": terminal.process_id,
                        "started_at": 1.0,
                        "executable": f"/usr/bin/{command}",
                    },
                    health="healthy",
                    report_revision=self._report_revision,
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
                connection=unit.connection,
            )
        return terminal.terminal_id

    def add_terminal(
        self,
        terminal_id: str | None = None,
        *,
        participant_id: str | None = None,
        command: str = "vibe",
        cwd: str = "/tmp",
        process_id: int | None = None,
        pid: int | None = None,
    ) -> FakeProviderTerminal:
        self._next += 1
        terminal = FakeProviderTerminal(
            terminal_id=terminal_id or f"terminal-{self._next}",
            occupant_id=participant_id or terminal_id or "unclaimed",
            command=command,
            cwd=cwd,
            process_id=process_id or pid or 10_000 + self._next,
            incarnation=f"incarnation-{self._next}",
        )
        self.terminals = [
            current for current in self.terminals if current.terminal_id != terminal.terminal_id
        ]
        self.terminals.append(terminal)
        return terminal

    def remove_terminal(self, terminal_id: str) -> None:
        self.terminals = [item for item in self.terminals if item.terminal_id != terminal_id]

    async def request(self, provider_id, generation, method, params):
        assert provider_id == self.provider_id and generation == self.generation
        if method == "terminal.create":
            launch = params["launch"]
            terminal = self.add_terminal(
                participant_id=str(params["participant_id"]),
                command=str(launch["executable"]),
                cwd=str(launch["cwd"]),
            )
            self.creations.append(
                {
                    "terminal_id": terminal.terminal_id,
                    "name": launch.get("presentation", {}).get("name"),
                    "cwd": terminal.cwd,
                    "command": list(launch["argv"]),
                    "env": dict(launch["environment"]),
                    "background": launch.get("presentation", {}).get("background", True),
                }
            )
            return {
                "operation_id": params["operation_id"],
                "provider_generation": generation,
                "outcome": "accepted",
                "terminal": self._identity(terminal),
                "launch_id": params["launch_id"],
            }
        terminal = self._terminal(str(params["terminal_id"]))
        if method == "terminal.inspect":
            self._report_revision += 1
            screen_max_bytes = params.get("screen_max_bytes", 0)
            screen = self.screens.get(terminal.terminal_id)
            if screen is not None and screen_max_bytes:
                screen = screen.encode("utf-8")[-screen_max_bytes:].decode("utf-8", "ignore")
            else:
                screen = None
            return {
                "provider_generation": generation,
                "report_revision": self._report_revision,
                "terminal": self._identity(terminal),
                "presence": {
                    "state": self.presence.get(terminal.terminal_id, "absent"),
                    "revision": self._report_revision,
                    "reason": "fixture",
                },
                "mode": "normal",
                "screen": screen,
                "lifecycle": {"alive": True, "authoritative": True},
            }
        if method == "terminal.deliver":
            action = params["action"]
            text = str(action.get("text", ""))
            self.deliveries.append((terminal.terminal_id, text))
        elif method == "terminal.interrupt":
            self.interruptions.append(terminal.terminal_id)
        elif method == "terminal.terminate":
            self.terminations.append(terminal.terminal_id)
            self.remove_terminal(terminal.terminal_id)
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": terminal.terminal_id,
            "terminal_incarnation": terminal.incarnation,
            "delivery": "accepted",
            **({"exit_confirmed": True} if method == "terminal.terminate" else {}),
        }

    def _terminal(self, terminal_id: str) -> FakeProviderTerminal:
        return next(item for item in self.terminals if item.terminal_id == terminal_id)

    def _identity(self, terminal: FakeProviderTerminal) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "provider_generation": self.generation,
            "terminal_id": terminal.terminal_id,
            "terminal_incarnation": terminal.incarnation,
            "occupant": {"occupant_id": terminal.occupant_id, "harness": terminal.command},
            "process": {
                "pid": terminal.process_id,
                "started_at": 1.0,
                "executable": (
                    terminal.command
                    if terminal.command.startswith("/")
                    else f"/usr/bin/{terminal.command}"
                ),
            },
        }
