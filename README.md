# dexi_x_radar

A reply radar for an X account. It watches a few accounts and your own mentions, then
sends reply drafts to your phone (Telegram), written in your voice by Claude. **You post
every reply yourself** from the X app. The radar never posts, likes or follows.

Built for [@DexiScan](https://x.com/DexiScan). Reply rules: [`PLAYBOOK.md`](PLAYBOOK.md).

> **Status:** phase 1. The service (`python -m dexi_x_radar serve`) and its MCP tools work.
> Drafts come from Claude Code on the owner's PC. `radar_poll.py` is the older one-shot
> script, kept for manual runs.

## How it works

```
             ┌──────────────── dexi_x_radar service ────────────────┐
X API ──────►│ poller (every 5 min)  → SQLite: cursor, queue,       │
             │                         drafts, sent replies, feedback│
             │ Telegram bot ─────────────────────────────────────────┼──► your phone
             │ MCP server (HTTP + token) ◄──────────────┐            │
             └──────────────────────────────────────────┼────────────┘
                                                        │
                        Claude (writes the drafts) ─────┘
                        • Claude Code on your PC (subscription), or
                        • a Claude app with the MCP connector, or
                        • later: a model inside the service (API key)
```

1. **Poll.** Every `POLL_INTERVAL_SECONDS` (default 300 s) the service reads the X API:
   new original posts from `watchlist.txt`, mentions of `X_OWN_HANDLE`, and your own new
   replies. It uses `since_id`, so each post is read (and paid for) once.
2. **Queue.** New posts go into the queue. The bot sends each one to your phone right
   away, marked *draft pending*.
3. **Draft.** Claude takes pending posts over MCP, adds data (for DexiScan: x-brief and
   Whale Radar), writes 2-3 drafts per `PLAYBOOK.md` and saves them back. The bot then
   edits the same Telegram message to show the drafts.
4. **You post.** **↩** opens X with the reply prefilled. **Copy** copies it.
   **Edit** takes your instruction and redrafts. **Skip** asks for a reason.
5. **Learn.** The radar sees what you actually posted and how it did (see *Feedback*).

## Two ways to draft

Both use the same MCP server; the difference is who calls it and when.

### A. Local Claude with a subscription (default, phase 1)

Claude Code on your own machine, logged in with your Claude subscription, runs the
`/radar-draft` command from this repo (`.claude/commands/radar-draft.md`) in a loop:

```
/loop 10m /radar-draft
```

- No Anthropic API key and no per-token cost. It uses your subscription, which is meant
  for your own interactive use. That is why this mode runs on your PC rather than on a
  server.
- Drafts are written by the strongest model you have in Claude Code.
- Drafts only appear while that session is running. Posts still reach your phone when it
  is off, marked *draft pending*. A `/loop` expires after 7 days; start it again.

### B. MCP server from any Claude client (on demand)

The service is an MCP server, so any Claude client can use it as a tool. This includes
Claude Code anywhere, and Claude apps once the server is added as a custom connector
(public HTTPS needed). Ask in plain words:

- "show the radar queue"
- "redraft #12, shorter, end with a takeaway"
- "what did I post today and which drafts did I use?"
- "summarise this week's feedback and suggest PLAYBOOK changes"

This is how you work with the radar from a phone or a second machine, and how the weekly
playbook review happens.

### Later: drafts inside the service

Set `ANTHROPIC_API_KEY` (and `DRAFT_MODEL`), and the service writes drafts itself, with
no PC needed. This is billed per token through the Anthropic API, separately from any
subscription.

## What the radar remembers

All in its own SQLite file (`DB_PATH`). It never connects to any other database.

1. **Where it stopped.** The last post id per source, so a restart neither re-reads nor
   re-pays.
2. **The draft queue.** A Telegram button carries only a short id, so the text lives
   here. MCP reads the queue from here too.
3. **Your feedback.** What you posted, which draft it came from, what you changed, and
   how it did.
4. **What was already sent to you.** The same post never reaches your phone twice.

## How it knows what you posted

Tapping ↩ only opens X; it does not prove you posted. So the poller also reads your own
latest posts every cycle (cheap "owned reads"):

- A reply whose `replied_to` is a queued post marks that post **sent** and stores your
  final text. This works whether you used ↩, Copy or typed the reply yourself.
- The final text is compared with each draft:
  - **used A / B / C** when it matches closely,
  - **merged** when it combines drafts,
  - **own** when it matches none.
  The diff (what you added or cut) is stored.
- A queued post with no reply after 6 h becomes **ignored**, which is an implicit skip.

## Feedback

| Signal | Source | Tells us |
|---|---|---|
| Which draft, what you changed | your posted reply vs the drafts | voice and format rules |
| Skip + reason (off-topic / weak data / tone / too late) | Telegram button | triage and watchlist |
| Edit instructions ("shorter", "add a takeaway") | Telegram reply | recurring fixes |
| Likes, replies, impressions after 24 h; did the author answer | X API, one read per reply | what actually works |

Once a week Claude reads the feedback over MCP and proposes concrete `PLAYBOOK.md`
changes as a diff. You approve them, and they are committed. Accounts that are always
skipped get proposed for removal from the watchlist.

## Setup

### 1. X API

1. Sign in at [console.x.com](https://console.x.com) **as the account you reply from**,
   so that reading its own mentions and replies counts as cheap owned reads.
2. Create an app and buy a few dollars of pay-per-use credit.
3. Copy the app's **Bearer Token** into `X_BEARER_TOKEN`. It is shown only once.

### 2. Telegram

1. In Telegram: [@BotFather](https://t.me/BotFather) → `/newbot` → token into
   `TELEGRAM_BOT_TOKEN`. Keep it secret. If it leaks, use `/revoke` and paste the new one.
2. Send `/start` to your bot.
3. Run `python -m dexi_x_radar chat-id` and put the number into `TELEGRAM_CHAT_ID`. The
   bot answers only this chat.

### 3. `.env`

```bash
cp .env.example .env     # then fill it in
```

| Variable | Required | What |
|---|---|---|
| `X_BEARER_TOKEN` | yes | X API app-only token |
| `X_OWN_HANDLE` | yes | your account, without @ |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | yes | the bot and your chat with it |
| `MCP_TOKEN` | yes | shared secret for the MCP server |
| `MCP_PATH` | no (`/mcp`) | URL path of the MCP server (see *Troubleshooting*) |
| `LOOKBACK_HOURS` | no (2) | how far back the very first poll reads |
| `POLL_INTERVAL_SECONDS` | no (300) | how often to read X |
| `MAX_POSTS_PER_POLL` | no (40) | cost guard per source per poll |
| `WATCHLIST_FILE` | no (`watchlist.txt`) | accounts to watch |
| `DB_PATH` | no (`data/radar.db`) | the SQLite file |
| `PORT` | no (8080) | HTTP port (Railway sets it) |
| `DEXISCAN_BASE_URL`, `X_BRIEF_KEY`, `API_READ_KEY` | no | DexiScan data for drafts (read-only keys) |
| `ANTHROPIC_API_KEY`, `DRAFT_MODEL` | no | later: drafts inside the service |

### 4. Run it

**Locally:** everything on your PC; MCP at `http://localhost:8080/mcp`.

```bash
pip install -r requirements.txt
python -m dexi_x_radar chat-id     # once, after /start to the bot (service stopped)
python -m dexi_x_radar poll-once   # one poll cycle, prints what it did and cost
python -m dexi_x_radar serve       # the service: poller + bot + MCP
python -m dexi_x_radar status      # queue counts
```

Run **one instance at a time**. Telegram lets only one process read the bot's updates, and
each instance keeps its own cursors, so two instances would also read and pay twice.

**On Railway:** the poller and bot run even when your PC is off.

1. New Railway project, then a service from this repository. No database plugin is needed.
2. Add a **volume** mounted at `/data` and set `DB_PATH=/data/radar.db`.
3. Set the variables from `.env` and generate a public domain for the service.

### 5. Connect Claude Code

```bash
claude mcp add --transport http dexi-x-radar https://<your-host>/mcp \
  --header "Authorization: Bearer <MCP_TOKEN>"
```

Then start the loop from *A* above in a Claude Code session in this folder.

## Troubleshooting

**MCP calls fail with "peer unexpectedly closed connection" while `/health` works.** Some
antivirus web filters inspect plain-HTTP MCP traffic. They act on requests whose path ends
in `/mcp` or that carry the `mcp-protocol-version` header, and cut the response off. Seen on
a Windows PC running Norton, over `localhost`. Fixes:
- serve MCP under another path (`MCP_PATH=/radar`), or
- use the Railway deployment over HTTPS, or
- add an exception for the service in the antivirus.

## Cost

| Item | Roughly |
|---|---|
| X API reads | $0.005 per post from the watchlist; owned reads $0.001. About $15-45/month for 20-30 accounts, depending on how much they post |
| Telegram | free |
| Drafts, mode A or B | included in your Claude subscription |
| Drafts, inside the service | Anthropic API per token, a few dollars a month at this volume |
| Railway (optional) | one small container and a 1 GB volume |

## Safety

- The radar never posts. Replies go out only when you press Post in X (X restricts API
  replies and AI reply bots anyway).
- The bot ignores every chat except `TELEGRAM_CHAT_ID`.
- The MCP server requires `MCP_TOKEN`.
- DexiScan keys are read-only.
- No cookies, no scraping: X data comes only from the official API.

## Files

| Path | What |
|---|---|
| `PLAYBOOK.md` | reply rules, grown from your feedback |
| `watchlist.txt` | accounts to watch |
| `radar_poll.py` | phase-0 poller (one run, writes `inbox/`) |
| `drafts/` | early rounds, kept as playbook evidence |
