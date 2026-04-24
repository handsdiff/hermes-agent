# wait4test Honcho Actor Runtime Canary

Goal: make Honcho memory multiplayer-safe by treating Hermes runtime identity as the source of truth for who is speaking, while framing retrieved memory as the agent's private recalled context rather than external instructions.

## Scope

- Actor-resolved Honcho peer ids from `SessionSource` plus authority.
- Group sessions keep one transcript/session while memory writes are attributed to the actual speaker peer.
- Cached `AIAgent` instances receive fresh actor context each gateway turn.
- Honcho retrieval targets the current speaker from the agent peer's perspective.
- Memory context is injected only into the current user-role API message inside `<private_memory>`, never persisted.
- Runtime flag: `actorRuntime` or `HERMES_HONCHO_ACTOR_RUNTIME`.

## Evaluation Checklist

- [x] Alice/Bob in one group resolve to distinct human peers.
- [x] Owner resolves to the configured owner peer.
- [x] Hub/cron-like traffic resolves to non-human peers.
- [x] Honcho writes preserve explicit speaker peer ids.
- [x] Cached agents can clear stale actor context.
- [x] Retrieved memory is private current-turn user context, not system prompt context.
- [x] Full focused pytest pass.
- [x] wait4test canary deploy.
- [x] wait4test live verification against Honcho config/logs.

## Canary Findings

- Focused local suite: `330 passed, 3 warnings`.
- Focused wait4test suite: `330 passed, 1 warning`.
- wait4test needed the `honcho` VM tag for the internal Honcho endpoint; without it the SDK initialized but API calls returned HTTP 403.
- `actorRuntime` must be read from the active host block, not only from root config.
- Actor runtime must skip local memory-file migration. Uploading existing private files into the current speaker peer during a public/group session is a privacy leak.
- Final live Honcho eval wrote Alice, Bob, and assistant messages to one shared session while preserving peer ids:
  - `human_discord_alice-eval`
  - `human_discord_bob-eval`
  - `agent_wait4test`
- Fresh Hub gateway canary delivered to wait4test and created Honcho session `agent-main-hub-dm-hub-codex-honcho-eval-a` with peer-attributed messages:
  - `hub_agent_codex-honcho-eval-a`: canary inbound
  - `agent_wait4test`: `NO_REPLY`
- The persisted gateway transcript for the Hub canary contains the normal user turn and assistant `NO_REPLY`; private memory context was not persisted into the session transcript.

## Remaining Caveat

wait4test was processing older Hub backlog during the canary and hit unrelated provider/service pressure: several Honcho dialectic queries timed out at 60s, Discord reconnects timed out before recovering, and model calls saw transient 429s. The actor-attributed Honcho write path still passed.

## Hardening Pass After Independent Review

Two independent reviews found three material blockers before this can credibly support multiplayer relationship mapping:

- Mutable actor context could race across turns and write Alice's turn under Bob's peer.
- Retrieval caches were session-scoped, so Bob could receive Alice-shaped private memory in a shared group session.
- Honcho tools allowed explicit peer targeting without hard authorization.

Fixes shipped in the hardening pass:

- Memory operations now accept a per-turn `actor_context` snapshot and thread it through prefetch, queued prefetch, sync, memory writes, and Honcho tools.
- Honcho sync captures the actor peer before the background writer starts.
- Honcho base-context and dialectic-result caches are actor-scoped under actor runtime.
- Honcho manager context prefetch cache is keyed by session plus actor plus assistant peer.
- Non-owner/non-trusted Honcho tool calls can only target the current speaker alias, with read-only access to `ai`; explicit peer targeting now requires owner or trusted authority.
- Actor identity now prefers `user_id_alt` where platforms provide a more stable identity than display/user id.

Adversarial local coverage now includes:

- Alice/Bob shared-session prefetch isolation.
- Delayed async sync after actor switch.
- Non-owner explicit peer access denial.
- Owner explicit peer access.
- Stable alternate user-id actor mapping.

Focused local hardening suite: `335 passed, 3 warnings`.
