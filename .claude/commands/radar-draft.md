---
description: Draft replies for the posts waiting in the X radar queue (dexi-x-radar MCP server)
---

Draft replies for the X reply radar. Work only through the `dexi-x-radar` MCP tools. Never post
anything to X: the owner posts every reply by hand.

1. Call `radar_pending` (limit 5). If `count` is 0, answer "Nothing pending." and stop.
2. Once per session, load the `humanizer` skill and read `radar_playbook`. Humanizer decides how
   a draft is written; the playbook says what a draft must carry. `owner_examples` in
   `radar_pending` are humanizer's writing sample: match them.
3. For each pending post, newest first:
   - **Triage.** If DexiScan data can't add anything to the post, call
     `radar_skip(post_id, reason)` with a short reason and move on.
   - **Data.** Call `dexiscan_xbrief` (`market: "xyz"` for stocks, indices and commodities).
     For every full wallet address in `addresses`, call `dexiscan_whale_radar`. Use numbers
     only from the post and these tools. Never estimate a figure.
   - **Owner input.** Apply `owner_instructions` if there are any. If `regenerate` is true, the
     owner wants a different angle than `previous_drafts`.
   - **Write 1-3 drafts** per the playbook. Each one opens with a DexiScan finding tied to the
     post and gives the readers something they didn't have.
   - **Write one general reply (G)** per the playbook, with no DexiScan data: constructive
     criticism of the post's claim, or a macro or economic point it leaves out. Numbers only
     from the post or simple math on them. Leave G out only when the post gives nothing to
     argue with or add to.
   - **Humanize.** Run the humanizer process on every draft, G included: mark the tells,
     rewrite, check that no fact or number was added or lost, search again for the tells that
     survive most often. Keep only the final text.
   - **Save.** Call `radar_save_drafts(post_id, drafts, general, note)`. `note` is one line
     naming the angle and the data used, e.g. `Whale flip vs ETF week · xbrief BTC book`. If
     the tool returns errors, rewrite the flagged drafts with humanizer and save again.
4. End with one line per post: the author and either "drafted" or "skipped: <reason>".
