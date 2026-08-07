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

Scoring is **baseline-snapshot P&L**: when the competition starts, the admin
locks a baseline snapshot of every account's total value (cash + positions).
Score = current total − baseline, so pre-existing balances don't matter.
Mid-competition deposits are against the rules; cash jumps that sells or
settlements can't explain get flagged on the leaderboard.

## Participant onboarding

**Polymarket:** open your profile in the app/site and copy your wallet address
(`0x…`). That's it — it's public information.

**Kalshi:** Account → Settings → API keys → create a key with **read-only
scopes only** (never enable trade/withdraw scopes for the tracker). Send the
organizer the key ID and the downloaded private key PEM file.

The organizer adds each account on `/admin` (needs the admin token).

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
   - `CRED_SECRET` — encrypts Kalshi private keys at rest:
     `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
4. **Expose it**: Settings → Networking → Generate Domain. Railway injects
   `$PORT` and the container binds to it.

Redeploys are automatic on push to the connected branch. Any other host that
runs a Docker container with a persistent volume works too:
`docker run -d -v tracker-data:/data -p 8080:8080 -e ADMIN_TOKEN=… -e CRED_SECRET=… <image>`.

## Competition-day checklist

1. Deploy, set `ADMIN_TOKEN` + `CRED_SECRET`.
2. Everyone funds their account with $100 and sends their address / read-only key.
3. Add every account on `/admin`.
4. **Validate each account** with the live smoke test:
   `python -m app.smoke --polymarket 0x…` — a Polymarket US-app account that
   comes back empty despite having positions isn't covered by the public data
   API; that participant should compete on Kalshi instead.
5. At the starting gun, hit **Lock baselines** on `/admin` (it syncs first, then
   stamps baselines).
6. Share the leaderboard URL. Winner takes bragging rights.
