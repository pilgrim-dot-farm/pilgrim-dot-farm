# Model fleet: registry, restart runbook, and the old systemd drafts

**Current (2026-11-14):** models run in tmux sessions `pg-<service>`, started
by `python -m pilgrim.tools.fleetctl` from `fleet.yaml` in this directory and
kept up by the launchd watchdog (`pilgrim/deploy/mac/fleet.sh`).
`RESTART.md` is the runbook the watchdog hands a pi agent when a scripted
restart fails. See `docs/systems/model-fleet.md` §0 and §6.0.

The `units/` drafts below were never installed and are **superseded**; the
rest of this file is their original description.

## systemd units — DRAFTS, NOT INSTALLED (superseded)

Operator requirement (2026-11-10): every fleet service starts on boot and
restarts on failure **via systemd**, except DeepSeek (stays manual).

**Status: draft.** Nothing in this directory has been installed on any host.
Per the operator, no manual service rollover has been performed; **if a
rollover is ever needed, practice on `ornith` first** (yuri, llama.cpp :8080 —
the least critical of the station-facing services).

The fleet's operational story (hosts, launcher scripts, `/models`,
`_reset_driver`, the 2026-11-10 ninfer vet) is in
`docs/systems/model-fleet.md`.

| Unit | Host | Replaces | Notes |
|---|---|---|---|
| `ninfer-qwen38.service` | `twr` | tmux `ninfer` | Qwen3.8-27B NVFP4 serve, :8081, model id `qwen38` |
| `audiocpp-music3.service` | `spx0`, `spx1` | tmux (none yet) | MiniMax Music 3 hot server :8080 + web UI :8321 |
| `ninfer-qwen-flash.service` | `spx0`, `spx1` | — | **BLOCKED**: needs a serve-form run script first (see unit header) |
| `ornith.service` | `yuri` | tmux `llm` | llama-server, Ornith-1.5-9B, :8080 — the rollover practice case |
| `kokoro.service` | `yuri` | tmux `kokoro` | Kokoro-82M TTS, :8001 |
| `spark-gpu-clocks.service` | `spx0`, `spx1` | manual step | `nvidia-smi -lgc 0,2200` after every reboot |

DeepSeek 4.1 (the 2x-Spark TensorFold job) is **deliberately excluded** by
operator decision. Its recipe ships its own
`systemd/dsv41-tensorfold.service` (the `FOREIGN=1` hook in `start.sh`), but
it stays manual: `start.sh` / `stop.sh` on the spx1 ds41 directory.

## Rollover procedure (when we actually do it)

1. Copy the unit to `/etc/systemd/system/` on the host.
2. `systemctl daemon-reload`.
3. **Stop the tmux-hosted instance first** (`tmux kill-session -t NAME`),
   then `systemctl enable --now NAME`. Order matters: `run.sh` guards on the
   port, and if the old instance is still serving, the new unit sees a
   healthy port, exits 0, and systemd leaves the unit inactive while the
   tmux session keeps serving — the exact confusion this directory exists
   to remove.
4. Verify: the service's `/health` endpoint, then watch
   `systemctl status` through one `kill -9` of the main process
   (`Restart=on-failure` must bring it back).
5. Keep the tmux session name as a record in `docs/systems/model-fleet.md`.

## What the units deliberately do NOT do

- **GPU memory cleanup.** A dead model process can leave GB10 unified memory
  parked (tens of GB "used" with no resident process). That needs the
  interactive `_reset_driver` (docs/systems/model-fleet.md §4); it is not
  automatable by these units and must stay a human/agent action with
  `free -h` as the detector.
- **Model selection.** The units exec the box run scripts as-is; model and
  flag changes happen in the run scripts (or, for the qwen-flash unit, in
  the still-to-be-written `run-serve.sh`), never by editing the unit.
