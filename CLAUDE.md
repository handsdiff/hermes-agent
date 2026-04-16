# Hermes Agent (handsdiff fork)

This is the `handsdiff/hermes-agent` fork of `NousResearch/hermes-agent`.

## Remotes

- `origin` — `handsdiff/hermes-agent` (fork)
- `upstream` — `NousResearch/hermes-agent`

## Open PRs to upstream

8 open PRs, all targeting `NousResearch/hermes-agent` main:

| PR | Branch | What | Depends on |
|----|--------|------|------------|
| #5957 | `hub-adapter` | Hub messaging platform adapter | — |
| #5688 | `feat/multi-memory-provider` | Multiple simultaneous memory providers | — |
| #7232 | `fix/lock-sethome-after-first-use` | Lock /sethome after home channel is set | — |
| #7297 | `feat/per-platform-model` | Per-platform model overrides via config.yaml | — |
| #9287 | `feat/cron-memory-peers` | Per-job Honcho peers for cron (+ revert #6995 guard) | — |
| #9308 | `feat/user-unify` | Unify owner identity across channels in Honcho memory | #9287 |
| #9829 | `fix/bg-skill-notify` | Notify main agent when background review creates skills | — |
| #9911 | `fix/session-list-sort` | Sort session listing by last activity, not creation time | — |

Merged:
- #6851 (telegram custom base_url). The `telegram-base-url-upstream` branch can be deleted as cleanup.
- #9924 (pty-job-control-hang) — cherry-picked via upstream #10584 with authorship preserved. The `fix/pty-job-control-hang` branch can be deleted as cleanup.

### Stacked branches

`feat/user-unify` is branched off `feat/cron-memory-peers` (not upstream/main) because
it depends on peer-plan's Change 0 (unconditional `user_id` override). When rebasing,
rebase `feat/cron-memory-peers` first, then rebase `feat/user-unify` onto it.

## Fork main structure

Fork `main` = upstream `main` + all open-PR branches + the `fork-only` branch merged together.
This is intentional: `hermes-provisioner/provision.py` clones this fork and depends on
all branches being present. Do not remove any branch from fork main until its PR lands upstream.

### Fork-only branch

The `fork-only` branch carries changes that apply only to the fork and are not intended
as upstream PRs (e.g., deleting irrelevant skills to reduce system prompt size). It is
rebased onto `upstream/main` and included in the octopus merge just like PR branches.

## Rebase workflow

When upstream `main` moves:

1. `git fetch upstream`
2. Rebase each feature branch individually onto `upstream/main` (independent branches):
   ```
   git checkout hub-adapter && git rebase upstream/main
   git checkout feat/multi-memory-provider && git rebase upstream/main
   git checkout fix/lock-sethome-after-first-use && git rebase upstream/main
   git checkout feat/per-platform-model && git rebase upstream/main
   git checkout feat/cron-memory-peers && git rebase upstream/main
   git checkout fix/bg-skill-notify && git rebase upstream/main
   git checkout fix/session-list-sort && git rebase upstream/main
   git checkout fork-only && git rebase upstream/main
   ```
3. Rebase stacked branches onto their parent (not upstream/main):
   ```
   git checkout feat/user-unify && git rebase feat/cron-memory-peers
   ```
4. Force-push each branch to origin.
5. Rebuild fork main: reset to `upstream/main`, then merge all branches:
   ```
   git checkout main
   git reset --hard upstream/main
   git merge hub-adapter feat/multi-memory-provider fix/lock-sethome-after-first-use feat/per-platform-model feat/cron-memory-peers feat/user-unify fix/bg-skill-notify fix/session-list-sort fork-only
   ```
6. Force-push fork main.

## Post-rebase pitfalls

Upstream refactors can silently break fork branches. After every rebase:

- **Check for deleted symbols.** Example: upstream deleted `MemoryManager.provider_names`
  which `feat/multi-memory-provider` depends on — silently disabled memory at init.
- **Check for refactored call sites.** Example: upstream consolidated 3 model-resolution
  call sites into `_resolve_session_agent_runtime` — #7297 had to be redesigned to push
  platform-awareness into that central helper instead of patching each call site.
- **Check for upstream memory plugin changes.** #9287 reverted upstream #6995's
  `not cfg.peer_name` guard. If upstream re-adds a similar guard, #9287 and #9308 both
  break silently — cron peers and owner unification stop working for provisioned agents.
- **Run the full test suite** after rebasing. All branches should be green before force-pushing.

## Git hygiene

- Each PR lives on its own branch. Don't mix work across branches.
- Commit to the feature branch, push, then rebuild fork main on top.
- Fork-only changes (not targeting upstream) go on the `fork-only` branch.
- Never commit directly to fork main — it's a derived merge, not a development branch.

## PR-specific notes

**#9308 (user-unify):** Stacked on #9287. Adds `_is_owner_source` gateway helper that
detects the provisioned owner via DM home channel match. Owner sources get `user_id=None`
so the configured `peerName: "user"` survives; strangers get transport-level peers.
Dual-reads legacy transport-level peer representations at init so pre-unification
history isn't orphaned. Sets `chat_type="synthetic"` on the background process
notification's synthetic SessionSource to prevent false-positive owner detection.

**#9287 (cron-memory-peers):** Gives cron jobs memory via per-job Honcho peers
(`cron-{name}`). Reverts #6995's `not cfg.peer_name` guard that blocked all `user_id`
overrides on provisioned agents. Adds `skip_memory=True` to hygiene compress agent.
Adds `shutdown_memory_provider()` to cron teardown.

**#7297 (per-platform model):** Redesigned during rebase to align with upstream's
`_resolve_session_agent_runtime` refactor. Single integration point — every call site
gets platform overrides for free. If reviewers ask, this version matches their refactoring direction.

**#5688 (multi-memory-provider):** Re-added `MemoryManager.provider_names` property
after upstream dead-code sweep deleted it. Branch depends on it in `run_agent.py`.

**#7232 (sethome lock):** Has tests in `tests/gateway/test_sethome_lock.py` (4 tests).

**#5957 (hub-adapter):** Fixed pre-existing test bug where `tests/gateway/test_hub.py`
patched the old `websockets.client.connect` symbol after upstream switched to `websockets.connect`.

**#9829 (bg-skill-notify):** Adds `_pending_bg_notifications` queue to `AIAgent`,
populated from the background review thread when skills are created. Drained at the
start of `run_conversation()` as `[System: ...]` messages. Also invalidates
`_cached_system_prompt` and the DB-stored prompt. If upstream refactors
`_spawn_background_review` or the scan loop at ~line 2233, this branch will conflict.

## Fork-only changes

**Skill pruning (fork-only branch):** Deleted skills irrelevant to provisioned agents
(distribution/comms for solo devs and startups). Reduces system prompt size, inference
cost, and LLM confusion. Deleted categories: apple, data-science, gaming, leisure,
mlops, red-teaming, smart-home, social-media, plus empty category stubs (diagramming,
domain, feeds, gifs, inference-sh). Partial deletions: creative (kept ideation,
excalidraw, p5js, popular-web-designs), media (kept gif-search, youtube-content),
productivity (kept nano-pdf, ocr-and-documents). 35 skills remain from original ~78.
