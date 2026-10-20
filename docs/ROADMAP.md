# Great Sage — Evolution Roadmap

> **Wise One — state your query.** Formerly Great Sage (kept as lore).
> Stage 1 of the evolution is **Great Sage**: logic without ego —
> bounded tools, validated config, honest degradation, permissioned
> everything. It computes; it does not want.
>
> Promotion to **Raphael** follows the gates in `docs/EVOLUTION.md`.
> Status: substrate complete, all checks green; roadmap phases below
> build on it, never rewrite it.

## Laws (carried over, renamed)

1. Substrate is frozen — extend, never re-architect.
2. Zero-cost + local-first hold (`docs/DEPENDENCY_POLICY.md` — written
   justification per dependency; stdlib preferred).
3. Every new tool goes through `ToolService.execute` (no bypass); every new
   loop carries bounds, timeouts, audit.
4. Stubs stay honest until replaced: mock → real swaps flip the health
   detail string and the docs in the same commit.
5. **GitHub is read-only by default.** Study freely; downloading anything
   needs the operator's explicit permission, every time.
6. **Game automation needs the operator's ToS judgment.** Botting violates
   most games' terms. The `gui.*` tools are hands, not a player;
   unattended game loops ship only with explicit per-game approval and
   stay ASK-gated. (CoC approval granted 2026-09-10.)

## Great Sage era — shipped

- **Voice given** (Phase A): Vosk STT + Piper TTS + fuzzy wake-word
  (`greatsage voice health|listen|speak`); mocks remain the default,
  real engines opt-in via `config/sage.yaml`.
- **Memory deepened** (Phase B): Ollama `nomic-embed-text` provider,
  `memory search --semantic`, `memory reindex`, `memory digest`.
- **Chores begun** (Phase C, early): `greatsage/scheduler/`
  (`schedule add|list|remove|tick|health`), `network.fetch`,
  `homeassistant.states|call`, corrupt-DB quarantine.
- **Reach** (Phase F, early): Telegram bridge
  (`telegram health|listen`, `/briefing` `/status` `/screenshot`).
- **Hands** (Phase F, early): `gui.screenshot|click|type` (ctypes,
  Windows-only, always ASK-gated, no silent auto-play).

## Next: the Raphael gates (`docs/EVOLUTION.md`)

1. **Eyes** — local VLM grounding: screenshot → coordinates, verified
   live (the CoC loop plays a full session on vision alone).
   ✅ demonstrated live 2026-10-17 (real GDI capture → local `moondream`
   grounding through the bounded vision pipeline, coordinates extracted;
   the full CoC-session criterion stays open until the game loop runs).
2. **Always-on voice** — mic loop with wake-word + barge-in.
   ◐ built 2026-10-17 (`chat --always-on`: wake-gated loop with barge-in,
   `greatsage/voice/always_on.py`, offline-verified state machine/bounds;
   the operator's spoken mic test remains).
3. **Wired brain** — a real LLM drives the bounded agent loop end-to-end.
   ✅ demonstrated live 2026-10-16 (`agent run`, local Ollama,
   `llama3.2:1b` and the configured default `llama3.1:8b` both
   tool-calling through the full security pipeline).
4. **Recall gating** — memory decides for itself what to surface.
5. **30-day stability** — nightly green, zero silent fallbacks.

## Progress log

| Date | Increment | Status |
|---|---|---|
| 2026-09-05 | Roadmap written; `memory digest` | Committed, pushed (Sage era) |
| 2026-09-06 | Legacy models housed; `briefing` | Committed, pushed (Sage era) |
| 2026-09-08/09 | Scheduler, tools, Telegram, embeddings, real voice, quarantine | Committed, pushed (Sage era) |
| 2026-09-12/13 | **Rebrand P1–P4**: Great Sage surface, `greatsage/` package, GREATSAGE env/root/names, great-sage dist | This evolution |
| 2026-10-16 | OpenCode **v2 API** provider rewrite (Basic auth, prompt-then-poll, poll-based delegation); voice STT live (vosk); P0/P1 hardening (poll pacing, memory refs in agent prompt, voice/vision tools registered, env int_list, honest provider states, dead-code sweep); docs reconciliation; **gate 3 wired brain demonstrated** (`agent run` → `system.info` → answer, local Ollama) | This evolution |
| 2026-10-16 | **Eyes capture half**: real primary-monitor capture (GDI via shared `capture_primary_monitor_bmp`) behind the vision backend ABC with nearest-neighbor scaling, `GREATSAGE_VISION_BACKEND` selection, honest `backend=gdi` health; verified live (frame ≠ stub, pipeline 57ms) | This evolution |
| 2026-10-17 | **Eyes grounding half**: `OllamaGrounding` (local `moondream`) — stdlib BMP→PNG, bounded strict-JSON prompt (`num_predict` cap), tolerant region parsing (flat/box/array/truncated shapes, out-of-range dropped, never fabricated), `GREATSAGE_VISION_GROUNDING` selection; verified live: GDI capture → moondream → real coordinates through the tool pipeline (141ms capture, 7.5s grounding) | This evolution |
| 2026-10-17 | **Always-on voice loop built** (`greatsage/voice/always_on.py` + `chat --always-on`): wake-gated WAKE⇄CONVERSING state machine, one-breath wake+command, idle return, barge-in (polled detector stops playback), fully injectable + offline-tested; bug pass: semantic-filter parity, spec-fetch retry cap, honest loop outcomes | This evolution |
| 2026-10-17 | **Brain swap: Spark-X2.5-4B-Q8** (iFlytek/XHToken, Apache-2.0, native tools + 1M ctx) as the local model. Ollama upgraded 0.30.6→0.40.6 (`spark2_5` support); benchmark vs llama3.1:8b: agent task completed 12.7s vs 126s-failure, warm ~12 tok/s at 3.7 GB VRAM (llama spilled to CPU); chat runs `think=false` via request metadata (agent keeps deliberation); `config/sage.yaml` swapped | This evolution |
