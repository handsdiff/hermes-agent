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
- [ ] Full focused pytest pass.
- [ ] wait4test canary deploy.
- [ ] wait4test live verification against Honcho config/logs.

