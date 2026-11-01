# Observed client compatibility

| Client | SDK/transport | Observed result | Limits |
|---|---|---|---|
| Official Python SDK 2.3.0 | Actual stdio subprocess | Core discovery/call/reconnect, EU5 fixture scans, CK3 scan/index/source/contributor/report workflow pass | Synthetic sources only |
| Official Python SDK 2.3.0 | Legacy initialize and modern resolver paths | Native accept/decline/cancel, forgedargument refusal and exact-question races pass | Synthetic operator callbacks, not live human consent |
| Codex CLI 0.164.0-alpha.11.1 | Actual client-launched stdio | One personal_lab lab_capabilities call completed; core-only/core observed; process exited 0 | Protocol revision not captured; no native approval/game/live-state action requested |
| Installed persistent environment | Actual stdio, personal profile | All 54 tools discovered; saved CK3 investigation resumed with unchanged scan/index/job counts | No rescan, repair or game launch |
| Current desktop chat | Installed connector cutover | Requires reconnect to load new prefixed tool names | Loaded old tool definitions cannot prove a reconnect happened |
| Other external agents | Unverified | Standard MCP contracts available | No claim of tested compatibility |

The actual host smoke's sanitized receipt is `codex-host-smoke.json`. Raw local
host logs remain outside Git; no credentials, source contents or hidden model
reasoning are included in this receipt. Transport support does not establish game
capability or relax application approvals.
