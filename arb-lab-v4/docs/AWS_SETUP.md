# Moving the paper bot to AWS EC2 (free-plan credits)

Paper trading only. Nothing here enables real orders.

**Cost** (on-demand, ap-south-1 Mumbai, approximate): t4g.small about $12/month + public IPv4
about $3.60/month + 20 GB gp3 about $1.60/month, about **$17/month**. Free-plan credits: $100 (+ up to
$100 more); the free-plan account closes after 6 months or when the credits run out, unless you upgrade.
Set a billing alarm (step 0) so nothing is a surprise.

## 0. Billing safety
AWS console → Billing → Budgets → Create budget → "Zero spend" or a $20 monthly cost budget with an
email alert.

## 1. GitHub token for status reports (so Claude can monitor the instance)
GitHub → Settings → Developer settings → Fine-grained personal access tokens → Generate:
* Repository access: **Only select repositories → Phaviwylera/polymarketbot**
* Permissions: **Contents: Read and write** (nothing else)
* Expiration: 90 days or more

The bot writes `status/latest.json` (every 15 min) and `status/hourly/*.json` to branch `bot-status`.
It contains paper results and health only; secrets and URLs are redacted. The repository is public,
so these files are public too.

## 2. Prepare the user-data script
Open `deploy/aws/user-data.sh` in a text editor and fill in the two values in the FILL IN block:
* `DATABASE_URL` — the Supabase **Session pooler** URI (port 5432) ending in `?sslmode=require`
* `STATUS_GH_TOKEN` — the token from step 1

Never paste these into chat or commit them.

## 3. Launch the instance
EC2 console (region **Asia Pacific (Mumbai) ap-south-1**) → Launch instance:
* Name: `polybot`
* AMI: **Ubuntu Server 24.04 LTS**, architecture **64-bit (Arm)**
* Instance type: **t4g.small**
* Key pair: "Proceed without a key pair" (use EC2 Instance Connect from the browser if you ever need a shell)
* Network: create a security group allowing **HTTP (80) from Anywhere** (dashboard). Do not open SSH to
  0.0.0.0/0; if you add SSH, restrict it to "My IP".
* Storage: **20 GiB gp3**
* Advanced details → **Metadata version: V2 only**; **User data**: paste the edited script
* Launch. Optional: Elastic IPs → allocate → associate with the instance so the address never changes.

Setup takes about 5–8 minutes (packages, clone, install). Log: `/var/log/polybot-setup.log`.

## 4. Handover (no double trading)
The database advisory lock allows exactly one bot to own the paper account; the other waits in standby.

1. Render must already be using Supabase (`POLY_DATABASE_URL` switched after the verified copy). Render
   holds the lock, so the new EC2 bot starts in **standby** and does not trade.
2. Check the EC2 bot is up: `http://<public-ip>/health` returns OK; `http://<public-ip>/poly` shows the
   standby snapshot.
3. **Suspend the Render service** (Render dashboard → arb-lab-v4-unified → Settings → Suspend). Its
   database connection closes, releasing the lock; within seconds the EC2 bot takes ownership, restores
   the paper account from Supabase and starts trading.
4. Claude verifies from `bot-status/status/latest.json`: equity, account P&L, trades, risk state and open
   positions equal the values before the handover; feeds live; storage healthy.

## 5. What runs on the instance
* `polybot.service` — the bot on port 80, restarts on failure, `MemoryMax=1800M`, all four lanes,
  research and wallet study **on** (`POLY_RESEARCH=1`, `POLY_WALLET_STUDY=1`, no paused lanes). The
  profiler keeps logging CPU; the status report shows load and memory.
* `polybot-update.timer` — every 5 minutes pulls `main`; restarts **only if the full test suite passes**
  on the new code, otherwise stays on the previous commit (`/var/log/polybot-update.log`).
* Secrets only in `/etc/polybot.env` (root, mode 600).

## 6. Rollback
* Resume the Render service and stop the EC2 instance: Render reacquires the lock and continues from
  the same Supabase state.
* Or terminate the instance entirely; the paper account lives in Supabase, not on the instance.

## 7. Before the 6-month free-plan limit
Either upgrade the AWS account to the paid plan (same instance keeps running, billed after credits) or
move again; the paper account is in Supabase, so moving is only a new host plus the same handover.

## 8. Bullpen wallet analytics (automatic, read-only)
The bot installs the pinned Bullpen CLI itself (checksum-verified, under `POLY_DATA_DIR/bullpen`) and
every 30 minutes records Bullpen's public wallet analytics for our top-scored wallets as
`BULLPEN_SIGNAL` events, next to our own score. Read-only (`--read-only`), never logs in, record-only:
it does not affect trading. Status: `http://<ip>/api/poly/bullpen` and `bullpen` in the status report.
Disable with `POLY_BULLPEN=0` in `/etc/polybot.env`.
