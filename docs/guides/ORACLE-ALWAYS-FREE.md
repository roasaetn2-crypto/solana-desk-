# Oracle Cloud Always Free — the $0 always-on server for the Solana Desk

Render's free tier gives you 750 hours a month and puts your service to sleep after
15 minutes of no traffic. Oracle Cloud's Always Free tier gives you a **real virtual
machine that never sleeps**, no hour cap, no spin-down, for $0 indefinitely.

This guide takes you from nothing to a running desk on that VM in about 30 minutes.
It is the same code you already run in Termux — same repo, same `start.sh` — just on
a machine that does not get hot in your pocket.

---

## What you actually get (2026 numbers)

Oracle changed the Always Free limits in 2026. The old 4 OCPU / 24 GB Ampere A1
allowance was **halved to 2 OCPU / 12 GB**, enforced from 18 August 2026. Everything
below reflects the new limits.

| Resource | Always Free allowance | What the desk needs |
|---|---|---|
| Ampere A1 (ARM) compute | **2 OCPU, 12 GB RAM** | ~1 OCPU, 200 MB RAM |
| AMD micro compute | 2 × VM.Standard.E2.1.Micro (1/8 OCPU, 1 GB each) | fallback option |
| Block storage | 200 GB total | 20 GB is plenty |
| Outbound data | 10 TB / month | ~3 GB / month |
| Public IPv4 | included | included |

You are buying roughly **60× more machine than the desk needs**. That is fine — it is
free, and the headroom means you never think about memory again.

**The catch nobody mentions:** Oracle may reclaim Always Free compute instances that
sit idle (CPU below roughly 10%) for 7 days. A desk that scans on a timer is not
truly idle, but it is also not busy. See [Keeping the instance](#keeping-the-instance)
below for the mitigation.

---

## Step 1 — Create the account

Go to **https://cloud.oracle.com** and sign up.

Three things matter here and you cannot fix them later:

**Home region.** Your Always Free resources live in the region you pick at signup, and
**you cannot change it**. Pick the region physically closest to you. For most of
Africa and the Middle East, that is one of: *Johannesburg*, *Jeddah*, *Dubai*, *Abu
Dhabi*, or a European region. Latency matters because the desk polls APIs every cycle.

**A credit card is required.** Oracle uses it to verify you are a real person. Always
Free resources are not charged. A temporary ~$1 authorization may appear and drop off.

**Do not upgrade to Pay As You Go** unless you want to. Staying on Always Free means
you cannot accidentally be billed. The downside is that ARM capacity is harder to get
(see the next step).

---

## Step 2 — Create the VM (the part that usually fails)

This is where most people get stuck, so read it before clicking.

### The capacity problem

Oracle's free ARM capacity is oversubscribed. You will very likely see:

```
Out of host capacity.
```

This is not an error in your setup. It means the free ARM pool in your region is
full. Your options, in order of practicality:

1. **Try a different availability domain.** Oracle regions have 1–3 ADs. In the
   create-instance form, change *Availability domain* and retry. Often one AD has room.
2. **Retry over hours, not minutes.** Capacity frees up as other people's instances
   get reclaimed. Some people script a retry loop; for most, checking a few times a
   day for a couple of days works.
3. **Use the AMD micro shape instead.** `VM.Standard.E2.1.Micro` is 1/8 OCPU and 1 GB
   RAM. That is genuinely enough for this desk (it needs ~200 MB), and AMD micro
   capacity is usually available. **This is the pragmatic choice** — take it if ARM
   keeps failing. You can always migrate later.
4. **Upgrade to Pay As You Go.** Paid accounts get priority on capacity, and the
   Always Free allowance still applies to your bill. Only do this if you accept the
   small risk of a charge from something else you create.

### Create it

**Menu → Compute → Instances → Create instance**

| Field | Value |
|---|---|
| Name | `solana-desk` |
| Image | **Ubuntu 24.04** (or 22.04) — do not pick Oracle Linux, the guide assumes apt |
| Shape | `VM.Standard.A1.Flex` → **2 OCPU, 12 GB** (or `VM.Standard.E2.1.Micro`) |
| Primary VNIC | create new VCN, "Assign a public IPv4 address" = **Yes** |
| SSH keys | **Upload your public key** or let it generate a pair and **download the private key now** |

If you let Oracle generate the key pair, it gives you one download. Lose it and you
have to rebuild the instance. Save it.

Wait for the instance state to become **Running**, then note the **Public IP address**.

### Connect

```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@<PUBLIC_IP>
```

On Windows, use PowerShell or WSL with the same command. If SSH hangs, the security
list is not the problem (port 22 is open by default) — it is usually a wrong username.
Oracle Ubuntu images use `ubuntu`, not `root` and not `opc`.

---

## Step 3 — Install the desk

One command. It installs Python, clones the repo, builds a virtualenv, generates your
login key, writes a systemd service, starts it, and health-checks it.

```bash
sudo apt-get update -qq && sudo apt-get install -y -qq git
git clone https://github.com/roasaetn2-crypto/solana-desk-.git ~/solana-desk
bash ~/solana-desk/deploy/oracle/oracle-setup.sh
```

You will see it work through each stage and finish with a green summary containing
your login key.

### Why this is faster than Termux

On Linux aarch64, `pydantic-core` ships a **prebuilt wheel**. There is no Rust compile.
The step that took 10 minutes on your phone takes about 10 seconds here. This is the
single biggest practical difference between the two setups.

### What the script created

| Path | What it is |
|---|---|
| `~/solana-desk` | the git checkout |
| `~/solana-desk/.venv` | Python virtualenv with the deps |
| `~/.desk_key` | your dashboard login key, mode 600 |
| `~/desk-data` | `journal.jsonl`, `paper_ledger.json`, settings, `desk.log` |
| `/etc/systemd/system/solana-desk.service` | the service that keeps it running |

The service sets `DESK_DATA_DIR=~/desk-data`, so your journal and paper ledger live
**outside** the git checkout. `git pull` will never touch them.

### Verify

```bash
sudo systemctl status solana-desk
curl -s http://127.0.0.1:8000/api/health
tail -f ~/desk-data/desk.log
```

The health endpoint should return `{"ok":true,"running":true,...}`. The log should
show `Uvicorn running on http://0.0.0.0:8000`, then `scan: cycle 1: N pools` and
`pumpportal: connected`.

---

## Step 4 — Reach the dashboard

Right now the desk listens on `0.0.0.0:8000` but **Oracle's firewall blocks it**.
Two independent firewalls are in the way, and you must open both if you want a public
port. Most guides forget the second one.

### Option A — Cloudflare Tunnel (recommended)

No open ports, no public IP exposure, real HTTPS, free.

```bash
bash ~/solana-desk/deploy/oracle/cloudflared-setup.sh
```

It installs `cloudflared`, registers it as a systemd service, and prints something
like:

```
https://random-words-here.trycloudflare.com
```

Open that on your phone, log in with `cat ~/.desk_key`. Traffic goes out from the VM
to Cloudflare; nothing inbound is exposed. This is strictly safer than opening a port.

**The trade-off:** the quick-tunnel URL is random and **changes when the service
restarts**. For a permanent URL you need a Cloudflare account, a domain, and a named
tunnel:

```bash
cloudflared tunnel login
cloudflared tunnel create solana-desk
cloudflared tunnel route dns solana-desk desk.yourdomain.com
cloudflared tunnel run --url http://127.0.0.1:8000 solana-desk
```

Then change the systemd `ExecStart` to use `tunnel run solana-desk` instead of
`--url`. Cloudflare's docs cover the `config.yml` details.

### Option B — Open the port (two firewalls)

**Firewall 1: the VCN security list.**

*Networking → Virtual Cloud Networks → your VCN → Security Lists → Default Security
List → Add Ingress Rule*

| Field | Value |
|---|---|
| Source Type | CIDR |
| Source CIDR | `0.0.0.0/0` |
| IP Protocol | TCP |
| Destination Port Range | `8000` |

To restrict it to your own IP only, use `YOUR.IP.HERE/32` as the source CIDR. Better.

**Firewall 2: the OS-level firewall.** Oracle's Ubuntu images ship with iptables rules
that persist across reboots and will silently drop your traffic even after the VCN
rule is added. This is the step people miss.

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
sudo netfilter-persistent save
```

Then `http://<PUBLIC_IP>:8000` works.

**Honest warning:** a public port guarded only by a token is a real exposure. Bots scan
for open ports constantly. Your `DESK_TOKEN` is 24 random bytes so it will not be
guessed, but you are still advertising an endpoint. Option A avoids this entirely.

---

## Step 5 — Keep it running

systemd handles this. `Restart=always` means the desk comes back if it crashes, and
`systemctl enable` means it starts on boot. You do not need a supervisor script here —
that was a Termux workaround for Android's process killer. Linux does this natively.

```bash
sudo systemctl restart solana-desk    # restart
sudo systemctl stop solana-desk       # stop
sudo systemctl disable solana-desk    # do not start on boot
journalctl -u solana-desk -n 100      # service-level logs
tail -f ~/desk-data/desk.log          # app logs
```

### Updating the code

```bash
bash ~/solana-desk/deploy/oracle/oracle-setup.sh
```

It is idempotent. It pulls `main`, reinstalls deps, keeps your existing `~/.desk_key`,
and restarts the service. Your journal and ledger in `~/desk-data` are untouched.

### Keeping the instance

Oracle can reclaim Always Free instances that are idle for 7 days. A scanner polling
every minute or two is not idle in the usual sense, but CPU usage is low. If you want
to be safe, a trivial keep-busy timer is enough:

```bash
sudo tee /etc/systemd/system/keep-busy.service >/dev/null <<'EOF'
[Unit]
Description=Light CPU pulse to keep the Always Free instance marked active
[Service]
Type=simple
ExecStart=/bin/bash -c 'while true; do timeout 20 bash -c "while :; do :; done"; sleep 280; done'
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now keep-busy
```

That burns about 6% of one core in short bursts. It is a hedge against a policy that
Oracle enforces inconsistently, not a guarantee.

### Backups

Your entire trading history is three files:

```bash
tar czf ~/desk-backup-$(date +%F).tar.gz -C ~ desk-data .desk_key
```

Copy that off the VM occasionally. `journal.jsonl` is your audit trail — every
decision the desk made, including every rejection. It is the most valuable file there.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Out of host capacity` | free ARM pool full | try another AD, or use `VM.Standard.E2.1.Micro` |
| SSH: `Permission denied (publickey)` | wrong user or wrong key | user is `ubuntu`; check `-i` path and `chmod 600` |
| Health check fails, log says `ModuleNotFoundError` | venv not used | the unit sets `PATH` to `.venv/bin`; confirm `~/solana-desk/.venv/bin/python -c "import fastapi"` |
| `Address already in use` | old process | `sudo systemctl restart solana-desk` |
| Dashboard unreachable, health OK locally | firewalls | open VCN ingress **and** iptables (both) |
| Tunnel URL 502 | desk not up yet | `sudo systemctl status solana-desk` |
| Instance stopped itself | idle reclaim | enable `keep-busy` above |
| `scan: cycle 1: 0 pools` | API rate limit or network | check `curl -s https://api.geckoterminal.com/api/v2/networks` |

---

## Termux vs Oracle — which should you run

| | Termux (phone) | Oracle Always Free |
|---|---|---|
| Cost | $0 | $0 |
| Always on | only if Android cooperates | yes, genuinely |
| Survives reboot | needs Termux:Boot | yes, systemd |
| Install time | 20–40 min (Rust compile) | ~5 min (prebuilt wheels) |
| Risk of dying | heat, battery, Android killer | Oracle reclaim policy |
| Public URL | needs Tailscale/Cloudflare | Cloudflare Tunnel built in |
| Electricity | your phone charger | none to you |

**Run Oracle as the primary and keep Termux as a backup.** The desk writes its state
to disk, so you can move between them by copying `desk-data`. Having two is not
redundancy theatre — a phone that overheats and a VM that gets reclaimed fail in
completely different ways.

---

## One last thing

`LIVE_DISABLED` is set in this repo. The desk runs paper trading only, on Oracle or
anywhere else. Moving to a bigger machine does not change that, and it should not —
your 100-trade backtest came back at a 13% win rate with 81% rugs. Fix the entry
filter before you think about real money.
