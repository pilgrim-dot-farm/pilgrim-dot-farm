# SFX — bug report: backend down, no retry for failed cues, weak at new SFX

> **Backlog.** Filed by the operator 2026-11-10 (transcribed by the docs
> agent). Not scheduled. Related design doc: [cgm.md](cgm.md) (2026-10-02,
> same subsystem); subsystem spec + findings: `docs/systems/sfx.md`;
> backend status: `docs/systems/model-fleet.md` §4.1.

## Operator's words (2026-11-10)

> SFX is not mission critical. They save the sfx, and quite frankly are not
> good at generating new ones. File that as a bug report somewhere.

Read: the pipeline **saves** what it renders (fixed palette, cues rendered
once and recycled — the CGM policy), and the **generator** (MOSS-SoundEffect
v2.0) is weak at producing new, usable SFX.

## Severity: low

Not mission critical by operator call. Playout is unaffected by design
(AGENTS hard rule 1: missing/expired stingers are a no-op, never a stall;
hosts air dry). Nothing here can take the station off air.

## The defects (with evidence, verified 2026-11-10)

### D1 — The backend is down, and has been for weeks

The MOSS-SoundEffect v2.0 server (spx0, `:8000`) has been down at least
since 2026-10-02: the Spark's GB10 unified memory is shared with the
DeepSeek 2x-Spark job, and MOSS unloads/OOMs while DeepSeek runs (cgm.md
§1.1: "expected, not a fault"). Confirmed dead 2026-11-10: the `sfx` tmux
session holds the failed launch (`CUDA_ERROR_OUT_OF_MEMORY` from
`cuDevicePrimaryCtxRetain`), `:8000` refuses connections from the M5, and no
supervisor restarted it (tmux, not systemd).

Consequences: **no new SFX can render at all**; the CGM C1 lazy stock fill
(the five animals with zero stock: horse, pig, duck, turkey, donkey) never
happened; the palette is frozen at whatever rendered before the outage
(air-approved keepers: chicken, cow, horse, pig contextual +
`slide_whistle` evergreen; props `sprinkler`/`tractor`/`hay_bale`/`barn_door`
evergreen with fixed seeds; `wind_bed` and `rimshot` wired but silent,
`sfx.md` §10).

### D2 — Failed cues are never retried (code bug, still present 2026-11-10)

`producer.sfx_step` pops `meta.sfx_pending` **unconditionally** after
`_attach_sfx` (`pilgrim/producer.py` ~line 941); there is no `sfx_attempts`
counter anywhere in the tree. A render that failed during a D1 outage is
dropped forever — the item stays dry for its whole life. This is exactly the
cgm.md §1.2 bug; its fix (C3: retry with cap + `/health` gate) is unimplemented.
Live stock-pool state is in RDS Postgres (`DATABASE_URL`; `station.db` is an
empty stub), so the pre-outage pool audit in cgm.md §1.4 (20 near-identical
animal takes) is the last recorded census.

### D3 — The generator is weak at new SFX (model limitation)

MOSS-SoundEffect v2.0 is bad at producing new, on-brand hits: "prompt
lottery is real" — only ~5 of the first 12 probed prompts gave a usable
result, and the re-rolled candidates (airhorn, rimshot, record scratch, sad
trombone, boing, applause) were "way off" (`docs/systems/sfx.md` §6). The
full 27-cue palette shipped with every cue `approved: true` but **unheard**
(config comment, 2026-10-01). So: extending the palette is low-confidence,
and the curation loop (render → human listens → approve/re-roll) is the only
quality gate, and it is a human bottleneck. This is a limitation of the
model, not a code bug — the mitigations are curation discipline (fixed seeds,
re-rolls) or accepting a thin palette.

## Impact

- On-air: hosts air dry or with the thin approved palette; stingers are
  optional by construction. No gap risk, no off-air risk.
- Cost: the box (spx0) stays half-useless while DeepSeek runs, and the SFX
  loop logs `sfx.failed` retries during outages (cgm.md §1.1 "retry storm"
  wart — the health-gate half of C3 would also fix that).

## Disposition options (operator's call)

1. **Leave it** — accept SFX-less / thin-palette operation. Zero action;
   this bug report is the record.
2. **Run the CGM plan (C1–C3)** — needs the backend up first (one-liner:
   `ssh spx0; cd ~/local_llm/MOSS-sfx; ./run_server.sh`, tmux recommended)
   and a box free of DeepSeek (both Sparks are clean as of 2026-11-10).
   Fixes D1's consequence (stock fill) and D2 (retry + health gate);
   mitigates D3 via curation. Plus the systemd unit draft
   (`pilgrim/deploy/model-fleet/units/` — no `moss-sfx` unit exists yet;
   add one) fixes the "nothing restarts it" half of D1.
3. **Retire the subsystem** — `sfx.enabled: false` in `config.yaml` + a note
   in RADIO.md §5.2/§12 (the SFX policy is normative there); stop rendering
   and stop the retry logging. The client sidecar path stays (harmless no-op).

## Related open items (not part of this bug)

- Stinger gain (`stinger_gain: 0.5`) and host duck are TBD-by-ear knobs
  (`sfx.md` §10.3).
- Field reporter's wind bed + rimshot are wired but silent until approved
  (`sfx.md` §10).
