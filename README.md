# 🎯 Roblox Free-Item Sniper — Discord Bot

A self-hosted Discord bot that watches the Roblox catalog for **free (R$0) items**
— including free limited UGC drops — and buys them the moment they appear, then
posts the result to a Discord channel.

```
Discord slash command ──▶ scan worker ──▶ catalog.roblox.com search
                                   │
                                   ├─▶ filter: price == 0, purchasable, not owned
                                   ├─▶ inventory check (skip what you own)
                                   └─▶ purchase (economy API / marketplace-sales)
                                          └─▶ Discord embed: ✅ bought / ❌ failed
```

---

## ⚠️ Read this first

- Automating an account violates the [Roblox Terms of Use](https://en.roblox.com/info/terms).
  Accounts that do it can be rate-limited, flagged, or **banned**. **Use a throwaway
  account**, never your main.
- Your `.ROBLOSECURITY` cookie is a **full-account credential** — whoever has it can
  read your email, change your password and spend your Robux. Keep it in `.env`
  only, never in a repo, never in a screenshot.
- Free limited items have finite supply. Sniping drains stock other players
  would otherwise get. That's your call, but it is what this tool does.
- Keep the scan interval sane (8–15 s). Hammering the catalog is the fastest way
  to get a 429 storm or an account action.

---

## Quick start

**One command** (creates the venv, installs deps, walks you through `.env`,
runs the offline self-test, then starts the bot):

```bash
cd roblox-sniper-bot
bash setup.sh              # macOS / Linux   (add --no-run to skip starting)
```

```powershell
cd roblox-sniper-bot
.\setup.ps1                # Windows         (add -NoRun to skip starting)
```

Or do it by hand:

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python selftest.py                                     # 31 offline checks
cp .env.example .env                                   # then fill it in
python bot.py
```

`.env` is the **only** place secrets belong, and it is already listed in
`.gitignore` — never paste a token or cookie into `bot.py`, a screenshot, a chat,
or a commit. If `.env` is ever leaked: reset the Discord token and log out of all
Roblox sessions immediately.

### 1. Discord bot

1. <https://discord.com/developers/applications> → **New Application** → **Bot**.
2. **Reset Token** → put it in `DISCORD_TOKEN`.
3. No privileged intents are needed (the bot only uses slash commands).
4. **OAuth2 → URL Generator** → scopes `bot` + `applications.commands`,
   permissions `Send Messages` + `Embed Links` → invite it to your server.
5. Put your server id in `GUILD_ID` so `/snipe` appears instantly.

### 2. Roblox account

Use a **throwaway account** you're willing to lose.

**Easiest — log in through a browser:**

```bash
pip install playwright && playwright install chromium   # one-time, ~130 MB
python connect_roblox.py
```

A Chromium window opens on the Roblox login page. Log in (2FA included), and the
script pulls `.ROBLOSECURITY` out of the browser, verifies which account it belongs
to, and writes it into `.env` with `600` permissions. No DevTools, no copy-paste.

```bash
python connect_roblox.py --print     # also print the cookie, for a host's env vars
python connect_roblox.py --timeout 600
```

This needs a desktop session — it opens a real window, so it won't work over plain
SSH. Generate the cookie on your laptop and paste it into the host's
`ROBLOX_COOKIE` variable.

**Manual — DevTools:**

1. Log into roblox.com in a **private/incognito** window.
2. DevTools (`F12`) → **Application** → **Cookies** → `https://www.roblox.com`.
3. Copy the **value** of `.ROBLOSECURITY` (starts with `_|WARNING:-DO-NOT-SHARE...`).
4. Paste it into `ROBLOX_COOKIE` in `.env`.

Anything that logs you out (password change, "log out of all sessions") invalidates
the cookie — re-run `connect_roblox.py` and restart the bot.

---

## Commands

All commands require **Manage Server**. Everything is under `/snipe`.

| Command | What it does |
|---|---|
| `/snipe start [channel] [interval] [pages] [max_per_cycle]` | Start the scanner, notifications go to `channel` (defaults to current) |
| `/snipe fast <on\|off> [interval]` | ⚡ Purchase-first polling of watched items (`0.6`–`5` s, default 1 s) |
| `/snipe stop` | Stop the scanner in this server |
| `/snipe status` | Account, Robux balance, filters, worker state, counters |
| `/snipe scan` | Run **one** scan and list what it *would* grab (never buys) |
| `/snipe grab [pages] [limit]` | One quick pass: buy the verified-free items it can see |
| `/snipe sweep start [limit] [pages]` | Walk the **whole catalog**, buying every verified-free item (resumable) |
| `/snipe sweep status` | Sweep progress: pages, items scanned, bought, rate |
| `/snipe sweep stop` | Stop the sweep (keeps its cursor, so you can resume) |
| `/snipe doctor` | Verify the cookie: which account, Robux balance, CSRF handshake, catalog access |
| `/snipe filter [category] [sort] [keyword] [exclude] [asset_types]` | Narrow what the scanner looks at |
| `/snipe watch <item_id \| url>` | Always poll one specific item (best for timed drops) |
| `/snipe unwatch [item_id]` | Remove one item, or clear the list |
| `/snipe history` | Last 12 purchase attempts |
| `/snipe buy <item_id \| url>` | Manual one-off purchase |
| `/snipe item <id \| url>` | Price, creator, stock and whether you own it |
| `/snipe whoami` | Which Roblox account is connected and whether it's healthy |
| `/snipe ping` | Discord gateway + Roblox API latency |
| `/snipe help` | Start-here guide with recipes for every common task |

### Getting it right

1. Deploy with `DRY_RUN=true`.
2. `/snipe doctor` — confirms the cookie resolves to the account you expect, that the CSRF
   handshake works, and that the catalog is readable.
3. `/snipe scan` — check the free items it returns actually look like what you want.
4. `/snipe filter exclude:"shirt,pants,face"` etc. until the list is clean.
5. Watch the log for a few cycles.
6. Flip `DRY_RUN=false` and `/snipe start`.

### `/snipe grab` — buying free items in bulk

```
/snipe grab pages:5 limit:25
```

Scans up to 5 catalog pages, then for every candidate:

1. **Re-reads the price from the catalog** — search results are seconds stale, so a
   snapshot that said R$0 may already have been repriced.
2. Rejects anything whose price is not provably zero. A missing price is *not*
   treated as free (`strictly_free` in `bot.py`).
3. Skips items you already own (via `inventory/.../is-owned`).
4. Sends the purchase with `expectedPrice: 0`, so even a race that reprices the
   item mid-flight fails safely instead of spending Robux.

`limit` caps purchases per run (1–50, default 10) and `pages` caps the scan. It is
intentionally bounded: the catalog holds thousands of free shirts and T-shirts, and
buying all of them would mean a 429 storm rather than a flex. The self-test proves
the guard — of four items that *looked* free in search results, only the one still
priced at zero gets bought.

Needs a catalog category (`/snipe filter category:all`), since it has nothing to
scan when category scanning is off.

### `/snipe sweep` — literally every free item

```
/snipe sweep start            # walk the entire catalog, buy every free item
/snipe sweep status           # how far it has got
/snipe sweep stop             # halt (cursor is kept)
```

`grab` is one pass over a few pages. `sweep` is the real thing: it **walks the whole
catalog page by page** (30 items per page, following Roblox's own pagination cursor)
and buys every item whose price is still zero when it arrives at it.

- **Resumable.** The cursor is persisted after every page, so a crash, restart or
  `/snipe sweep stop` picks up exactly where it left off — `start` resumes by default
  (`resume: false` starts over).
- **Same safety as `grab`.** Each candidate's price is re-read immediately before
  buying, `expectedPrice: 0` is always sent, and items you already own are skipped.
- **Caps.** `limit` (purchases) and `pages` (default 500) stop a runaway run.
- **Progress.** Posts to the server's configured channel every 10 purchases, plus a
  summary when it finishes or is stopped.

Be realistic about the scale: the free catalog is tens of thousands of items, so a
full sweep is a **multi-hour background job**, not an instant win. It is also the most
visible thing this bot does — thousands of purchases on one account is exactly what
Roblox's abuse detection looks for. Keep `limit` modest, run it on a throwaway
account, and expect to hit 429s (the client backs off and the cursor keeps your place).

### Filters worth knowing

- **category** — `all`, `collectibles`, `clothing`, `body`, `gear`, `accessories`,
  `animations`, `community` (or the raw catalog number: `1, 2, 3, 4, 5, 11, 12, 13`).
  `collectibles` (2) is where limited UGC drops live.
- **sort** — `updated` (default, catches fresh drops fastest), `relevance`,
  `sales`, `favorited`, `price_low`, `price_high`.
- **asset_types** — Roblox asset type ids, e.g. `8` (hat), `41/42/43` (hair/face/neck),
  `17` (head), `2` (T-shirt), `11` (shirt), `12` (pants). Empty = any.
- **keyword** — server-side search term; useful for a themed drop
  (e.g. `keyword:"free limited"`). Leave empty to scan everything.

### Fastest possible setup for a known drop

```
/snipe watch https://www.roblox.com/catalog/123456789/item-name
/snipe filter category:none      # stop wasting requests on catalog pages
/snipe fast on interval:1        # purchase-first polling every second
/snipe start
```

---

## ⚡ Fast mode (and the honest ceiling)

`/snipe fast on` switches the watched items to a **purchase-first** hot loop.

Normal mode spends three round trips before money moves:

```
GET item details  ->  GET is-owned  ->  POST purchase
```

Fast mode spends **one**, because it caches the product/seller fields when you run
`/snipe watch` and lets the POST itself be the availability check:

```
POST purchase   (on failure: refresh metadata once, retry only if it now looks free)
```

Measured with a simulated 150 ms Roblox RTT — reproduce it with `python selftest.py`:

| Mode | Requests per attempt | Wall clock |
|---|---|---|
| normal | 3 | ~455 ms |
| **fast** | **1** | **~153 ms** |

Other things that make it quick:

- **Two request lanes.** Purchases and watch polls use a separate clock and lock
  from catalog scans (`HOT_GAP` vs `REQUEST_GAP`), so a slow 30-item page can
  never sit in front of a purchase.
- **Pooled connections.** TLS sessions, DNS and keep-alive are reused, so the
  handshake is paid once instead of per poll.
- **Warm start.** `/snipe start` and `/snipe fast on` pre-fetch the CSRF token and
  item metadata, so the first poll is already warm.
- **Parallel candidates.** Multiple free items are attempted concurrently
  (`MAX_IN_FLIGHT`), not one after another.
- **No I/O in the loop.** Cooldowns live in memory and "not for sale yet" probes
  are never written to `state.json`, so a 1 s loop does zero disk writes.
- **No thumbnail API call** in the notification path.

### Where the ceiling actually is

Adding speed only helps until physics and Roblox's own limits take over:

1. **Round-trip time dominates.** From Europe to Roblox's US endpoints a single
   request is ~100–150 ms; from Australia ~250 ms. One request is already ~150 ms,
   and there is no code change that beats the speed of light.
2. **The queue is on their side.** Roblox serialises purchases server-side. When
   thousands of clients hit a free limited drop in the same 200 ms, being 5 ms
   earlier than the next bot rarely decides who wins — supply and queue position do.
3. **Rate limits are real.** Past roughly a few requests per second per account you
   start seeing 429s, and persistent hammering is exactly the pattern that gets an
   account actioned. `HOT_GAP=0.35` is a deliberate floor; going below ~0.25 mostly
   buys you errors, and each 429 costs you far more than the gap you removed.
4. **Free limiteds are gone in well under a second.** A bot with a residential
   connection polling at 1 s will miss most surprise drops; a watched, pre-cached
   drop at 0.6–1 s is the realistic best case.

So: fast mode roughly triples single-attempt speed and removes all avoidable
overhead. It does not turn a home connection into a datacentre, and no setting
here will out-race a farm of bots colocated with the API. Use it for drops you
actually know about (`/snipe watch`), where the pre-cached metadata is what buys
you the win.

---

## How the buying works

| Item kind | Endpoint |
|---|---|
| Ordinary free asset (`productId`) | `POST economy.roblox.com/v1/purchases/products/{productId}` |
| Limited UGC / collectible (`collectibleItemId`) | `POST apis.roblox.com/marketplace-sales/v1/item/{collectibleItemId}/purchase-item` |

Both POSTs need an `X-CSRF-TOKEN`, obtained by posting to `auth.roblox.com/v2/logout`
(the API returns 403 with the token in the response headers). The client caches it,
refreshes on 403, and backs off on 429/5xx.

An item is considered "free" when `price == 0`, `lowestPrice == 0`, or
`priceStatus == "Free"`. Before buying, the bot checks
`inventory.roblox.com/.../is-owned` so it doesn't waste requests on items you own.

Terminal failures (already owned, sold out, insufficient funds) are recorded and
the item is never retried. Soft failures get a 5-minute cooldown instead.

---

## Layout

```
bot.py            Discord front-end, per-guild scan worker, slash commands
roblox_api.py     async Roblox client: auth, CSRF, catalog, ownership, purchase
store.py          JSON state (config, seen ids, watch metadata, history)
storage.py        is the state directory a real volume? (survives redeploys?)
doctor.py         offline preflight: env, token, cookie, storage, deps
config.py         env parsing
connect_roblox.py browser login → cookie into .env (no DevTools)
selftest.py       offline checks: fast path, retries, lanes, doctor (no network)
setup.sh / setup.ps1  one-command install + configure + start
publish_github.sh     create the private GitHub repo and push
deploy_vps.sh         install as a systemd service on any always-on Linux box
docs/ORACLE_CLOUD.md  step-by-step Oracle Always Free VM guide
.env.example      template
Dockerfile        container image
```

State lives in `data/state.json` — delete it to reset everything.

---

## Deploy from GitHub (Railway / Render)

**One command creates the repo and pushes everything:**

```bash
./publish_github.sh              # needs the GitHub CLI, one browser login
```

It refuses to run if `.env` isn't gitignored (so a token can never be pushed),
creates a private repo, commits and pushes, then prints the Railway steps.

The repo ships deploy configs for both: `railway.json` + `Dockerfile` (Railway
builds the image and runs it as a worker), and `render.yaml` (a Render blueprint
that creates the worker, its env vars and a 1 GB disk in one go).

**It must run as a worker / background process, not a web service** — there is no
HTTP port to bind, and a web-service health check will kill it.

Set these environment variables in the host's dashboard (never in the repo):

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | your bot token |
| `ROBLOX_COOKIE` | `.ROBLOSECURITY` of the throwaway account |
| `GUILD_ID` | `1570754134276313299` (Kryv's sniper) — instant command sync |
| `DRY_RUN` | `true` until you have verified with `/snipe scan` |
| `DATA_PATH` | `/app/data/state.json` (must be on the persistent volume) |
| `LOG_LEVEL` | `INFO` |

**Attach a persistent volume at `/app/data`.** Without it, every redeploy wipes
`state.json` and the bot forgets which items it already bought — it will re-attempt
them on each restart.

Railway: *New Project → Deploy from GitHub repo* → it picks up `railway.json` →
add the variables above → add a volume mounted at `/app/data`.
Render: *New → Blueprint* → point at the repo → it reads `render.yaml` and asks for
the two secrets.

Free tiers spin workers down; the bot only snipes while the process is alive.

## Stop the state from being wiped

`data/state.json` remembers which items were bought and how far a sweep got. On a
host, the container filesystem is rebuilt on every deploy — so **the data directory
must be a mounted volume**, or the bot quietly starts from zero each time.

The bot now checks this itself, on start and in `/snipe doctor`:

```
WARNING sniper: STATE WILL BE WIPED ON REDEPLOY — no volume at /app/data
```

It compares the state directory's device id with the app directory's: a real mount
is a different device. That check is only enforced when a host is detected
(`RAILWAY_*`, `RENDER*`, `FLY_APP_NAME`, …) — on a VPS or your PC the disk persists
anyway, so no volume is needed.

To be sure before deploying, run the offline preflight:

```bash
python doctor.py
```

```
✅ Python — 3.11.2
✅ Dependencies — discord.py, aiohttp, python-dotenv
✅ Discord token — 74 chars
✅ Roblox cookie — 130 chars
✅ Guild id — 1570754134276313299 (instant command sync)
✅ State file — /app/data/state.json · ✅ volume · 4,120 MB free
✅ Buying — live
```

It exits non-zero on anything that would stop the bot from coming online.

## Truly free hosting

| Option | Free? | Stays awake? | Card needed | Verdict |
|---|---|---|---|---|
| **Your own PC / Raspberry Pi / old laptop** | yes | yes, while it's on | no | Cheapest real answer — `bash setup.sh`, or `deploy_vps.sh` for a systemd service |
| **Oracle Cloud "Always Free"** | yes, forever | yes | yes (verification) | Best free cloud: up to 4 ARM cores / 24 GB, never sleeps |
| **Fly.io / Railway / Render workers** | trial credit, then paid (~$2–5/mo) | yes | no | Easiest, but not free long-term |
| Free "Discord bot hosting" panels | yes | usually | no | Unreliable, small RAM, can vanish without notice |
| GitHub Actions cron | yes | **no** — 6 h cap per job | no | Not suitable for a 24/7 worker |

**Step-by-step for the best free option:** [`docs/ORACLE_CLOUD.md`](docs/ORACLE_CLOUD.md) —
account, instance, firewall, upload, install, verify.

### Free Discord bot panels (Pterodactyl)

Free hosts aimed at Discord bots — e.g. **bot-hosting.net** or **Wispbyte** — give you a
Pterodactyl panel and run the process for you. They are the least reliable option
(small RAM, activity/renewal rules, occasionally vanish), so they are fine for a
throwaway-account experiment and wrong for anything you care about.

Panel settings that work:

| Setting | Value |
|---|---|
| Egg / runtime | **Python 3.11+** |
| Startup file | `bot.py` |
| Memory | 512 MB is enough (the bot idles near 120 MB) |
| Install deps | `pip install -r requirements.txt` in the console, once |

Steps: upload `roblox-sniper-bot.zip` in the **File Manager** → unzip → create `.env`
in the same folder by pasting `.env.example` and filling in the token and cookie →
`pip install -r requirements.txt` → **Start**.

Panel file storage is persistent, so `data/state.json` survives restarts — that keeps
the sweep cursor and the "already bought" list.

### No repo? Publish one yourself

```bash
./publish_github.sh            # private
./publish_github.sh --public   # public
```

It still refuses to push if `.env` isn't gitignored — which matters much more for a
public repo.

For a Linux box that stays on, one command installs it as a service that starts on
boot and restarts on crash:

```bash
sudo bash deploy_vps.sh
```

Installs to `/opt/roblox-sniper`, creates a venv, asks once for the token and
cookie (hidden input), writes `.env` with `600`, and registers
`roblox-sniper.service` with `Restart=always`. Re-running is safe — the install
directory and `.env` are preserved.

```bash
systemctl status roblox-sniper
journalctl -u roblox-sniper -f
```

Set `NO_SERVICE=1` to install without systemd (containers, testing).

## Docker

```bash
docker build -t roblox-sniper .
docker run -d --name roblox-sniper --env-file .env \
  -v "$PWD/data:/app/data" --restart unless-stopped roblox-sniper
```

---

## Tuning

| Symptom | Fix |
|---|---|
| `429` warnings in the log | Raise `REQUEST_GAP` and `HOT_GAP`, raise `/snipe start interval` to 12–20 |
| Watched drop still too slow | `/snipe filter category:none` + `/snipe fast on interval:0.6` — and check `/snipe status` for the last request latency |
| Buys take 1–2 s and you still miss | Watch the exact item id instead of the catalog; lower interval to 3–5 s |
| Free items you don't want | `exclude:` words, or restrict `asset_types` |
| "Could not obtain a CSRF token" | Cookie is stale/logged out — refresh it, restart |
| `Purchase failed: ... Robux` | Account has no Robux and the item isn't actually free |
| Buys fail with "already own" | Expected — item was owned; bot marks it seen and moves on |
| Everything fails and you don't know why | Run `/snipe doctor` — it names the exact failing step and the account the cookie belongs to |

Multiple accounts? Run one container per cookie with different `DATA_PATH` and
`DISCORD_TOKEN` (or the same token if the accounts live in different servers).

---

## Troubleshooting

- **Slash commands missing** — set `GUILD_ID`, or wait up to an hour for global
  propagation. Re-inviting the bot with `applications.commands` fixes permission issues.
- **`cannot import name ... from config`** — run from the project directory so the
  local modules resolve: `python bot.py`, not `python roblox-sniper-bot/bot.py`.
- **Everything says "dry-run"** — `DRY_RUN=true` in `.env` unset or still set.
