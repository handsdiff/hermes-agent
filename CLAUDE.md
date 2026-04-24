# Hermes Agent (handsdiff fork)

This is the `handsdiff/hermes-agent` fork of `NousResearch/hermes-agent`.

## Remotes

- `origin` — `handsdiff/hermes-agent` (fork)
- `upstream` — `NousResearch/hermes-agent`

## Open PRs to upstream

All targeting `NousResearch/hermes-agent` main:

| PR | Branch | What | Depends on |
|----|--------|------|------------|
| #5957 | `hub-adapter` | Hub messaging platform adapter | — |
| #5688 | `feat/multi-memory-provider` | Multiple simultaneous memory providers | — |
| #7232 | `fix/lock-sethome-after-first-use` | Lock /sethome after home channel is set | — |
| #9287 | `feat/cron-memory-peers` | Per-job Honcho peers for cron (+ revert #6995 guard) | — |
| #9308 | `feat/user-unify` | Unify owner identity across channels in Honcho memory | #9287 |
| #9829 | `fix/bg-skill-notify` | Notify main agent when background review creates skills | — |
| #9911 | `fix/session-list-sort` | Sort session listing by last activity, not creation time | — |
| #11646 | `fix/mcp-initial-connect-retries` | Bump MCP initial connect retries 3→6 for slow warmups | — |
| #11647 | `fix/mcp-sse-transport` | Support SSE transport alongside Streamable HTTP | — |
| #12234 | `feat/model-routing` | Match-based model routing via `model.routes` (subsumes per-platform + per-source) | — |
| #14883 | `feat/routing-context-user-id` | Expose trusted-platform `user_id` in the `model.routes` routing context | #12234 |
| #14884 | `feat/agent-actor-runtime-stacked` | Actor/runtime state layer, `self_state`, owner authority, and outbound policy gates | #12234, #14883 |
| #12590 | `feat/epub-document-support` | Add `.epub` to `SUPPORTED_DOCUMENT_TYPES` allowlist (shared by telegram/slack/discord/feishu/whatsapp document handlers) | — |
| #12702 | `feat/html-document-support` | Add `.html` / `.htm` to the same allowlist | — |
| #12606 | `feat/rehydrate-compaction-summary` | Rehydrate `ContextCompressor._previous_summary` from transcript on agent boot so the UPDATE-prompt compaction chain survives process restarts and agent-cache eviction | — |
| #12686 | `fix/route-aware-background-agents` | Carry `model.routes` bundle through flush-memory and background-review spawn sites so post-turn auxiliary calls don't pair the routed `model` with the config-default `base_url` | — |

Merged:
- #6851 (telegram custom base_url). The `telegram-base-url-upstream` branch can be deleted as cleanup.
- #9924 (pty-job-control-hang) — cherry-picked via upstream #10584 with authorship preserved. The `fix/pty-job-control-hang` branch can be deleted as cleanup.
- #12207 (compound-background-subshell-leak) — cherry-picked via upstream #12724 with authorship preserved (upstream commit `abfc1847`). The `fix/compound-background-subshell-leak` branch can be deleted as cleanup.

Closed / superseded:
- #7297 (per-platform model overrides) — superseded by #12234. Branch `feat/per-platform-model` already deleted from origin.
- #12227 (per-source model selection, draft) — superseded by #12234. Branch `feat/per-source-model` already deleted from origin.
- #11617 (tool-call args valid JSON) — superseded by upstream commit `3128d9fc` ("fix(context_compressor): keep tool-call arguments JSON valid when shrinking"), which introduced `_truncate_tool_call_args_json` — strictly better than the branch's sentinel-object replacement because it preserves keys/structure. Branch `fix/compressor-tool-args-valid-json` deleted from origin.
- `feat/slate-dg-proxy-hook` (no PR) — abandoned prototype that built the Discord zero-secrets transport *inside* hermes-agent as an opt-in `SLATE_DG_PROXY=1` hook (adding `gateway/platforms/_slate_dg_patch.py` + a conditional import in `gateway/platforms/discord.py`). Superseded by the provisioner-side approach: `hermes-provisioner/dg_patch.py` installed into each VM's site-packages via a `.pth` file, zero hermes-agent patches required. The branch was also mis-cut off fork `main` instead of `upstream/main`, so it carries a 434-file / ~41k-line-deletion diff — unmergeable as an upstream PR. Fork main does not include it; no VM runs this code. Branch can be deleted from origin as cleanup.

### ⚠️ Always branch off `upstream/main`, never off fork `main`

Every feature branch must be cut from `upstream/main`. If you branch off fork `main`,
the branch inherits *every other fork branch's content* (because fork main is an
octopus merge of all of them). The upstream PR then shows a ~100k-line diff including
unrelated work and fork-only skill deletions — unreviewable and unmergeable. This has
happened three times already (MCP retries, MCP SSE, compressor fix were all originally
cut off fork main and had to be rebuilt from scratch off `upstream/main`).

```
# correct
git checkout -b my-feature upstream/main

# WRONG — will pollute the branch with every other feature
git checkout -b my-feature main
```

### Stacked branches

`feat/user-unify` is branched off `feat/cron-memory-peers` (not upstream/main) because
it depends on peer-plan's Change 0 (unconditional `user_id` override). When rebasing,
rebase `feat/cron-memory-peers` first, then rebase `feat/user-unify` onto it.

`feat/routing-context-user-id` is branched off `feat/model-routing` because it adds
trusted-platform `user_id` to the routing context produced for `model.routes`. Rebase
`feat/model-routing` first, then rebase `feat/routing-context-user-id` onto it.

`feat/agent-actor-runtime-stacked` is branched off `feat/routing-context-user-id`.
Rebase the routing stack first, then rebase `feat/agent-actor-runtime-stacked` onto
`feat/routing-context-user-id`.

## Fork main structure

Fork `main` = upstream `main` + all open-PR branches + the `fork-only` branch merged together.
This is intentional: `hermes-provisioner/provision.py` clones this fork and depends on
all branches being present. Do not remove any branch from fork main until its PR lands upstream.

Fork `main` also currently carries these non-upstream-PR branches because provisioned
agents depend on them:
- `feat/discord-shared-channel`
- `feat/mirror-autocreate-channel-session`
- `feat/send-message-dgproxy`

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
   git checkout feat/model-routing && git rebase upstream/main
   git checkout feat/cron-memory-peers && git rebase upstream/main
   git checkout fix/bg-skill-notify && git rebase upstream/main
   git checkout fix/session-list-sort && git rebase upstream/main
   git checkout fix/mcp-initial-connect-retries && git rebase upstream/main
   git checkout fix/mcp-sse-transport && git rebase upstream/main
   git checkout feat/discord-shared-channel && git rebase upstream/main
   git checkout feat/mirror-autocreate-channel-session && git rebase upstream/main
   git checkout feat/send-message-dgproxy && git rebase upstream/main
   # (fix/compressor-tool-args-valid-json removed — superseded by upstream 3128d9fc)
   # (fix/compound-background-subshell-leak removed — cherry-picked via upstream #12724 as abfc1847)
   git checkout feat/epub-document-support && git rebase upstream/main
   git checkout feat/html-document-support && git rebase upstream/main
   git checkout feat/rehydrate-compaction-summary && git rebase upstream/main
   git checkout fix/route-aware-background-agents && git rebase upstream/main
   git checkout fork-only && git rebase upstream/main
   ```
3. Rebase stacked branches onto their parent (not upstream/main):
   ```
   git checkout feat/user-unify && git rebase feat/cron-memory-peers
   git checkout feat/routing-context-user-id && git rebase feat/model-routing
   git checkout feat/agent-actor-runtime-stacked && git rebase feat/routing-context-user-id
   ```
4. Force-push each branch to origin.
5. Rebuild fork main: reset to `upstream/main`, then merge all branches. The
   octopus strategy (`git merge A B C ...` in one go) fails when any two
   branches add code to adjacent lines in the same function. Use sequential
   `git merge` instead — 3-way recursive is more forgiving:
   ```
   git checkout main
   git reset --hard upstream/main
   for b in hub-adapter feat/multi-memory-provider fix/lock-sethome-after-first-use \
            feat/cron-memory-peers feat/user-unify \
            fix/bg-skill-notify fix/session-list-sort \
            fix/mcp-initial-connect-retries fix/mcp-sse-transport \
            feat/model-routing \
            feat/routing-context-user-id \
            feat/discord-shared-channel \
            feat/mirror-autocreate-channel-session \
            feat/send-message-dgproxy \
            feat/epub-document-support \
            feat/html-document-support \
            feat/rehydrate-compaction-summary \
            fix/route-aware-background-agents \
            fork-only \
            feat/agent-actor-runtime-stacked; do
     git merge --no-edit "$b" || break
   done
   ```
   If a conflict fires, resolve by union (take both sides' additions — same
   function, non-overlapping edits), commit the merge, then continue the
   loop manually with the remaining branches. This happens most often at the
   `_classify_source_kind` / `_is_owner_source` adjacency in `gateway/run.py`
   when `feat/model-routing` meets `feat/user-unify`.
   Also expect a union conflict when `feat/agent-actor-runtime-stacked` meets
   fork main: keep both `self_state` and `integrations` in `toolsets.py`, and
   keep `infer_platform_authority(source) == "owner"` before the `_is_owner_source`
   fallback in `_classify_source_kind`.
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
Owner detection must normalize platform identity by value before checking untrusted
platforms; fork-main routing tests use source shims whose `platform` is not hashable.

**#9287 (cron-memory-peers):** Gives cron jobs memory via per-job Honcho peers
(`cron-{name}`). Reverts #6995's `not cfg.peer_name` guard that blocked all `user_id`
overrides on provisioned agents. Adds `skip_memory=True` to hygiene compress agent.
Adds `shutdown_memory_provider()` to cron teardown.

**#12234 (model-routing):** Supersedes closed #7297 (per-platform) and #12227 (per-source).
Adds `agent/smart_model_routing.apply_route(model, runtime, model_config, context)` —
a pure helper that iterates `model.routes` and applies the first entry whose `match`
predicates match the caller's context dict. Gateway classifies `owner / hub_peer /
stranger` via `_classify_source_kind` (delegates to `_is_owner_source` for the owner
check on fork main); cron and CLI hardcode their own context. Legacy
`model.platforms.<name>` and `model.by_source.<kind>` configs are auto-synthesized into
routes at match time, so no deployed config breaks. When rebasing, expect a conflict
at the `_classify_source_kind` / `_is_owner_source` adjacency in `gateway/run.py` —
union-resolve (keep both methods, have `_classify_source_kind` delegate to
`_is_owner_source`).

**#14883 (routing-context-user-id):** Stacked on #12234. Adds trusted-platform
`user_id` to the routing context so `model.routes` can match a specific sender in
group channels. Suppresses `user_id` for spoofable transports (`webhook`,
`api_server`) so caller-supplied identity cannot trigger owner routes.

**#14884 (agent-actor-runtime-stacked):** Stacked on #14883. Adds
`gateway/agent_actor.py`, a state-db event log for inbound/outbound/directive
activity, an ephemeral runtime-state packet injected into gateway turns, the
read-only `self_state` tool, and outbound policy checks for `send_message` and cron
delivery. On fork main it overlaps with `feat/user-unify` and `fork-only`: keep
actor `infer_platform_authority` owner detection before `_is_owner_source`, keep
trusted `user_id` routing context, and keep both core tools (`self_state` and
`integrations`) in `toolsets.py`.

**#11647 (mcp-sse-transport):** During the April 2026 rebase, upstream had added
`ssl_verify` handling in `MCPServer._run_http` at the same line where this branch
adds transport selection. Union-resolve by keeping both
`ssl_verify = config.get("ssl_verify", True)` and
`transport = self._resolve_http_transport(url, config)`.

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

**#12590 (epub):** One-line addition to `SUPPORTED_DOCUMENT_TYPES` in
`gateway/platforms/base.py`. Every platform handler (telegram, slack, discord, feishu,
whatsapp) imports and uses this allowlist, so adding `.epub → application/epub+zip`
there lights up epub uploads across all of them. Paired with the OCR skill's
`marker` extractor which already supports epub.

**#12702 (html):** Same shape as #12590 — adds `.html` and `.htm` → `text/html`
to the same allowlist. Expect a union-merge conflict with #12590 on the
`SUPPORTED_DOCUMENT_TYPES` dict and the parametrize list in
`tests/gateway/test_document_cache.py` when rebuilding fork main.

**#12606 (rehydrate-compaction-summary):** `ContextCompressor._previous_summary` is
an in-memory field that enables the UPDATE-prompt compaction chain (preserves prior
structured summary instead of re-summarizing from scratch). A fresh `AIAgent` — new
gateway turn after cache eviction, process restart, session reload — always starts
with it `None`, so the next compaction falls back to INITIAL prompt and re-blurs the
existing `[CONTEXT COMPACTION]` marker as raw content. Fix: scan transcript newest→
oldest for a `SUMMARY_PREFIX` (or legacy `[CONTEXT SUMMARY]:`) message, strip the
prefix, seed `_previous_summary` with the body. Called once per turn in
`run_conversation` right after `_hydrate_todo_store`.

**#12686 (route-aware-background-agents):** When a routed turn (owner DM → slate-3 at
a dedicated LiteLLM integration) ends, follow-up hygiene/review/flush agents were
inheriting a *partially* routed bundle. Two bugs: (1) `_flush_memories_for_session`
called `_resolve_session_agent_runtime(session_key=...)` without `source`, so
`apply_route` short-circuits on empty context and the flush agent gets config
defaults. (2) `_spawn_background_review` constructed `AIAgent(model=self.model,
provider=self.provider)` without `base_url`/`api_key`, and `AIAgent.__init__`
re-resolved them from `resolve_provider_client` → `_try_custom_endpoint()` → config
default. Both paths converge on model=routed + base_url=default → 401
`key_model_access_denied` on integration keys scoped to one model. Fix: recover
`source` from `session_store._entries[session_key].origin` in the flush path;
forward the full bundle (base_url, api_key, api_mode, acp_command, acp_args,
credential_pool) at the review spawn site.

## Fork-only changes

**Skill pruning (fork-only branch):** Deleted skills irrelevant to provisioned agents
(distribution/comms for solo devs and startups). Reduces system prompt size, inference
cost, and LLM confusion. Deleted categories: apple, data-science, gaming, leisure,
mlops, red-teaming, smart-home, social-media, plus empty category stubs (diagramming,
domain, feeds, gifs, inference-sh). Partial deletions: creative (kept ideation,
excalidraw, p5js, popular-web-designs), media (kept gif-search, youtube-content),
productivity (kept nano-pdf, ocr-and-documents). 35 skills remain from original ~78.

**Integrations tool (fork-only branch):** `tools/integrations_tool.py` exposes the
provisioner-written manifest at `$HERMES_HOME/integrations.json` as a discoverable
tool. Fork-only because it's specific to the exe.dev proxy model and references
`niyant@slate.ceo` as the platform admin contact. Auto-registers via
`tools/registry.py:discover_builtin_tools()`; listed in `_HERMES_CORE_TOOLS` in
`toolsets.py` so it's exposed to every platform. If provisioner isn't wired up, the
manifest file is absent and the tool returns an explanatory empty result. Keep
`tests/tools/test_registry.py`'s builtin discovery expectation in sync with this tool;
on fork main the expected set should include both `integrations_tool` and, after
merging #14884, `self_state_tool`.
