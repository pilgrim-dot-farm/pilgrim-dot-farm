# Runbook: a model service is down and the scripted restart failed

You were started by the fleet watchdog (`pilgrim/tools/fleetctl.py`) on the
station's Mac. One model service is down. The watchdog already tried
`fleetctl restart <service>` and it did not come back. Your job is to find out
why, fix it if the fix is on the allowed list below, and write a report.

You are in the repo root. Run the tool as:

    .venv/bin/python -m pilgrim.tools.fleetctl status
    .venv/bin/python -m pilgrim.tools.fleetctl restart <service>

The registry `pilgrim/deploy/model-fleet/fleet.yaml` has each service's ssh
host, directory, run command, ports and cold-start budget. Reach a box with
`ssh <host>`; for `yuri` use
`ssh -o HostName=192.168.68.89 -o HostKeyAlias=yuri.local yuri`.
`local` means this Mac.

## Rules

1. Work on the one service named in your prompt. Do not touch any other
   service, session, or box.
2. Models run only in the tmux session `pg-<service>`, and only `fleetctl`
   starts them. Never start a model with `nohup`, `&`, or a session of
   another name.
3. Never reboot a box. Never use `sudo` except `sudo fuser -k <port>/tcp` on a
   port listed for your service in the registry.
4. Never edit a run script on a box, never delete model files, never touch
   `/models`.
5. Never kill a process you cannot name. Sessions that do not start with `pg-`
   belong to the operator: leave them alone.
6. If DeepSeek is running (`ssh spx1 'ss -ltn | grep :8888'` prints a line),
   the Sparks belong to it. Stop and report; do not fight it.
7. At most three restart attempts. Then stop and report.

## Steps

1. **State.** `fleetctl status`. If your service is `up`, it recovered:
   report and stop.
2. **Why did it die?** On the host, read the end of the service log:
   `tail -60 ~/pg-fleet-logs/<service>.log`. Then `free -h`, and on a GPU box
   `nvidia-smi`. Look for: out of memory, "address already in use", a missing
   file, a CUDA error.
3. **Port held.** `ss -ltnp | grep :<port>` (macOS: `lsof -nP -iTCP:<port>
   -sTCP:LISTEN`). If a process you can name as this service still holds the
   port, `kill <pid>`, wait 10 s, `kill -9 <pid>` if it is still there.
4. **Memory parked (spx0, spx1 only).** `free -h` shows tens of GiB used and
   no model process is running on that box: `ssh <host> 'bash -lc
   "_reset_driver"'`, wait 30 s, check `free -h` again. Do not run this while
   any model on that box is serving.
5. **Restart.** `fleetctl restart <service>`. It waits for `/health` up to the
   budget. Then `fleetctl status`.
6. **Not fixed after three attempts, or the cause is not on this list**
   (driver errors from `nvidia-smi` itself, a full disk, a missing binary or
   model file, the host unreachable): stop. Do not improvise.

## Report

End with a short report: the service, what you found (quote the log lines),
every command that changed something, the final `fleetctl status` line for the
service, and — if it is still down — what a human needs to do.
