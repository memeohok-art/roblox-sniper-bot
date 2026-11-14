# Running the sniper on Oracle Cloud Always Free

Oracle's Always Free tier gives you a VM that **never sleeps and never expires** —
the only genuinely free "always on" option for this bot. No credit card charge
beyond a temporary ~$1 verification hold.

Total time: about 20 minutes, most of it waiting for the instance to boot.

> **What you do vs. what the script does.** You create the Oracle account and the
> instance (Oracle requires your identity + card, so nobody can do that for you).
> Everything after `ssh` — installing, configuring, starting, auto-restarting — is
> one command: `sudo bash deploy_vps.sh`.

---

## 1. Create the Oracle Cloud account

1. Go to <https://www.oracle.com/cloud/free/> → **Start for free**.
2. Email + password, verify the email.
3. **Phone verification** (SMS code).
4. Add a **credit/debit card** — a temporary ~$1 authorisation, not a charge.
5. Pick a **Home Region**.

   ⚠️ **This is permanent.** You cannot move an Always Free account to another
   region later. Pick the one physically closest to you that has free capacity —
   popular choices: `us-ashburn-1`, `eu-frankfurt-1`, `uk-london-1`,
   `ap-sydney-1`, `ap-singapore-1`.

   Closer to you = lower Roblox latency, which matters if you ever snipe a drop
   instead of sweeping.

Wait for the "your account is ready" email (usually minutes, occasionally hours).

---

## 2. Create the VM

Console → **Compute → Instances → Create instance**.

| Field | Value |
|---|---|
| **Name** | `roblox-sniper` |
| **Compartment** | your root compartment (or a new one) |
| **Image** | *Canonical Ubuntu 22.04* (or 24.04) — **not** Oracle Linux, the commands below assume apt |
| **Shape** | **Ampere `VM.Standard.A1.Flex`**, 1 OCPU / 6 GB **or** **`VM.Standard.E2.1.Micro`** (x86, 1 GB) |
| **Networking** | Create new VCN (the default is fine) · **Assign a public IPv4 address: Yes** |
| **Add SSH keys** | **Generate a key pair** → download the private key, *or* paste your own public key |
| **Boot volume** | leave the default (~47–50 GB; Always Free covers 200 GB total) |

**Shape notes**

- **ARM A1.Flex** is the best free option (up to 4 OCPU / 24 GB across instances)
  but frequently shows **"Out of capacity"** in busy regions. Try: a different
  availability domain, a smaller size (1 OCPU/6 GB), a different region, or retry
  later. It is not a bug on your side.
- **E2.1.Micro (x86, 1 GB)** is almost always available and is plenty for this bot
  (~120 MB RSS). You may run **two** of these for free.
- The bot is pure Python — it runs on ARM (`aarch64`) with no changes.

Click **Create**. Wait until **State: Running**, then copy the **Public IP address**.

---

## 3. Open the firewall — and why you mostly don't have to

**The bot needs no inbound ports at all.** It connects *out* to Discord and Roblox,
so nothing has to be exposed. This is what makes the setup safe: no open web port,
no attack surface.

What you need is only **SSH (22)**, which the default Oracle VCN security list
already allows — for that reason, **skip this section unless you plan to add a web
dashboard later**.

If you do want to open ports (optional):

1. Console → **Networking → Virtual Cloud Networks** → your VCN → **Security Lists**
   → *Default Security List* → **Add Ingress Rules**
   - Source CIDR `0.0.0.0/0`, IP Protocol `TCP`, Destination Port `80` (or `443`)
2. **And** the instance's own firewall, because Oracle's Ubuntu images ship with
   `iptables`/`netfilter` rules that block everything except SSH:

   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
   sudo netfilter-persistent save
   ```

Both layers are required — a rule in the console alone will still be blocked by the
instance firewall. (This trips up almost everyone, which is exactly why it's worth
skipping when you don't need it.)

---

## 4. Connect over SSH

```bash
chmod 600 ~/Downloads/ssh-key-*.key          # SSH refuses world-readable keys
ssh -i ~/Downloads/ssh-key-*.key ubuntu@<PUBLIC_IP>
```

- User is **`ubuntu`** for Ubuntu images (`opc` for Oracle Linux — we're not using that).
- First connect asks to accept the host fingerprint: type `yes`.
- On Windows, `ssh` and `scp` are built in (PowerShell); or use PuTTY/pscp.

---

## 5. Upload the project and run the installer

On your machine, in the folder containing `roblox-sniper-bot.zip`:

```bash
scp -i ~/Downloads/ssh-key-*.key roblox-sniper-bot.zip ubuntu@<PUBLIC_IP>:~/
```

Then on the VM:

```bash
sudo apt update && sudo apt install -y unzip
unzip roblox-sniper-bot.zip
cd roblox-sniper-bot
sudo bash deploy_vps.sh
```

If you'd rather paste the zip URL than scp it:

```bash
wget -O sniper.zip "<paste the download link from the chat>"
```

### What the script asks and does

1. Installs `python3` / `python3-venv` if missing.
2. Copies the bot to **`/opt/roblox-sniper`** (safe to re-run; keeps `.env`).
3. Creates a venv and installs `discord.py`, `aiohttp`, `python-dotenv`.
4. Prompts **once**, with hidden input:
   - `Discord bot token (hidden):`
   - `Roblox .ROBLOSECURITY cookie (hidden):`
   - `.env` is written with `600` permissions.
5. Registers **`roblox-sniper.service`** with `Restart=always` and
   `WantedBy=multi-user.target` — it starts on boot and restarts on crash.
6. Starts it and prints the commands below.

> The cookie prompt needs an interactive SSH session — that's normal. If you'd
> rather generate the cookie on your laptop, run `python connect_roblox.py --print`
> there, then paste the value at the prompt.

---

## 6. Verify

```bash
systemctl status roblox-sniper          # expect: active (running)
journalctl -u roblox-sniper -f          # live logs, Ctrl+C to leave
```

You want to see:

```
INFO sniper: Authenticated as <account> (<id>)
INFO sniper: Synced slash commands to guild 1570754134276313299
INFO sniper: Logged in as Synergy sniper#9574
```

In Discord: `/snipe whoami` → then `/snipe doctor`.
`whoami` proves the process is alive; `doctor` proves the cookie works.

---

## 7. Go live when you're ready

The installer sets `DRY_RUN=true`, so it reports but never buys. Once
`/snipe scan` shows sensible results:

```bash
sudo sed -i 's/^DRY_RUN=.*/DRY_RUN=false/' /opt/roblox-sniper/.env
sudo systemctl restart roblox-sniper
```

Then `/snipe doctor` again, and `/snipe sweep start` or `/snipe start`.

---

## Day-2 operations

| Task | Command |
|---|---|
| Change settings | `sudo nano /opt/roblox-sniper/.env` then `sudo systemctl restart roblox-sniper` |
| Refresh an expired cookie | re-run `python connect_roblox.py --print` locally, paste into `.env`, restart |
| Update the bot | upload the new zip, `unzip -o`, `cd roblox-sniper-bot && sudo bash deploy_vps.sh` (`.env` and `data/` survive) |
| Free disk / memory | `df -h` · `free -m` |
| Stop it entirely | `sudo systemctl stop roblox-sniper && sudo systemctl disable roblox-sniper` |

State lives in `/opt/roblox-sniper/data/state.json` — it remembers which items were
already bought, and the sweep's catalog cursor.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Permission denied (publickey)` | `chmod 600` the key; connect as **`ubuntu`**, not `root`/`opc` |
| Connection times out | Instance has no public IP, or you're on the wrong IP. Check the console |
| Shape: "Out of capacity" | ARM is oversubscribed in that region. Use `VM.Standard.E2.1.Micro`, another AD, or retry later |
| `deploy_vps.sh: python3-venv` error | The script retries after `apt install python3-venv`; if apt is locked, run `sudo apt update` first |
| Service keeps restarting | `journalctl -u roblox-sniper -n 50` — almost always a missing/expired cookie or a typo in `.env` |
| `/snipe` missing in Discord | Set `GUILD_ID` in `.env`, restart. Global commands take up to an hour |
| Lots of `429` warnings | Roblox rate-limiting. Raise `REQUEST_GAP` (1.2 → 2.0) and `HOT_GAP`, or lower `/snipe sweep start pages:` |
| Instance stopped after ~7 idle days | Oracle reclaims **idle** Always Free instances. A bot polling every second is not idle — this only bites if you stop the service |

---

## Cost

Always Free covers: 2 × `E2.1.Micro`, or up to 4 OCPU / 24 GB of `A1.Flex`
arm64 (3000 OCPU-hours + 18 000 GB-hours/month), 200 GB block storage total, plus
10 TB/month outbound transfer. This bot uses roughly **1 OCPU / 1 GB / 20 GB disk**
and a few hundred MB of egress per day — comfortably inside the free allowance,
indefinitely.
