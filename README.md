# dexi_x_radar

Reply radar for @DexiScan on X. It watches a few accounts and @DexiScan mentions, then
gets reply drafts in the DexiScan voice to the owner's phone. Every reply is posted by
hand from the X app. The radar never posts.

Design: the "X/Twitter Radar" artifact (https://claude.ai/artifact/8NTTSM9vkMmx5X74k2xGpy).
Reply rules: `PLAYBOOK.md`.

## Phase 1 (now)

- **Drafts** are written by Claude (Opus) in a Claude Code session on the owner's PC,
  under the subscription. No Anthropic API key.
- **Storage** is the radar's own small store. It never touches the DexiScan prod database.
- **DexiScan numbers** come over HTTPS: `/api/x-brief` and Whale Radar
  (`/api/wallet/:address/lens`), read-only keys only.

```bash
python radar_poll.py --dry-run   # queries and worst-case cost, no calls
python radar_poll.py             # new posts since the last run -> inbox/latest.json
```

Configuration: `.env` (names in `.env.example`).
