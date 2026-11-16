---
description: Draft replies for the posts waiting in the X radar queue (dexi-x-radar MCP server)
---

Draft replies for the X reply radar. Work only through the `dexi-x-radar` MCP tools, plus WebSearch
for the insight reply N. Never post anything to X: the owner posts every reply by hand.

1. Call `radar_pending` (limit 5). If `count` is 0, answer "Nothing pending." and stop.
2. Once per session, read `radar_playbook`, load the `rudejohn-x-voice` skill (how drafts sound)
   and the `humanizer` skill (the final AI-tell check). `owner_examples` and `top_replies` in
   `radar_pending` are the owner's own writing and the replies with the most reach: match them.
3. For each pending post, newest first (reach falls off within 30 minutes):
   - **Triage.** If nothing is worth saying, call `radar_skip(post_id, reason)` with a short reason
     and move on.
   - **Data.** Call `dexiscan_xbrief` (`market: "xyz"` for stocks, indices and commodities).
     For every full wallet address in `addresses`, call `dexiscan_whale_radar`.
   - **Research for N.** When the post has a subject a fact could deepen (news, an asset, a
     company, a claim), run at most 2 web searches. Keep a finding only if it passes the
     playbook's four-point test for N; note its URL.
   - **Owner input.** Apply `owner_instructions` if there are any. If `regenerate` is true, the
     owner wants a different angle than `previous_drafts`.
   - **Write** per the playbook, in the owner's voice: A (data reply), B (short human reaction),
     N only with a finding that passes, G only when the post claims something. Aim for one
     real question to the author across the drafts.
   - **Humanize.** Run the humanizer check on every draft: mark the tells, rewrite, check that no
     fact or number was added or lost. Keep only the final text.
   - **Save.** Call `radar_save_drafts(post_id, drafts=[A, B], insight=N, general=G, note)`.
     `note` is one line naming the angle and the data, plus `· N: <url>` when N uses a finding.
     If the tool returns errors, rewrite the flagged drafts and save again.
4. End with one line per post: the author and either "drafted" or "skipped: <reason>".
