"""Fleet watchdog logic against a fake runner: no ssh, no tmux, no models."""
from __future__ import annotations

from pathlib import Path

import pytest
from pilgrim.tools import fleetctl
from pilgrim.tools.fleetctl import Ctl, Fleet, Service, load_fleet, pick_model, watch_once


class FakeBox:
    """Holds {service: (session, health)}; answers the scripts fleetctl sends."""

    def __init__(self, fleet: Fleet) -> None:
        self.fleet = fleet
        self.up: dict[str, tuple[bool, bool]] = {n: (True, True) for n in fleet.services}
        self.open_ports: set[tuple[str, int]] = set()
        self.unreachable: set[str] = set()
        self.start_works = True
        self.calls: list[tuple[str, str]] = []

    def _svc(self, host: str, script: str) -> Service:
        return next(s for s in self.fleet.services.values()
                    if s.host == host and s.session in script)

    def __call__(self, svc_host: str, ssh_opts: list[str], script: str,
                 timeout: float) -> tuple[int, str]:
        host = svc_host
        if host in self.unreachable:
            return 255, "ssh: connect to host: timed out"
        if "/dev/tcp/" in script:
            port = int(script.split("/dev/tcp/127.0.0.1/")[1].split(")")[0])
            return 0, "open=1" if (host, port) in self.open_ports else "open=0"
        svc = self._svc(host, script)
        if "new-session" in script:
            self.calls.append(("start", svc.name))
            self.up[svc.name] = (True, self.start_works)
            return 0, ""
        if "kill-session" in script:
            self.calls.append(("stop", svc.name))
            self.up[svc.name] = (False, False)
            return 0, ""
        session, health = self.up[svc.name]
        return 0, f"session={int(session)}\nhealth={int(health)}"


@pytest.fixture
def rig(tmp_path: Path):
    fleet = load_fleet()
    box = FakeBox(fleet)
    t = [1000.0]

    def sleep(s: float) -> None:
        t[0] += s

    ctl = Ctl(fleet, runner=box, sleep=sleep, now=lambda: t[0])
    escalated: list[tuple[str, list[str]]] = []

    def escalate(svc: Service, reason: str, models: list[str]) -> str | None:
        escalated.append((svc.name, models))
        return "log"

    def tick() -> dict[str, str]:
        t[0] += 60
        return watch_once(ctl, tmp_path, escalate)

    return fleet, box, tick, escalated, tmp_path


def test_registry_is_valid_and_sessions_are_identifiable():
    fleet = load_fleet()
    assert {"qwen38", "ornith", "kokoro", "music-spx0", "music-spx1", "music-m5",
            "flux3"} <= set(fleet.services)
    assert all(s.session == "pg-" + n for n, s in fleet.services.items())
    assert fleet.services["sfx"].watch is False


def test_all_up_does_nothing(rig):
    _, box, tick, escalated, _ = rig
    states = tick()
    assert set(states.values()) == {"up"}
    assert box.calls == [] and escalated == []


def test_restart_only_after_threshold(rig):
    fleet, box, tick, _, _ = rig
    box.up["ornith"] = (False, False)
    for _ in range(fleet.defaults.fail_threshold - 1):
        tick()
        assert box.calls == []
    states = tick()
    assert box.calls == [("stop", "ornith"), ("start", "ornith")]
    assert states["ornith"] == "up"


def test_missing_session_with_dead_health_is_down_but_healthy_orphan_is_left(rig):
    fleet, box, tick, _, _ = rig
    box.up["kokoro"] = (False, True)     # started by hand: answers, no pg- session
    for _ in range(fleet.defaults.fail_threshold + 1):
        assert tick()["kokoro"] == "unmanaged"
    assert box.calls == []


def test_session_alive_but_health_dead_is_restarted(rig):
    fleet, box, tick, _, _ = rig
    box.up["qwen38"] = (True, False)     # hung process
    for _ in range(fleet.defaults.fail_threshold):
        tick()
    assert ("start", "qwen38") in box.calls


def test_blocked_by_deepseek_is_never_restarted(rig):
    fleet, box, tick, escalated, _ = rig
    box.up["music-spx0"] = (False, False)
    box.open_ports.add(("spx1", 8888))
    for _ in range(fleet.defaults.fail_threshold + 2):
        assert tick()["music-spx0"] == "blocked"
    assert box.calls == [] and escalated == []


def test_unreachable_host_is_not_restarted(rig):
    fleet, box, tick, escalated, _ = rig
    box.unreachable.add("yuri")
    for _ in range(fleet.defaults.fail_threshold + 2):
        states = tick()
    assert states["ornith"] == "unreachable"
    assert box.calls == [] and escalated == []


def test_pause_file_holds_the_watchdog(rig):
    fleet, box, tick, _, d = rig
    (d / "PAUSE.flux3").touch()
    box.up["flux3"] = (False, False)
    for _ in range(fleet.defaults.fail_threshold + 2):
        tick()
    assert box.calls == []
    (d / "PAUSE.flux3").unlink()
    for _ in range(fleet.defaults.fail_threshold):
        tick()
    assert ("start", "flux3") in box.calls


def test_failed_restart_escalates_once_with_a_model_that_is_not_the_patient(rig):
    fleet, box, tick, escalated, _ = rig
    box.up["qwen38"] = (False, False)
    box.start_works = False
    for _ in range(fleet.defaults.fail_threshold + 3):
        tick()
    assert escalated == [("qwen38", ["Ornith-1.5-9B"])]


def test_restart_budget_then_escalate(rig):
    fleet, box, tick, escalated, _ = rig
    box.start_works = False
    box.up["kokoro"] = (False, False)
    for _ in range(fleet.defaults.fail_threshold + 10):
        tick()
    starts = [c for c in box.calls if c == ("start", "kokoro")]
    assert len(starts) <= fleet.defaults.max_restarts_per_hour
    assert escalated == [("kokoro", ["qwen38"])]


def test_recovery_rearms_escalation(rig):
    fleet, box, tick, escalated, _ = rig
    box.start_works = False
    box.up["kokoro"] = (False, False)
    for _ in range(fleet.defaults.fail_threshold + 1):
        tick()
    assert len(escalated) == 1
    box.up["kokoro"] = (True, True)
    tick()
    box.up["kokoro"] = (False, False)
    for _ in range(fleet.defaults.fail_threshold + 1):
        tick()
    assert len(escalated) == 2


def test_pick_model_skips_down_models():
    fleet = load_fleet()
    kokoro = fleet.services["kokoro"]
    assert pick_model(fleet, kokoro, {"qwen38": "down", "ornith": "up"}) == "Ornith-1.5-9B"
    assert pick_model(fleet, kokoro, {"qwen38": "down", "ornith": "down"}) is None


def test_scripts_name_the_session_and_never_use_sudo_or_reboot():
    fleet = load_fleet()
    for svc in fleet.services.values():
        text = (fleetctl.stop_script(svc, 5)
                + fleetctl.start_script(svc, "bash -lc") + fleetctl.probe_script(svc))
        assert svc.session in text
        assert "sudo" not in text and "reboot" not in text
