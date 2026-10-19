---
description: Draft replies for the posts waiting in the X radar queue (dexi-x-radar MCP server)
---

Draft replies for the X reply radar. Work only through the `dexi-x-radar` MCP tools. Never post
anything to X: the owner posts every reply by hand.

1. Call `radar_pending` (limit 5). If `count` is 0, answer "Nothing pending." and stop.
2. Read `radar_playbook` once per session, and follow the `rudejohn-x-voice` skill. Where they
   conflict, the playbook wins (it holds the owner's own rules). Load the `quant-trader` skill
   once per session too; it supplies the lens for the general reply (G).
   `owner_examples` in `radar_pending` are the owner's rewrites of our earlier drafts ("this is
   how it should have sounded"). Compare each with `our_drafts` and write like the rewrite:
   its length, structure, word choice and takeaway. They outrank the voice skill too.
3. For each pending post, newest first:
   - **Triage.** Not worth a reply (meme, airdrop, memecoin, generic motivation, nothing we can
     add with data)? Call `radar_skip(post_id, reason)` with a short reason and move on.
   - **Data.** Call `dexiscan_xbrief` for current whale positioning (`market: "xyz"` for stocks,
     indices and commodities). For every full wallet address in `addresses`, call
     `dexiscan_whale_radar`. Use numbers only from the post itself or from these tools. Never
     invent or estimate a figure.
   - **Owner input.** If `owner_instructions` is not empty, apply it. If `regenerate` is true, the
     owner wants a different angle than `previous_drafts`.
   - **Write 1-3 drafts.** The first one is the full reply: data, a kicker, and our own takeaway
     at the end. The others are shorter alternatives. Hard rules:
     - write as **we** (DexiScan: we, our, us); *I* only occasionally, for a personal take;
     - no em or en dashes;
     - at most 280 characters each;
     - every sentence ends with punctuation;
     - $CASHTAGS;
     - never state the total number of tracked wallets;
     - no hashtags;
     - at most one emoji.
   - **Write one general reply (G)** with no DexiScan data.
     - Content: the single sharpest observation an experienced quant/macro trader would add to
       this post, for example:
       - the macro or liquidity regime;
       - funding and basis, or crowded positioning and who is forced to act;
       - the volatility regime or liquidation mechanics;
       - position sizing and risk of ruin;
       - a base rate the post ignores.
     - It ends with our takeaway.
     - Numbers: only ones from the post, or simple math on them (e.g. 14x leverage → a ~7% move
       liquidates). No outside figures you cannot source.
     - Same voice and hard rules as above.
     - Skip G only when the post gives nothing to add beyond our data.
   - **Save.** Call `radar_save_drafts(post_id, drafts, general, note)`. `note` is one line
     naming the reply type and the data used, e.g. `Report card · Whale Radar 0x020c…5872`. If
     the tool returns errors, fix the drafts and save again.
4. End with one line per post: the author and either "drafted" or "skipped: <reason>".
