# EvilGoPhish Infrastructure — Deploy Automation

> **For authorized penetration testing and red team engagements only.**

Automated deployment of a phishing infrastructure on a VPS using Ansible + Docker.
One config file, one command.

---

## Stack deployed

| Component | Role |
|-----------|------|
| **Evilginx3** | Reverse-proxy phishing (MFA bypass, session token capture) |
| **GoPhish** | Campaign management, email tracking |
| **EvilFeed** | Real-time session feed between evilginx3 and GoPhish |
| **Postfix + DKIM** | Outgoing SMTP with DKIM signing |
| **Dovecot** | IMAP/LMTP for mailbox delivery |
| **PostfixAdmin** | Mailbox management (web UI) |
| **Roundcube** | Webmail |
| **MariaDB** | Database backend for mail stack |

All services run in isolated Docker containers on a dedicated bridge network with static IPs defined in `deploy_config.yml`.

---

## Prerequisites

### Local machine (controller)
- Python 3.9+
- `pip3 install pyyaml ansible`
- SSH key pair with access to the VPS

### VPS (target)
- Ubuntu 22.04
- Docker + docker-compose-plugin
- Ports open: `22`, `25`, `53`, `80`, `443`
- Domain pointing to the VPS IP with a wildcard DNS record

### DNS (Cloudflare recommended)
```
A    yourdomain.com        → VPS_IP  (proxied OFF)
A    *.yourdomain.com      → VPS_IP  (proxied OFF)
MX   yourdomain.com        → mail.yourdomain.com  priority 10
```

### Cloudflare Origin Certificate
Generate a wildcard **Origin Certificate** (not a Let's Encrypt cert) in Cloudflare dashboard:
- `yourdomain.com` and `*.yourdomain.com`
- Save as `yourdomain.com.pem` and `yourdomain.com.key`

---

## Repository structure

```
.
├── deploy_infra.py          # Main deploy script
├── manage-phishlets.py      # Phishlet & lure management
├── deploy_config.yml        # Your config (gitignored — never commit)
├── deploy_config.example.yml  # Template to copy
├── evilginx3/               # Evilginx3 source (local build)
├── gophish/                 # GoPhish source (local build)
├── evilfeed/                # EvilFeed source (local build)
└── phishing_stats.py        # Post-campaign Excel statistics report
```

---

## Quick start

### 1. Copy and fill the config

```bash
cp deploy_config.example.yml deploy_config.yml
nano deploy_config.yml
```

Fill every field. The config drives the entire deployment — no hardcoded values in the scripts.

### 2. Deploy

**From your local machine (SSH remote deploy):**
```bash
pip3 install pyyaml ansible
python3 deploy_infra.py -c deploy_config.yml
```

**From the VPS itself (local deploy):**
```bash
# In deploy_config.yml set: options.local: true
python3 deploy_infra.py -c deploy_config.yml
```

**Dry run (no changes applied):**
```bash
python3 deploy_infra.py -c deploy_config.yml --dry-run
```

**Run a specific step only:**
```bash
python3 deploy_infra.py --list-steps
python3 deploy_infra.py -c deploy_config.yml --step evilgophish
```

---

## Phishlet management

```bash
# Run locally on the VPS
python3 manage-phishlets.py --local list
python3 manage-phishlets.py --local activate o365 login.yourdomain.com
python3 manage-phishlets.py --local sessions
python3 manage-phishlets.py --local lures

# Run remotely (via SSH)
python3 manage-phishlets.py activate o365 login.yourdomain.com
```

---

## Accessing admin interfaces

GoPhish and PostfixAdmin listen on localhost inside the VPS. Use SSH tunnels to access them.

```bash
# GoPhish admin panel
ssh -L 3333:127.0.0.1:3333 root@YOUR_VPS -N
# → http://localhost:3333
# Credentials printed in GoPhish logs on first start:
docker exec evilgophish cat /var/log/evilgophish/gophish.log | grep "login with"

# PostfixAdmin
ssh -L 8026:127.0.0.1:8026 root@YOUR_VPS -N
# → http://localhost:8026

# Roundcube webmail
# → http://YOUR_VPS_IP:8025
```

---

## deploy_config.yml reference

```yaml
vps:
  host:     "mail.yourdomain.com"   # VPS hostname or IP
  user:     "root"
  ssh_key:  "/path/to/private_key"  # Leave empty for local deploy
  ssh_port: 22

domain:  "yourdomain.com"
vps_ip:  "1.2.3.4"

subdomains:
  - login                           # Creates login.yourdomain.com

ssl:
  cert_local: "/path/to/yourdomain.com.pem"
  key_local:  "/path/to/yourdomain.com.key"

mail:
  deploy: false                     # Set true to deploy the full mail stack

evilgophish:
  deploy: true
  phishlet:          "o365"
  phishlet_hostname: "login.yourdomain.com"
  lure_path:         "/"
  unauth_url:        "https://www.microsoft.com"

docker:
  network: "phishing-net"
  subnet:  "10.10.0.0/24"
  ips:
    smtp:         "10.10.0.3"
    maildb:       "10.10.0.12"
    dovecot:      "10.10.0.10"
    postfixadmin: "10.10.0.13"
    roundcube:    "10.10.0.11"
    evilgophish:  "10.10.0.6"

repos:
  local: "."                        # Uses local evilginx3/ gophish/ evilfeed/

options:
  dry_run: false
  verbose: false
```

---

## Teardown

```bash
python3 deploy_infra.py -c deploy_config.yml --destroy
```

---

## Post-campaign statistics — phishing_stats.py

Generates a formatted Excel report from GoPhish campaign CSV exports (Events + Results files).

### Prerequisites

```bash
pip3 install pandas xlsxwriter
```

### Input files

Export from GoPhish: **Results → Export as CSV** and **Events → Export as CSV** for each campaign.
Expected filenames: `<campaign name> - Events.csv` / `<campaign name> - Results.csv`

Optional: `reported_users.txt` — one email per line, with optional report channel tags:
```
john.doe@corp.com **jira**
jane.smith@corp.com **teams**
bob.martin@corp.com **mail**
```

### Usage

```bash
# Scan current directory, no exclusions
python phishing_stats.py

# Explicit directory + output file
python phishing_stats.py -d ./campaign_data -o rapport_Q1.xlsx

# Exclude specific users by username
python phishing_stats.py --exclude soufiane.tahiri atia.nadhmi guillaume.decasaban

# Exclude users from a file (one email/username per line)
python phishing_stats.py --exclude-file excluded.txt

# Pass CSV files directly (multiple campaigns merged)
python phishing_stats.py \
  --events camp1_Events.csv camp2_Events.csv \
  --results camp1_Results.csv camp2_Results.csv

# Full example
python phishing_stats.py -d ./data -o out.xlsx \
  --exclude soufiane.tahiri \
  --exclude-file more.txt \
  --reported reported_users.txt \
  --timezone America/New_York
```

### Output — Excel sheets

| Sheet | Content |
|-------|---------|
| **Résumé Global** | Campaign funnel, behavioral segmentation, charts |
| **Identifiants saisis** | Victims who submitted credentials (with username/password) |
| **Cliqué sans creds** | Victims who clicked but did not submit credentials |
| **Signalements** | Users who reported the phishing email, breakdown by channel (Jira / Teams / Mail) |
| **Analyse Temporelle** | Reaction time buckets, hourly distribution, day-of-week analysis, multi-clickers |
| **Toutes les cibles** | Full target list with all interaction flags |

### Behavioral segments

| Segment | Meaning |
|---------|---------|
| Identifiants saisis | Credentials submitted (highest severity) |
| Cliqué sans creds | Clicked the link, no credentials submitted |
| Ouvert sans clic | Email opened, link not clicked |
| Aucune action | No interaction detected |

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Evilginx3 not capturing sessions | `docker exec evilgophish cat /var/log/evilgophish/evilginx3.log` |
| GoPhish not starting | `docker exec evilgophish supervisorctl status` |
| SSL error `EE certificate key too weak` | RSA 1024-bit key — rebuild with patch (already applied in Dockerfile) |
| Phishlet hostname not set after deploy | Run `manage-phishlets.py --local activate <phishlet> <hostname>` |

---

## Legal

This tool is intended exclusively for **authorized security assessments**.
Unauthorized use against systems you do not own or have explicit written permission to test is illegal.
