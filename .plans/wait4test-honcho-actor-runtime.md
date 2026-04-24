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

## Remaining Caveat

A fresh synthetic Hub injection was blocked by Hub registration/access controls from the control environment. The deployed wait4test gateway is running with Honcho initialized, and the final direct provider eval covers the critical actor-attributed memory path.
