# The model fleet — hosts, launchers, ops

Status: reference, surveyed 2026-11-10 (read-only probes + the sanctioned
ninfer vet run on spx1). No station code touched. Companion docs:
`docs/backends.md` (request/response shapes — owned by the chat agent, probe
first) and `MONITORING.md` (the observability plan; the `pg_` prefix rule
lives in its §2).

The station reaches its models through two kinds of doors: LiteLLM (twr) for
LLMs and the music pool, and direct HTTP for Kokoro, SFX and ninfer. This
page is the system description of what is behind those doors — which box
runs what, how each service is launched, and how to keep a box healthy when
the GPU memory is stuck.

## 1. Fleet at a glance (verified 2026-11-10)

| Box | SSH | LAN IP | Hardware | Serving today | Launcher | Ports | Supervisor |
|---|---|---|---|---|---|---|---|
| `twr` | `twr` | 192.168.68.75 | RTX 5090 32 GB, 124 GB RAM | qwen38 (Qwen 3.8 27B NVFP4, ninfer-serve); LiteLLM (docker :4000 → `litellm.twr.church`) | `~/local_llm/ninfer/run.sh` → `build/rtx5090-release/apps/ninfer-serve` | 8081, 4000 | tmux `ninfer` (systemd draft) |
| `spx0` | `spx0` | 192.168.68.88 | DGX Spark GB10, 121 GiB unified | **none** — SFX is down (§4.1), was DeepSeek rank 1 | `~/local_llm/audio.cpp/run.sh`; `~/local_llm/MOSS-sfx/run_server.sh`; ninfer built (no run.sh) | 8000 (SFX), 8080+8321 (music) | tmux `sfx` (holds the dead server) |
| `spx1` | `spx1` | 192.168.68.56 | DGX Spark GB10, 121 GiB unified | **none** — idle, clean (DeepSeek stopped 2026-11-10) | `~/local_llm/audio.cpp/run.sh`; `~/local_llm/ninfer/run.sh` (tentative, **vet passed** §4.4) | 8080+8321 (music) | tmux; a stale ssh forward holds :8000 |
| `yuri` | via `twr` (hard to reach directly) | 192.168.68.89 | RTX 4070 Ti 12 GB, 62 GB RAM | ornith (Ornith-1.5-9B, llama.cpp); Kokoro TTS | `~/local_llm/llama.cpp/ornith15_run.sh`; `~/local_llm/kokoro/server/run.sh` | 8080, 8001 | tmux `llm`, `kokoro` |

Health at survey: qwen38 `{"status":"ok"}`, `/v1/models` → `qwen38`
(max_model_len 131072, owned_by `ninfer`); ornith `{"status":"ok"}`; kokoro
`{"status":"ok"}` (cuda, 55 voices, default `af_heart`, ~10 days uptime).
SFX: **down** (§4.1).

Note: `MONITORING.md` §1.1 lists spx1 as `192.168.68.90` — that was the
survey-time value; spx1 is on `.56` today. LAN IPs change; the ds41 recipe
is unaffected (it uses the hardwired CX7 link, §3).

## 2. Per-box notes

**twr** — `ninfer-serve` (RTX 5090 build) serves the Qwen3.8-27B NVFP4
artifact at `/tank/huggingface/Qwen3.8-27B-nvfp4-NInfer` with:
`--host 0.0.0.0 --port 8081 --model-id qwen38 --max-context 131072
--kv-dtype int8 --max-concurrency 3 --vision --preserve-thinking
--chat-template chat_template.jinja --pending-timeout 600000
--spec dflash2 --draft-tokens 7 --proposal-head optimized`. This is the box
the station's `qwen38` model resolves to through LiteLLM (news, song briefs,
DJ talk). `run.sh` `start` execs the server in the foreground; its pid-file
stop/status bookkeeping is unreliable — treat tmux (or the draft systemd
unit) as the source of truth.

**yuri** — ornith is a foreground `llama-server`:
`-m /ssd/models/Ornith-1.5-9B-GGUF/Ornith-1.5-9B-Q5_K_M.gguf -c 131072
--cache-type-k q8_0 --cache-type-v q8_0 --no-mmap -ngl 99` (commercials,
liners). Kokoro is a venv `tts_server.py` with weights
`/ssd/models/Kokoro-82M` on :8001 (16-bit mono 24 kHz WAV). Both in tmux
(`llm`, `kokoro`). yuri also runs ollama (system service, default model on
127.0.0.1) — not station-facing.

**spx0 / spx1** — GB10 unified memory: the GPU and CPU share one 121 GiB
pool, and `nvidia-smi` reports Spark memory as "Not Supported". Host memory
(`free -h`) is the signal that the GPU is stuck (§4.3). Both boxes carry the
ninfer `dgx-spark-release` build (`ninfer`, `ninfer-serve`,
`ninfer-perplexity` under `build/dgx-spark-release/apps/`) and the
audio.cpp MiniMax Music 3 tree.

## 3. Shared infrastructure

- **`/models`** — NFSv4, 1.8 TB, ~98% used at survey. The Sparks' shared
  model store: holds `Qwen3.8-Flash-Next-NInfer` (32 GB `.ninfer` + three
  PLE part files, ~108.5 GB total, plus `conversion.json`/`manifest.json`).
  ninfer reads from it at ~3.2 GiB/s (§4.4).
- **CX7 direct link** (per the ds41 recipe): spx1 `192.168.100.11/31` ↔
  spx0 `192.168.100.10/31` (+ `192.168.103.x` twins, MTU 9000, RoCE).
  DeepSeek tensor-parallel uses it; ninfer runs single-rank (one Spark).
- **GPU clocks** locked at 2200 MHz (`sudo nvidia-smi -lgc 0,2200` on both
  Sparks). The lock is lost after every reboot — `spark-gpu-clocks.service`
  draft re-applies it at boot.
- **LiteLLM as the naming layer.** The station's model names (`qwen38`,
  `ornith`, the MiniMax pool, …) resolve to the boxes above through LiteLLM
  config on twr; the vLLM pass-through (`/vllm`) routes the music pool to
  the audiocpp hot servers. This mapping was inferred from station config +
  process observation, not from the LiteLLM config itself.

## 4. Ops runbook

### 4.1 SFX is down (as of 2026-11-10)

MOSS-SoundEffect v2.0 on spx0 (`~/local_llm/MOSS-sfx/run_server.sh`, :8000,
model `~/local_llm/huggingface/MOSS-SoundEffect-v2.0`, ~1.3B DiT + Qwen3
text encoder, cold start 1–4 min) died with
`CUDA_ERROR_OUT_OF_MEMORY` (`cuDevicePrimaryCtxRetain`) when the DeepSeek
2x-Spark job took the box's unified memory, and was never restarted. The
station has been running SFX-less since at least the 2026-11-07 survey
(`:8000` was already not listening then); a failing backend degrades
inventory, it never crashes the station. Both Sparks are clean again, so a
restart should work: `ssh spx0; cd ~/local_llm/MOSS-sfx; ./run_server.sh`
(tmux recommended). Endpoint: `POST /audio/speech` (OpenAI-compatible).

### 4.2 DeepSeek 4.1 Flash Next (2x Spark) — manual by decision

Head on spx1: rank 0 + API :8888 (Bearer key at
`~/.local/state/dsv41-tensorfold/api-key`), docker container
`dsv41-1017-exl3-tf`; worker on spx0: rank 1 over the CX7 link. Mia-AiLab
EXL3 weights, TensorFold-native. `start.sh`/`stop.sh` in
`~/local_llm/ds41/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks/` manage both ranks
(stop saves logs to `~/.cache/dsv41-tensorfold/logs/`). The recipe ships
`systemd/dsv41-tensorfold.service` (its `FOREIGN=1` hook), but per operator
decision DeepSeek stays manual — it is the only fleet service excluded from
the systemd requirement. **Stopped 2026-11-10** (to vet ninfer on spx1);
logs `20261109-195145-rank{0,1}.log.gz` are kept; restore:
`ssh spx1; cd ~/local_llm/ds41/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks; ./start.sh`.
The 2x-Spark job and single-Spark models (MiniMax, qwen flash) do not
coexist; that trade-off is the operator's.

### 4.3 `_reset_driver` (Sparks)

Defined in `/etc/profile.d/reset_nvidia.sh` on spx0 and spx1. It is a
function, so it needs a login bash shell — a bare
`ssh spx1 _reset_driver` won't see it; use `ssh spx1 'bash -lc
"_reset_driver"'`. What it does: stop `nvidia-persistenced` →
`fuser -k /dev/nvidia*` → `rmmod/modprobe nvidia_uvm` → restart
`nvidia-persistenced` → `drop_caches`. When to run it: `free -h` shows tens
of GiB "used" with no resident model process (parked pages after a model
process dies; they sometimes drain on their own in 1–2 min, the reset forces
it). Verified 2026-11-10: after the DeepSeek stop, both Sparks went from
~117 GiB used to ~5 GiB used.

### 4.4 ninfer vet — Qwen3.8-Flash-Next on spx1 (2026-11-10): PASS

Tentative `~/local_llm/ninfer/run.sh` on spx1: one-shot `ninfer` CLI
against `/models/Qwen3.8-Flash-Next-NInfer/qwen3_8_flash_next.ninfer` with
`--kv-dtype fp8 --spec mtp --draft-tokens 3 --proposal-head optimized
--max-context 32768 --max-tokens 1024`. Result: **EXIT_CODE=0**; 73.0 GiB
weights loaded from `/models` at 3.18 GiB/s (22.9 s); engine ready in 30.9 s;
110.6 GiB of the 116 GiB device budget; prefill 320.6 tok/s, decode
45.1 tok/s; MTP acceptance 52.1% (2.56 tok/round). The vet used the CLI;
the serving form is the same build's `ninfer-serve` — a "qwen 3.8 Flash
alongside MiniMax" deployment needs a `run-serve.sh` wrapping it (draft
unit: `pilgrim/deploy/model-fleet/units/ninfer-qwen-flash.service`). The
build exists on both Sparks; spx0 has no `run.sh` yet.

### 4.5 MiniMax Music 3 (audio.cpp)

`run.sh` (same on both Sparks): hot server :8080 (config
`music3_webui/server_config.json`) + web UI :8321; idempotent (port guard);
runs the server in the foreground then `wait`, traps INT/TERM —
systemd-ready (`audiocpp-music3.service` draft). Requires
`build/bin/audiocpp_server` (`./build.sh`, CMake Release). Not running at
survey (the boxes were hosting DeepSeek). Model loads at startup (~30–60 s).

### 4.6 tmux is the de facto supervisor

Session names: `ninfer` (twr), `llm` + `kokoro` (yuri), `sfx` (spx0, holds
the dead SFX server), plus long-lived numbered sessions on the Sparks.
Nothing fleet-side is systemd-managed yet.

## 5. systemd requirement (operator, 2026-11-10)

Every fleet service must start on boot and restart on failure via systemd,
**except DeepSeek** (§4.2). Drafts live in
`pilgrim/deploy/model-fleet/units/` — **not installed**, and no manual
rollover has been performed; **if a rollover is ever needed, practice on
ornith first** (least critical station-facing service). The README in that
directory has the install/rollover procedure, including why you must stop
the tmux-hosted instance before enabling a unit (the `run.sh` port guards
make the unit exit 0 while the old instance still serves).

## 6. Open items

1. Restart SFX on spx0 (§4.1) — one command, needs a go-ahead.
2. Restore DeepSeek vs. keep the Sparks free for MiniMax + qwen flash (§4.2)
   — operator's call; they don't coexist.
3. Write `run-serve.sh` on the Sparks and install the draft units (practice:
   ornith).
4. Stale ssh forward holding spx1 :8000 (owned by an sshd child, pid 2886)
   — unknown owner, clean up on next login.
5. `MONITORING.md` §1.1 host table: spx1 `.90` → `.56` (LAN IP changed).
