# Prediction Draft Tracker

Rolling leaderboard for a friends' prediction-market competition: everyone gets
$100 on **Kalshi** and/or **Polymarket**, the tracker syncs each account every
5 minutes, and the leaderboard ranks everyone by P&L since the competition
started. A live feed shows everyone's recent bets.

## How syncing works

| Platform | What a participant shares | How it's used |
|---|---|---|
| **Polymarket** | Their public wallet address (no credentials at all) | Public Data API (`data-api.polymarket.com`) for positions value + trades; a public Polygon RPC call reads their USDC cash balance |
| **Kalshi** | A **read-only** API key (key ID + RSA private key) | Signed requests to the portfolio endpoints: balance, positions, fills. Read-only scopes mean the tracker can never trade or withdraw |

Scoring enforces the **$100 game format**: every account's baseline locks
automatically at its first sync, and the leaderboard shows a game bankroll of
`$100 + P&L since joining` — real account balances never leak into the game.
The admin's "lock baselines" button restarts everyone at $100. Mid-competition
deposits are against the rules; cash jumps that sells or settlements can't
explain get flagged on the leaderboard.

Beyond the leaderboard, each player has a detail page (`/player/{id}`) with
total wagered, win rate, biggest win/loss, a category breakdown (Baseball vs
Tennis vs Golf etc. — from Kalshi market categories and Polymarket event
tags), and a per-bet ledger with open/won/lost status.

## Participant onboarding

Send friends to **`/join`** — it walks them through getting credentials and
they add themselves using the shared `SIGNUP_PASSPHRASE`. They enter the game
at $100 the moment they sign up. The admin portal (`/admin`) can also add or
remove accounts manually.

**Polymarket:** profile → copy wallet address (`0x…`) — public info, no keys.

**Kalshi:** Account → Settings → API keys → create a key with **read-only
scopes only** (never enable trade/withdraw scopes for the tracker); the
signup form takes the key ID and the private key file contents.

## Running it

```bash
pip install -e .[dev]
cp .env.example .env        # set ADMIN_TOKEN at minimum
uvicorn app.main:app --port 8080
```

- Leaderboard: `http://localhost:8080/` (JSON at `/api/leaderboard`)
- Admin: `http://localhost:8080/admin` — add participants, force a sync,
  lock baselines
- Demo without network access: `MOCK_CONNECTORS=1 ADMIN_TOKEN=test uvicorn app.main:app`

Tests: `pytest`

## Deploying (Railway)

1. **Create the service**: Railway dashboard → New Project → Deploy from GitHub
   repo. Railway picks up the `Dockerfile` and `railway.json` automatically.
   (Note: Railway rejects Dockerfiles containing the `VOLUME` instruction —
   this one deliberately has none; volumes attach in the dashboard instead.)
2. **Attach a volume**: service → right-click / Settings → Attach Volume, with
   **mount path `/data`** (the image's `DB_PATH` already points at
   `/data/tracker.db`). Without this, history resets on every deploy.
3. **Set variables** (service → Variables):
   - `ADMIN_TOKEN` — e.g. `openssl rand -hex 16`
   - `SIGNUP_PASSPHRASE` — the shared passphrase friends use on `/join`
   - `CRED_SECRET` — encrypts Kalshi private keys at rest:
     `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
4. **Expose it**: Settings → Networking → Generate Domain. Railway injects
   `$PORT` and the container binds to it.

Redeploys are automatic on push to the connected branch. Any other host that
runs a Docker container with a persistent volume works too:
`docker run -d -v tracker-data:/data -p 8080:8080 -e ADMIN_TOKEN=… -e CRED_SECRET=… <image>`.

## Competition-day checklist

1. Deploy, set `ADMIN_TOKEN` + `SIGNUP_PASSPHRASE` + `CRED_SECRET`.
2. Everyone funds their account with $100 and signs up at `/join` with the
   passphrase — they enter the game at $100 automatically.
3. **Validate each account** with the live smoke test:
   `python -m app.smoke --polymarket 0x…` — a Polymarket US-app account that
   comes back empty despite having positions isn't covered by the public data
   API; that participant should compete on Kalshi instead.
4. Optional: at an official starting gun, hit **Lock baselines** on `/admin`
   to restart everyone at $100 simultaneously.
5. Share the leaderboard URL. Winner takes bragging rights.
