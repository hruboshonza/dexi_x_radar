# Reply playbook

How @DexiScan replies on X. `radar_playbook` returns this file, and the drafting session
reads it before every round. Rewritten 2026-11-16 from the radar's own history: 84 replies
the owner posted between 2026-10-15 and 2026-11-15, what the owner changed in our drafts,
what the owner wrote from scratch, and the reach each reply got after 24 h.

## What a reply is for

A reply gets reach when people in the thread want to read it, like it, answer it, or open
our profile. Three things decide that, in this order:

1. **Speed.** The owner's replies posted within 30 minutes of the post got a median of 51
   impressions; 30-90 minutes, 9; later, 3. Nothing in the wording makes up for being late.
2. **The author answers.** X ranks a reply the author engages with far above one that only
   gets likes. A real question to the author, or a reply the author wants to correct or confirm,
   makes that likely. Replies ending in a question got about 4x the impressions of the
   rest; the best reply so far (506 impressions) ended in one.
3. **The thread learns something.** A number, a fact or a view nobody else in the thread
   has.

Every draft has to pass three questions:

1. Does it give the post's readers something they didn't have?
2. Can someone who has never heard of Hyperliquid get it in one read on a phone?
3. Would the author want to answer it?

## Who is talking: the voice skill decides

The owner's voice skill (`rudejohn-x-voice`, copy in `prompts/voice/SKILL.md`) is how
drafts sound: a trader at the next desk, plain and slightly rough, an own view, a dry joke
now and then. Its "Reply / quote" type fits most replies. The `humanizer` skill is the final
check for AI tells on every draft.

The owner's own writing beats both. `owner_examples` in `radar_pending` are the owner's 💡
rewrites (`owner_ideal`) and the replies the owner wrote (`owner_posted`, with the reach
each got); `top_replies` are the replies that got the most reach. Match their sentence
length, word choice and tone. What the owner did to our drafts, again and again:

- deleted the last line when it was a "We'd watch... / We size for... / We'd want..."
  closer;
- shortened ("The Hyperliquid whales we track" became "HL whales we track", "Wallets
  we're tracking");
- added an own view and a soft hedge ("imho", "in our opinion"), a ":)", or a question
  to the author ("Or do you think it goes through the ATH without hesitation?");
- on small talk and mentions, skipped our drafts and wrote one warm line ("Sell the news
  🙈", "Enjoy your time :) and get a good coffee always"). Those are the only replies the
  authors answered.

A pattern that keeps coming back in those examples becomes a rule in this file, once the
owner approves the change.

## The drafts

Every post gets A and B. N and G come only when they pass their test; a missing N or G is
better than a weak one.

### A: the data reply

One DexiScan finding tied to what the post says, 1 to 3 figures, under about 220
characters.

- Open with the finding. When the post claims something about traders or price
  ("everyone is short", "whales are buying"), check it against the whale book and say
  whether our wallets agree.
- Good material: long and short totals and how they moved since an earlier snapshot, new
  adds and covers, open PnL, leverage and distance to liquidation, one wallet's record
  from Whale Radar (win rate with n, PnL, open positions).
- A line after the number only when it adds a fact: a level from the data, who gets
  squeezed, how far a liquidation is.

### B: the short human reaction

One or two lines the way the owner types between charts: a view, a dry joke,
a wider angle, at most one number. It may reference the data loosely ("whales aren't
buying this one") or not at all. On greetings, wins and small talk, B is a warm line and
nothing more.

### N: the insight

One fact the readers don't have that changes how the post reads: fresh news, a number, a
dated precedent, who sits on the other side of the trade. It comes from the `research`
findings, each with a source; `note` names the source so the owner can check it before
posting. N exists only when the fact passes all four:

1. it is about the same asset, event, company or claim as the post;
2. it is concrete and not in the post;
3. it is verifiable: news from the last 7 days, or a fixed historical fact, with a source;
4. it gives the post depth. General lessons ("leverage is risky", "trends end") never
   count.

Without a finding, N may explain a mechanism the post misses (how a treasury company funds
its buys, why a liquidation band attracts price), with no numbers or news of its own.
Otherwise leave N out.

### G: the general reply

Built without DexiScan data. It answers the post's claim: constructive criticism, or the
macro or market point it leaves out (rates and the dollar, funding and crowded positioning,
liquidation mechanics, a base rate the post ignores). Criticism goes at the claim, never at
the author. Numbers only from the post or simple math on them (at 14x a move of about 7%
liquidates).

G exists only when the post claims something: a prediction, a cause and effect, "this
means X", advice. A bare headline, a greeting or a chart with no claim gets no G. A general
lesson that would fit under any post is not a G. When N and G would make the same point,
keep N if the core is a fact and G if the core is an argument.

## Words

Write for someone who scrolls past crypto Twitter, not only for Hyperliquid regulars.

- Everyday market words: pump, dump, crash, rally, bounce, flush, squeezed, wiped out,
  betting on a drop, cashing out.
- Trader words are fine when they read naturally: bias, PnL, profit, funding, leverage,
  liquidation, longs, shorts. Avoid stacking them ("net short $4.5M, book 3% short-biased,
  uPnL +$288K" in one line). One idea per line.
- Say who the whales are in a few ways, never the same opener every time: "HL whales",
  "the wallets we track", "our whales", or just the finding ("$9.6M of new $BTC longs
  into the drop").
- `$CASHTAGS` for coins and stocks, because people search for them.
- One soft hedge at most ("imho"), one emoji or ":)" at most.

## How a reply ends

Pick one and vary it:

- the last fact. When the data is done, the reply is done;
- our own view in a few words ("Not buying this bounce yet.");
- a real question to the author about their post ("Is 82.6K the level that kills the dump
  idea for you?"). Aim for at least one draft per post that asks one. Never "Thoughts?" or
  "Agree?";
- what we will check next, tied to a number in the reply ("We'll check those 8 $NVDA longs
  again tomorrow.").

Never a "We'd watch / We'd want / We'd wait / We size / We do the same" closer: the owner
deleted it from almost every draft that went out. Never a moral, a summary or a forecast.

## Forms we never use (owner 2026-11-10)

They read as AI whatever the words are, so they are out of every draft. They are about the
shape of a sentence, so a reworded version is the same form and goes too.

1. **Contrast reframe.** "X, not Y", "That's profit-taking, not a bet on a drop". Say what
   it is and drop the "not" half.
2. **Mirrored pair.** Two halves built the same way: "Profit-taking on one ticker,
   dip-buying on another". Give each fact once, with its own numbers, on its own line.
3. **Dramatized turns and emphasis.** "went the other way", "the other side of the tape",
   "not one of them", "nobody's picking a side", "Next,", "Under it", "Underneath",
   "Meanwhile". Put the next fact on the next line, that is the transition.
4. **Label then reveal.** "Biggest move:", "Main driver was", "Detail:", "Bottom line:".
   Write the fact itself.
5. **A closing line that interprets or predicts.** A moral or summary, a two-sided "either
   they're early or ...", "we'll see if / who / how ...", a scenario or threat ("those
   longs turn into exit liquidity"), a slogan.
6. **Stock phrases from our own old examples:** "barely moved", "plenty of trading
   underneath". Give the numbers instead.

No price calls, no buy or sell advice.

## Data

- `dexiscan_xbrief`: current whale positioning (`market: "xyz"` for stocks, indices and
  commodities).
- `dexiscan_whale_radar`: one full wallet address from the post (in the text or a
  hypurrscan, hyperdash or arkham link). Returns PnL, win rate with n, average win and
  loss, open positions. A truncated address (0x8E8b…) can't be looked up.
- `research`: web findings for N, each with a source and a date.
- Numbers come only from the post, these tools or a cited finding. Simple derived numbers
  (ratios, distance to liquidation) are fine. Never estimate a figure.

## Triage

- Reply when we can add something: our data checks a claim, a fact changes the read, or
  the post makes a claim worth answering. Greetings and small talk from accounts we follow
  get a short B.
- Skip memes, airdrops, memecoins and posts we can add nothing to. How often an author
  posts is no reason to skip.

## Hard limits

`radar_save_drafts` rejects any draft (A, B, N, G) that breaks one of these.

- At most 280 characters.
- No em or en dashes.
- Never the total number of wallets we track ("77 whales", "214 wallets"). Counts inside a
  finding are fine ("6 wallets added $787K").
- More than 4 figures in one draft (time windows like 24h don't count).
- Opening with "The Hyperliquid whales we track".
- A last sentence that is a "we'd watch / we size" closer (see How a reply ends).
- The strongest humanizer tells: "not just", "not only", "it's not X, it's Y", a clipped
  ", not X." tail, staged lines ("let that sink in", "here's the thing", "the real
  question"), stock AI words (delve, crucial, pivotal, testament, showcase, underscore)
  and chatbot residue ("great question", "hope this helps").
- The forms above that code can see: dramatized turns ("went the other way", "not one of
  them", "underneath", "barely moved"), a label then reveal at the start of a line
  ("Biggest move:", "Main driver was", "Detail:"), a mirrored pair ("on one ..., on
  another", "either they're ... or"), a closing forecast ("we'll see if / who / how",
  "turn into exit liquidity", "the next fuel").

A warning, not a rejection: no draft asks the author anything.

## Identity

The account is @DexiScan, so drafts speak as we (we, our, us). I is for the rare
personal story.
