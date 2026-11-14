"""Model fleet control + watchdog (operator tool, docs/systems/model-fleet.md §6).

Every model service runs in a tmux session named `pg-<service>` on its host.
The registry is `pilgrim/deploy/model-fleet/fleet.yaml`. This tool is the only
thing that starts, stops or restarts a model; the station never imports it.

    python -m pilgrim.tools.fleetctl status
    python -m pilgrim.tools.fleetctl restart ornith   # stop, start in pg-ornith, wait for /health
    python -m pilgrim.tools.fleetctl stop music-spx0
    python -m pilgrim.tools.fleetctl pause [service]  # watchdog leaves it (or everything) alone
    python -m pilgrim.tools.fleetctl resume [service]
    python -m pilgrim.tools.fleetctl watch            # one watchdog pass (launchd, every minute)

Watchdog rule: a watched service is healthy when its session exists AND its
/health answers. `fail_threshold` unhealthy passes in a row trigger a restart;
a restart that does not come back, or more than `max_restarts_per_hour`,
escalates once per incident to a pi agent in the tmux session `pg-fix-<service>`
on this Mac, pointed at `pilgrim/deploy/model-fleet/RESTART.md`.

A service that answers /health without its session was started by hand: it is
reported as `unmanaged` and left alone (a working model is never killed to
tidy it up; `restart` adopts it).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "pilgrim" / "deploy" / "model-fleet" / "fleet.yaml"
RUNBOOK = ROOT / "pilgrim" / "deploy" / "model-fleet" / "RESTART.md"
SESSION_PREFIX = "pg-"
FIX_PREFIX = "pg-fix-"
REMOTE_LOG_DIR = "$HOME/pg-fleet-logs"
SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
SSH_UNREACHABLE = 255


class BlockedBy(BaseModel):
    host: str
    port: int


class Defaults(BaseModel):
    fail_threshold: int = 3
    max_restarts_per_hour: int = 3
    stop_grace_s: int = 20
    login_shell: str = "bash -lc"


class Escalation(BaseModel):
    enabled: bool = True
    models: list[str] = Field(default_factory=list)
    provider: str = "litellm"
    timeout_s: int = 1800


class Service(BaseModel):
    name: str = ""
    host: str
    ssh_opts: list[str] = Field(default_factory=list)
    cwd: str
    start: str
    ports: list[int] = Field(min_length=1)
    budget_s: int = 180
    watch: bool = True
    login_shell: str | None = None
    blocked_by: BlockedBy | None = None
    pi_model: str | None = None

    @property
    def session(self) -> str:
        return SESSION_PREFIX + self.name


class Fleet(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    escalation: Escalation = Field(default_factory=Escalation)
    services: dict[str, Service]


def load_fleet(path: Path = REGISTRY) -> Fleet:
    fleet = Fleet.model_validate(yaml.safe_load(path.read_text()))
    for name, svc in fleet.services.items():
        svc.name = name
    return fleet


class Runner(Protocol):
    def __call__(self, svc_host: str, ssh_opts: list[str], script: str,
                 timeout: float) -> tuple[int, str]: ...


def shell_runner(svc_host: str, ssh_opts: list[str], script: str,
                 timeout: float) -> tuple[int, str]:
    """Run a bash script on `local` or over ssh. (rc, combined output);
    rc 255 = host unreachable, 124 = timed out."""
    if svc_host == "local":
        cmd = ["/bin/bash", "-c", script]
    else:
        cmd = [*SSH_BASE, *ssh_opts, svc_host, "bash -c " + shlex.quote(script)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return p.returncode, (p.stdout + p.stderr).strip()


# --- scripts run on the model's own host -------------------------------------

_PIDS_ON = r"""pids_on() {
  if command -v lsof >/dev/null 2>&1; then lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null
  else ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u
  fi
}
"""


def probe_script(svc: Service) -> str:
    return (
        f"tmux has-session -t ={svc.session} 2>/dev/null && echo session=1 || echo session=0\n"
        f"curl -sf -m 5 -o /dev/null http://127.0.0.1:{svc.ports[0]}/health "
        "&& echo health=1 || echo health=0\n")


def port_script(port: int) -> str:
    return f"(exec 3<>/dev/tcp/127.0.0.1/{port}) 2>/dev/null && echo open=1 || echo open=0\n"


def stop_script(svc: Service, grace_s: int) -> str:
    ports = " ".join(str(p) for p in svc.ports)
    return _PIDS_ON + f"""
tmux kill-session -t ={svc.session} 2>/dev/null && echo "killed session {svc.session}"
for port in {ports}; do
  for pid in $(pids_on $port); do
    echo "TERM $pid ($(ps -o comm= -p $pid 2>/dev/null)) on :$port"; kill $pid 2>/dev/null
  done
done
for _ in $(seq 1 {grace_s}); do
  left=""; for port in {ports}; do left="$left$(pids_on $port)"; done
  [ -z "$left" ] && break; sleep 1
done
for port in {ports}; do
  for pid in $(pids_on $port); do echo "KILL $pid on :$port"; kill -9 $pid 2>/dev/null; done
done
sleep 1
for port in {ports}; do
  [ -n "$(pids_on $port)" ] && {{ echo "port $port still held"; exit 1; }}
done
exit 0
"""


def start_script(svc: Service, login_shell: str) -> str:
    log = f"{REMOTE_LOG_DIR}/{svc.name}.log"
    inner = (f"cd {shlex.quote(svc.cwd)} && echo \"=== $(date) fleetctl start {svc.name}\" "
             f">> {log} && {{ {svc.start} ; }} 2>&1 | tee -a {log}")
    pane = f"exec {login_shell} {shlex.quote(inner)}"
    return (f"mkdir -p {REMOTE_LOG_DIR}\n"
            f"tmux has-session -t ={svc.session} 2>/dev/null && "
            f"{{ echo 'session {svc.session} already exists'; exit 1; }}\n"
            f"tmux new-session -d -s {svc.session} -c {shlex.quote(svc.cwd)} {shlex.quote(pane)}\n")


# --- operations ---------------------------------------------------------------

class Probe(BaseModel):
    state: str          # up | down | unmanaged | blocked | unreachable
    session: bool = False
    health: bool = False
    detail: str = ""


class Ctl:
    def __init__(self, fleet: Fleet, runner: Runner = shell_runner,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.time) -> None:
        self.fleet = fleet
        self.run = runner
        self.sleep = sleep
        self.now = now

    def _svc_run(self, svc: Service, script: str, timeout: float) -> tuple[int, str]:
        return self.run(svc.host, svc.ssh_opts, script, timeout)

    def blocked(self, svc: Service) -> bool:
        b = svc.blocked_by
        if b is None:
            return False
        opts = next((s.ssh_opts for s in self.fleet.services.values() if s.host == b.host), [])
        rc, out = self.run(b.host, opts, port_script(b.port), 20)
        return rc == 0 and "open=1" in out

    def probe(self, svc: Service) -> Probe:
        rc, out = self._svc_run(svc, probe_script(svc), 30)
        if rc in (SSH_UNREACHABLE, 124) or "session=" not in out:
            return Probe(state="unreachable", detail=out[-200:])
        session, health = "session=1" in out, "health=1" in out
        if session and health:
            state = "up"
        elif health:
            state = "unmanaged"
        elif self.blocked(svc):
            state = "blocked"
        else:
            state = "down"
        return Probe(state=state, session=session, health=health)

    def stop(self, svc: Service) -> tuple[bool, str]:
        grace = self.fleet.defaults.stop_grace_s
        rc, out = self._svc_run(svc, stop_script(svc, grace), grace + 40)
        return rc == 0, out

    def start(self, svc: Service) -> tuple[bool, str]:
        """Start in tmux and wait for /health up to the cold-start budget."""
        shell = svc.login_shell or self.fleet.defaults.login_shell
        rc, out = self._svc_run(svc, start_script(svc, shell), 40)
        if rc != 0:
            return False, out
        deadline = self.now() + svc.budget_s
        while self.now() < deadline:
            self.sleep(5)
            p = self.probe(svc)
            if p.state == "up":
                return True, out
            if p.state == "down" and not p.session:
                return False, f"session {svc.session} exited before /health answered"
        return False, f"no /health within {svc.budget_s}s"

    def restart(self, svc: Service) -> tuple[bool, str]:
        ok, out = self.stop(svc)
        if not ok:
            return False, "stop failed: " + out
        ok2, out2 = self.start(svc)
        return ok2, (out + "\n" + out2).strip()


# --- watchdog state -------------------------------------------------------------

def state_dir() -> Path:
    base = Path(os.environ.get("PILGRIM_ORIGIN_STATE") or Path.home() / "pilgrim-origin")
    d = base / "fleet"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def locked(d: Path) -> Iterator[None]:
    with open(d / "lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_state(d: Path) -> dict[str, Any]:
    try:
        return dict(json.loads((d / "state.json").read_text()))
    except (OSError, ValueError):
        return {}


def save_state(d: Path, state: dict[str, Any]) -> None:
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(d / "state.json")


def paused(d: Path, name: str) -> bool:
    return (d / "PAUSE").exists() or (d / f"PAUSE.{name}").exists()


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}),
          flush=True)


Escalate = Callable[[Service, str, list[str]], str | None]


def pick_model(fleet: Fleet, patient: Service, states: dict[str, str]) -> str | None:
    """First escalation model whose backing service is up and is not the patient."""
    backing = {s.pi_model: s for s in fleet.services.values() if s.pi_model}
    for model in fleet.escalation.models:
        svc = backing.get(model)
        if svc is None or (svc.name != patient.name and states.get(svc.name) == "up"):
            return model
    return None


def spawn_pi(fleet: Fleet) -> Escalate:
    def escalate(svc: Service, reason: str, models: list[str]) -> str | None:
        if not models:
            return None
        session = FIX_PREFIX + svc.name
        log_dir = Path.home() / "Library" / "Logs" / "pilgrim"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"fleet-fix-{svc.name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        prompt = (f"The model service `{svc.name}` on host `{svc.host}` is down and the "
                  f"scripted restart did not fix it. Reason: {reason}. "
                  "Follow the runbook in your system prompt and finish with the report "
                  "it asks for.")
        pi = (f"pi -p --no-session --no-context-files "
              f"--model {shlex.quote(fleet.escalation.provider + '/' + models[0])} "
              f"--append-system-prompt {shlex.quote(str(RUNBOOK))} {shlex.quote(prompt)}")
        inner = (f"cd {shlex.quote(str(ROOT))} && {pi} < /dev/null 2>&1 "
                 f"| tee -a {shlex.quote(str(log))}")
        pane = f"exec zsh -lc {shlex.quote(inner)}"
        subprocess.run(["tmux", "kill-session", "-t", "=" + session], capture_output=True)
        p = subprocess.run(["tmux", "new-session", "-d", "-s", session, pane],
                           capture_output=True, text=True)
        return str(log) if p.returncode == 0 else None
    return escalate


def reap_fix_sessions(fleet: Fleet, state: dict[str, Any], now: float) -> None:
    for name, st in state.items():
        started = st.get("escalated_at")
        if started and now - started > fleet.escalation.timeout_s and not st.get("fix_reaped"):
            subprocess.run(["tmux", "kill-session", "-t", "=" + FIX_PREFIX + name],
                           capture_output=True)
            st["fix_reaped"] = True


def watch_once(ctl: Ctl, d: Path, escalate: Escalate) -> dict[str, str]:
    """One watchdog pass over every watched service. Returns {service: state}."""
    fleet = ctl.fleet
    state = load_state(d)
    watched = [s for s in fleet.services.values() if s.watch]
    with ThreadPoolExecutor(max_workers=max(1, len(watched))) as pool:
        probes = dict(zip([s.name for s in watched], pool.map(ctl.probe, watched), strict=True))
    states = {n: p.state for n, p in probes.items()}

    def handle(svc: Service) -> None:
        st = state.setdefault(svc.name, {})
        p = probes[svc.name]
        if p.state != st.get("state"):
            emit("fleet.state", service=svc.name, state=p.state, was=st.get("state"),
                 session=p.session, health=p.health)
        st["state"] = p.state
        if p.state in ("up", "unmanaged", "blocked"):
            st.update(fails=0, escalated_at=None, fix_reaped=False)
            return
        if p.state == "unreachable" or paused(d, svc.name):
            return
        st["fails"] = int(st.get("fails") or 0) + 1
        if st["fails"] < fleet.defaults.fail_threshold:
            return
        now = ctl.now()
        recent = [t for t in st.get("restarts") or [] if now - t < 3600]
        reason = ""
        if len(recent) >= fleet.defaults.max_restarts_per_hour:
            reason = f"{len(recent)} restarts in the last hour and it is down again"
        else:
            st["restarts"] = [*recent, now]
            emit("fleet.restart", service=svc.name, host=svc.host, fails=st["fails"])
            ok, out = ctl.restart(svc)
            emit("fleet.restart.done", service=svc.name, ok=ok, detail=out[-400:])
            if ok:
                st.update(state="up", fails=0)
                states[svc.name] = "up"
                return
            reason = "restart failed: " + out[-300:]
        if fleet.escalation.enabled and not st.get("escalated_at"):
            model = pick_model(fleet, svc, states)
            log = escalate(svc, reason, [model] if model else [])
            st.update(escalated_at=now, fix_reaped=False)
            emit("fleet.escalate", service=svc.name, model=model, log=log, reason=reason)

    with ThreadPoolExecutor(max_workers=max(1, len(watched))) as pool:
        list(pool.map(handle, watched))
    reap_fix_sessions(fleet, state, ctl.now())
    save_state(d, state)
    return states


# --- CLI ------------------------------------------------------------------------

def cmd_status(ctl: Ctl, d: Path, as_json: bool) -> int:
    svcs = list(ctl.fleet.services.values())
    with ThreadPoolExecutor(max_workers=len(svcs)) as pool:
        probes = list(pool.map(ctl.probe, svcs))
    state = load_state(d)
    rows = []
    for svc, p in zip(svcs, probes, strict=True):
        st = state.get(svc.name) or {}
        rows.append({"service": svc.name, "host": svc.host, "session": svc.session,
                     "port": svc.ports[0], "state": p.state,
                     "watch": svc.watch and not paused(d, svc.name),
                     "fails": int(st.get("fails") or 0),
                     "restarts_1h": len([t for t in st.get("restarts") or []
                                         if time.time() - t < 3600])})
    if as_json:
        print(json.dumps(rows, indent=1))
    else:
        print(f"{'SERVICE':<12} {'HOST':<6} {'SESSION':<15} {'PORT':<6} {'STATE':<12} "
              f"{'WATCH':<6} FAILS RESTARTS/1h")
        for r in rows:
            print(f"{r['service']:<12} {r['host']:<6} {r['session']:<15} {r['port']:<6} "
                  f"{r['state']:<12} {'yes' if r['watch'] else 'no':<6} "
                  f"{r['fails']:<5} {r['restarts_1h']}")
    bad = [r for r in rows if r["watch"] and r["state"] not in ("up", "blocked")]
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fleetctl", description=__doc__.splitlines()[0])
    ap.add_argument("--registry", type=Path, default=REGISTRY)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    for name in ("start", "stop", "restart"):
        sub.add_parser(name).add_argument("service")
    for name in ("pause", "resume"):
        sub.add_parser(name).add_argument("service", nargs="?")
    sub.add_parser("watch")
    args = ap.parse_args(argv)

    fleet = load_fleet(args.registry)
    ctl = Ctl(fleet)
    d = state_dir()
    if args.cmd == "status":
        return cmd_status(ctl, d, args.json)
    if args.cmd == "watch":
        with locked(d):
            states = watch_once(ctl, d, spawn_pi(fleet))
        return 0 if all(v in ("up", "blocked") for v in states.values()) else 1
    if args.cmd in ("pause", "resume"):
        if args.service and args.service not in fleet.services:
            ap.error(f"unknown service {args.service}")
        flag = d / (f"PAUSE.{args.service}" if args.service else "PAUSE")
        if args.cmd == "pause":
            flag.touch()
        else:
            flag.unlink(missing_ok=True)
        print(f"{args.cmd}d {args.service or 'the whole watchdog'}")
        return 0
    if args.service not in fleet.services:
        ap.error(f"unknown service {args.service}; one of {', '.join(fleet.services)}")
    svc = fleet.services[args.service]
    with locked(d):
        ok, out = {"start": ctl.start, "stop": ctl.stop, "restart": ctl.restart}[args.cmd](svc)
        state = load_state(d)
        st = state.setdefault(svc.name, {})
        st.update(fails=0, escalated_at=None)
        save_state(d, state)
    print(out)
    print(f"{args.cmd} {svc.name}: {'ok' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
