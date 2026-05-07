#!/usr/bin/env python3
"""
EvilGophish CSV → Excel statistics report — single merged campaign.

Usage
-----
  # Minimal — scan current directory, no exclusions
  python phishing_stats.py

  # Explicit CSV directory + output file
  python phishing_stats.py -d ./campaign_data -o rapport_Q1.xlsx

  # Exclude specific users by username (part before @)
  python phishing_stats.py --exclude soufiane.tahiri atia.nadhmi

  # Exclude users listed in a file (one email or username per line)
  python phishing_stats.py --exclude-file excluded.txt

  # Reported users file (default: reported_users.txt in CSV dir)
  python phishing_stats.py --reported reported_users.txt

  # Combine everything
  python phishing_stats.py -d ./data -o out.xlsx \\
      --exclude soufiane.tahiri --exclude-file more_exclusions.txt \\
      --reported reported_users.txt --timezone America/New_York

  # Pass CSV files directly (Events + Results pairs, space-separated)
  python phishing_stats.py --events camp1_Events.csv camp2_Events.csv \\
                           --results camp1_Results.csv camp2_Results.csv
"""

import os, re, json, sys, argparse
import pandas as pd
import xlsxwriter

# ── CLI ───────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="EvilGophish CSV → Excel phishing statistics report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Input sources
    src = p.add_argument_group("Input sources (mutually exclusive with -d)")
    src.add_argument("-d", "--directory", default=None,
                     help="Directory to scan for *Events.csv / *Results.csv pairs "
                          "(default: directory of this script)")
    src.add_argument("--events",  nargs="+", metavar="CSV",
                     help="Explicit Events CSV file(s)")
    src.add_argument("--results", nargs="+", metavar="CSV",
                     help="Explicit Results CSV file(s) — must match --events order")

    # Output
    p.add_argument("-o", "--output", default="rapport_phishing_stats.xlsx",
                   help="Output Excel file path (default: rapport_phishing_stats.xlsx)")

    # Exclusions
    excl = p.add_argument_group("User exclusions")
    excl.add_argument("--exclude", nargs="+", metavar="USERNAME",
                      default=[],
                      help="Usernames (part before @) to exclude, space-separated")
    excl.add_argument("--exclude-file", metavar="FILE",
                      help="File with one email/username per line to exclude")

    # Reported users
    p.add_argument("--reported", metavar="FILE", default=None,
                   help="Path to reported_users.txt "
                        "(default: auto-detected in CSV directory)")

    # Timezone
    p.add_argument("--timezone", default="Europe/Paris",
                   help="Timezone for time analysis (default: Europe/Paris)")

    args = p.parse_args()

    # Validate: --events and --results must come together and have same length
    if bool(args.events) != bool(args.results):
        p.error("--events and --results must be provided together")
    if args.events and args.results and len(args.events) != len(args.results):
        p.error("--events and --results must have the same number of files")
    if (args.events or args.results) and args.directory:
        p.error("--directory and --events/--results are mutually exclusive")

    return args


def build_excluded_set(args):
    """Build the set of excluded usernames from CLI flags + optional file."""
    excluded = set(u.strip().lower().split("@")[0] for u in args.exclude if u.strip())

    if args.exclude_file:
        path = args.exclude_file
        if not os.path.exists(path):
            print(f"[!] --exclude-file not found: {path}", file=sys.stderr)
            sys.exit(1)
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    excluded.add(line.lower().split("@")[0])

    return excluded


# ── Runtime config (populated by main before any function uses it) ─────────────
_CONFIG = {
    "excluded":  set(),   # set of lowercase usernames
    "timezone":  "Europe/Paris",
}

OUTPUT_FILE = "rapport_phishing_stats.xlsx"

EXCLUDED_USERNAMES = set()  # kept for backward-compat; overridden by CLI
SEG_CREDS = "Identifiants saisis"
SEG_CLICK = "Cliqué (sans creds)"
SEG_OPEN  = "Ouvert (sans clic)"
SEG_NONE  = "Aucune action"

BUCKET_ORDER = ["< 5 min","5–15 min","15–30 min","30–60 min",
                "1–2h","2h–24h","J+1 à J+3","J+4 et +"]
WEEKDAY_FR = {"Monday":"Lundi","Tuesday":"Mardi","Wednesday":"Mercredi",
              "Thursday":"Jeudi","Friday":"Vendredi","Saturday":"Samedi","Sunday":"Dimanche"}
WEEKDAY_ORDER = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]

# ── Palette 20-40 % accent — pastels lisibles, aucun fond foncé ────────────────
C_HDR_BG  = "#9DC3E6"   # ~40 % bleu Office (titres principaux)
C_HDR_FG  = "#1F3864"   # bleu nuit (texte)
C_SEC_BG  = "#BDD7EE"   # ~25 % bleu (sections)
C_SEC_FG  = "#2E4057"
C_SUB_BG  = "#DEEAF1"   # ~15 % bleu (sous-sections)
C_SUB_FG  = "#2E4057"
C_WHITE   = "#FFFFFF"
C_LIGHT   = "#F5F9FD"   # alternance très légère

C_SENT       = "#EEF4FB"
C_OPEN       = "#FFF9E6"
C_CLICK_ROW  = "#FFF3E0"
C_SUBMIT     = "#FFEBEE"
C_REPORT_ROW = "#E8F4FD"
C_NONE_ROW   = "#F5F5F5"

C_CHECK_BG  = "#E2EFDA"
C_CHECK_FG  = "#375623"
C_REPORT_BG = "#DEEAF1"
C_REPORT_FG = "#1F3864"
C_CRED_BG   = "#FCE4D6"
C_CRED_FG   = "#C00000"
C_EMPTY_FG  = "#AEAAAA"

SEG_COLORS = {
    SEG_CREDS: "#F4CCCC",
    SEG_CLICK: "#FCE5CD",
    SEG_OPEN:  "#D5E8D4",
    SEG_NONE:  "#D5D8DC",
}
SEG_FG = {
    SEG_CREDS: "#7B241C",
    SEG_CLICK: "#784212",
    SEG_OPEN:  "#1A5E1A",
    SEG_NONE:  "#4A4A4A",
}

BUCKET_BG = {
    "< 5 min":   "#C9EAC3", "5–15 min":  "#D5E8D4",
    "15–30 min": "#FFF2CC", "30–60 min": "#FFE6CC",
    "1–2h":      "#FCE5CD", "2h–24h":    "#FADADD",
    "J+1 à J+3": "#F4CCCC", "J+4 et +": "#E8BDBD",
}
BUCKET_FG = {
    "< 5 min":   "#1A5E1A", "5–15 min":  "#375623",
    "15–30 min": "#7F6000", "30–60 min": "#7F4600",
    "1–2h":      "#784212", "2h–24h":    "#7B3B3B",
    "J+1 à J+3": "#7B241C", "J+4 et +": "#6B1515",
}

REPORT_TYPES   = {"jira", "mail", "teams"}
REPORT_UNKNOWN = "autre"
TYPE_COLORS    = {"jira":"#AED6F1","mail":"#A9DFBF","teams":"#D2B4DE",REPORT_UNKNOWN:"#D5D8DC"}
TYPE_FG        = {"jira":"#1A5276","mail":"#145A32","teams":"#6C3483",REPORT_UNKNOWN:"#4A4A4A"}
TYPE_ICONS     = {"jira":"🔵 Jira","mail":"📧 Mail","teams":"💬 Teams",REPORT_UNKNOWN:"❓ Autre"}


# ── CSV loading ───────────────────────────────────────────────────────────────
def find_csv_pairs(directory):
    """Scan directory for *Events.csv / *Results.csv pairs."""
    events, results = {}, {}
    for fn in os.listdir(directory):
        fp = os.path.join(directory, fn)
        if fn.endswith("Events.csv"):
            events[fn.replace(" - Events.csv","").strip()] = fp
        elif fn.endswith("Results.csv"):
            results[fn.replace(" - Results.csv","").strip()] = fp
    return [(k, events[k], results[k]) for k in sorted(events) if k in results]


def pairs_from_explicit(event_files, result_files):
    """Build pairs from explicit --events / --results lists."""
    pairs = []
    for ev, res in zip(event_files, result_files):
        if not os.path.exists(ev):
            print(f"[!] Events file not found: {ev}", file=sys.stderr); sys.exit(1)
        if not os.path.exists(res):
            print(f"[!] Results file not found: {res}", file=sys.stderr); sys.exit(1)
        name = os.path.basename(ev).replace(" - Events.csv","").replace("Events.csv","").strip()
        pairs.append((name, ev, res))
    return pairs


def is_excluded(email):
    return str(email).split("@")[0].strip().lower() in _CONFIG["excluded"]


def load_reported_users(directory, explicit_path=None):
    """Load reported_users.txt.  explicit_path overrides auto-detection."""
    if explicit_path:
        if not os.path.exists(explicit_path):
            print(f"[!] --reported file not found: {explicit_path}", file=sys.stderr)
            sys.exit(1)
        candidates = [explicit_path]
    else:
        candidates = [
            os.path.join(directory, "reported_users.txt"),
            os.path.join(directory, "reported users.txt"),
        ]

    path = None
    for c in candidates:
        if os.path.exists(c):
            path = c
            break

    if path is None:
        return {}

    result = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            email = line.split()[0].lower()
            types = [t.lower() for t in re.findall(r'\*\*(\w+)\*\*', line)]
            types = [t if t in REPORT_TYPES else REPORT_UNKNOWN for t in types]
            if not types:
                types = [REPORT_UNKNOWN]
            if email not in result:
                result[email] = types
    return result


def load_all(pairs):
    ev_list, res_list = [], []
    for _, ev_path, res_path in pairs:
        ev_list.append(pd.read_csv(ev_path,  on_bad_lines="skip"))
        res_list.append(pd.read_csv(res_path, on_bad_lines="skip"))
    events  = pd.concat(ev_list,  ignore_index=True)
    results = pd.concat(res_list, ignore_index=True)
    events["time"] = pd.to_datetime(events["time"], utc=True, errors="coerce")
    events  = events[~events["email"].fillna("").apply(is_excluded)].copy()
    results = results[~results["email"].fillna("").apply(is_excluded)].copy()
    return events, results


def extract_creds(details_str):
    try:
        raw = str(details_str).replace('""', '"')
        m = re.search(r'\{.*\}', raw)
        if not m:
            return "", ""
        data = json.loads(m.group())
        p = data.get("payload", {})
        return p.get("Username",[""])[0], p.get("Password",[""])[0]
    except Exception:
        return "", ""


# ── Temporal analytics ────────────────────────────────────────────────────────
def build_temporal_stats(events, campaign_start):
    ev = events.copy()
    ev["time_local"] = ev["time"].dt.tz_convert(_CONFIG["timezone"])

    def bucket(m):
        if m <    5: return "< 5 min"
        if m <   15: return "5–15 min"
        if m <   30: return "15–30 min"
        if m <   60: return "30–60 min"
        if m <  120: return "1–2h"
        if m < 1440: return "2h–24h"
        if m < 4320: return "J+1 à J+3"
        return "J+4 et +"

    clicks = ev[ev["message"] == "Clicked Link"].copy()
    subs   = ev[ev["message"] == "Submitted Data"].copy()

    for df in (clicks, subs):
        df["minutes_after"] = (df["time"] - campaign_start).dt.total_seconds() / 60
        df["bucket"]    = df["minutes_after"].apply(bucket)
        df["weekday"]   = df["time_local"].dt.day_name()
        df["weekday_fr"]= df["weekday"].map(WEEKDAY_FR)
        df["hour"]      = df["time_local"].dt.hour
        df["is_weekend"]= df["time_local"].dt.dayofweek >= 5

    # Multi-clickers
    click_counts = clicks.groupby("email").size().reset_index(name="nb_clics")
    click_times  = clicks.groupby("email")["time_local"].apply(list).reset_index()
    click_times.columns = ["email","click_times"]
    multi = click_counts[click_counts["nb_clics"] > 1].merge(click_times, on="email")

    return {
        "clicks":          clicks,
        "subs":            subs,
        "click_buckets":   clicks["bucket"].value_counts(),
        "sub_buckets":     subs["bucket"].value_counts(),
        "weekday_counts":  clicks["weekday_fr"].value_counts(),
        "hour_counts":     clicks["hour"].value_counts().sort_index(),
        "weekend_clicks":  int(clicks["is_weekend"].sum()),
        "weekday_clicks":  int((~clicks["is_weekend"]).sum()),
        "multi":           multi,
        "median_reaction": clicks["minutes_after"].median(),
        "total_clicks":    len(clicks),
        "campaign_start":  campaign_start,
    }


# ── Victim table ──────────────────────────────────────────────────────────────
def build_victim_table(events, results, reported_set=None):
    if reported_set is None:
        reported_set = {}
    ev = events.copy()
    ev["email_lower"] = ev["email"].fillna("").str.strip().str.lower()

    opened_set  = set(ev[ev["message"]=="Email/SMS Opened"]["email_lower"])
    clicked_set = set(ev[ev["message"]=="Clicked Link"]["email_lower"])
    submit_set  = set(ev[ev["message"]=="Submitted Data"]["email_lower"])
    session_set = set(ev[ev["message"]=="Captured Session"]["email_lower"])

    click_cnt = ev[ev["message"]=="Clicked Link"].groupby("email_lower").size()

    creds = {}
    for _, row in ev[ev["message"]=="Submitted Data"].iterrows():
        e = row["email_lower"]
        if e and e not in creds:
            u, p = extract_creds(str(row.get("details","")))
            if p:
                creds[e] = (u, p)

    res = results.copy()
    res["email_lower"] = res["email"].fillna("").str.strip().str.lower()
    res_dedup = res.drop_duplicates(subset=["email_lower"], keep="first")

    rows = []
    for _, r in res_dedup.iterrows():
        email = r["email_lower"]
        if not email:
            continue
        opened    = email in opened_set
        clicked   = email in clicked_set
        submitted = email in submit_set
        captured  = email in session_set
        u, p      = creds.get(email, ("",""))
        seg = (SEG_CREDS if (captured or submitted)
               else SEG_CLICK if clicked
               else SEG_OPEN  if opened
               else SEG_NONE)
        rows.append({
            "Email":            r.get("email",""),
            "Prénom":           r.get("first_name",""),
            "Nom":              r.get("last_name",""),
            "Poste":            r.get("position",""),
            "Ouvert":           "✓" if opened    else "",
            "Cliqué":           "✓" if clicked   else "",
            "Identifiants":     "✓" if submitted else "",
            "Session":          "✓" if captured  else "",
            "Nb clics":         int(click_cnt.get(email, 0)),
            "Signalé":          "✓" if email in reported_set else "",
            "Type signalement": " / ".join(reported_set.get(email,[])),
            "Username":         u,
            "Mot de passe":     p,
            "Segment":          seg,
        })
    return pd.DataFrame(rows)


# ── Format cache ──────────────────────────────────────────────────────────────
class FmtCache:
    def __init__(self, wb):
        self._wb, self._c = wb, {}
    def __call__(self, **kw):
        key = str(sorted(kw.items()))
        if key not in self._c:
            self._c[key] = self._wb.add_format(kw)
        return self._c[key]


def auto_col_widths(ws, df, columns, min_w=8, max_w=45, pad=3):
    for i, col in enumerate(columns):
        hl = len(str(col))
        cl = int(df[col].fillna("").astype(str).str.len().max()) if col in df.columns else 0
        ws.set_column(i, i, min(max_w, max(min_w, max(hl, cl) + pad)))



# ── Shared style helpers ──────────────────────────────────────────────────────
def title_fmt(fmt, bg=None, fg=None):
    return fmt(bold=True, font_size=13, font_color=fg or C_HDR_FG,
               bg_color=bg or C_HDR_BG, align="center", valign="vcenter")

def section_fmt(fmt, bg=None, fg=None):
    return fmt(bold=True, font_size=10, font_color=fg or C_SEC_FG,
               bg_color=bg or C_SEC_BG, align="left", valign="vcenter", indent=1)

def hdr_l(fmt): return fmt(bold=True, font_color=C_HDR_FG, bg_color=C_HDR_BG,
                            align="left",   valign="vcenter", border=1, indent=1)
def hdr_c(fmt): return fmt(bold=True, font_color=C_HDR_FG, bg_color=C_HDR_BG,
                            align="center", valign="vcenter", border=1)


# ── Sheet: Résumé Global ──────────────────────────────────────────────────────
def write_resume(wb, victims, fmt, reported_dict=None, temporal=None):
    ws = wb.add_worksheet("Résumé Global")
    ws.set_zoom(90)
    ws.set_column(0, 0, 30)
    ws.set_column(1, 1, 12)
    ws.set_column(2, 2, 12)
    ws.set_column(3, 3, 28)

    if reported_dict is None: reported_dict = {}
    total     = len(victims)
    opened    = (victims["Ouvert"]       == "✓").sum()
    clicked   = (victims["Cliqué"]       == "✓").sum()
    submitted = (victims["Identifiants"] == "✓").sum()
    rep_count = len(reported_dict)

    def pct(n): return round(n / total * 100, 1) if total else 0.0

    ws.merge_range("A1:D1", "RAPPORT PHISHING — CAMPAGNE GLOBALE", title_fmt(fmt))
    ws.set_row(0, 30)

    row = 2
    ws.merge_range(row,0,row,3,"ENTONNOIR DE CAMPAGNE", section_fmt(fmt))
    ws.set_row(row, 20); row += 1

    funnel = [
        ("🎯  Cibles uniques",      total,     None,         C_SENT),
        ("📨  Emails ouverts",      opened,    pct(opened),  C_OPEN),
        ("🖱  Lien cliqué",         clicked,   pct(clicked), C_CLICK_ROW),
        ("🔑  Identifiants saisis", submitted, pct(submitted), C_SUBMIT),
        ("📣  Signalements",        rep_count, pct(rep_count), C_REPORT_ROW),
    ]
    ws.write(row,0,"Indicateur",hdr_l(fmt))
    ws.write(row,1,"Nombre",    hdr_c(fmt))
    ws.write(row,2,"% ciblés",  hdr_c(fmt))
    ws.write(row,3,"Tendance",  hdr_c(fmt))
    ws.set_row(row,18); row += 1

    for label, count, p, bg in funnel:
        fl = fmt(bold=True,bg_color=bg,font_color=C_HDR_FG,align="left",  valign="vcenter",border=1,indent=1)
        fn = fmt(bold=True,bg_color=bg,font_color=C_HDR_FG,align="center",valign="vcenter",border=1)
        fp = fmt(          bg_color=bg,font_color=C_HDR_FG,align="center",valign="vcenter",border=1,num_format='0.0"%"')
        fb = fmt(          bg_color=bg,font_color=C_SEC_BG, align="left", valign="vcenter",border=1)
        bar = "█" * int((p or 0) / 4) if p else ""
        ws.write(row,0,label,fl); ws.write(row,1,count,fn)
        ws.write(row,2,p if p is not None else "",fp if p is not None else fn)
        ws.write(row,3,bar,fb)
        ws.set_row(row,20); row += 1

    row += 1
    ws.merge_range(row,0,row,3,"SEGMENTATION COMPORTEMENTALE", section_fmt(fmt))
    ws.set_row(row,20); row += 1

    seg_counts = victims["Segment"].value_counts()
    seg_desc = {
        SEG_CREDS:("🔑  Identifiants saisis",    "Identifiants soumis (avec ou sans session)"),
        SEG_CLICK:("🖱  Cliqué sans identifiants","Clic sur le lien, aucun creds soumis"),
        SEG_OPEN: ("📨  Ouvert sans clic",        "Email ouvert, lien non cliqué"),
        SEG_NONE: ("📭  Aucune action",            "Aucune interaction détectée"),
    }
    ws.write(row,0,"Segment",    hdr_l(fmt))
    ws.write(row,1,"Nombre",     hdr_c(fmt))
    ws.write(row,2,"% ciblés",   hdr_c(fmt))
    ws.write(row,3,"Description",hdr_c(fmt))
    ws.set_row(row,18); row += 1

    for seg in [SEG_CREDS,SEG_CLICK,SEG_OPEN,SEG_NONE]:
        count = int(seg_counts.get(seg,0))
        p     = round(count/total*100,1) if total else 0.0
        bg, fg = SEG_COLORS[seg], SEG_FG[seg]
        lbl, desc = seg_desc[seg]
        ws.write(row,0,lbl, fmt(bold=True,font_color=fg,bg_color=bg,align="left",  valign="vcenter",border=1,indent=1))
        ws.write(row,1,count,fmt(bold=True,font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1))
        ws.write(row,2,p,    fmt(           font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1,num_format='0.0"%"'))
        ws.write(row,3,desc, fmt(italic=True,font_color=fg,bg_color=bg,align="left",valign="vcenter",border=1,indent=1,font_size=9))
        ws.set_row(row,20); row += 1

    row += 1

    # Signalements par canal
    if reported_dict:
        from collections import Counter
        tc = Counter(t for types in reported_dict.values() for t in types)
        all_types = [t for t in ["jira","mail","teams",REPORT_UNKNOWN] if tc.get(t,0)>0]
        ws.merge_range(row,0,row,3,"SIGNALEMENTS PAR CANAL", section_fmt(fmt))
        ws.set_row(row,20); row += 1
        ws.write(row,0,"Canal",         hdr_l(fmt))
        ws.write(row,1,"Nombre",        hdr_c(fmt))
        ws.write(row,2,"% signalements",hdr_c(fmt))
        ws.set_row(row,18); row += 1
        for rtype in all_types:
            n = tc[rtype]; p = round(n/rep_count*100,1) if rep_count else 0.0
            bg,fg = TYPE_COLORS[rtype], TYPE_FG[rtype]
            ws.write(row,0,TYPE_ICONS[rtype],fmt(bold=True,font_color=fg,bg_color=bg,align="left",  valign="vcenter",border=1,indent=1))
            ws.write(row,1,n,               fmt(bold=True,font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1))
            ws.write(row,2,p,               fmt(           font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1,num_format='0.0"%"'))
            ws.set_row(row,20); row += 1

    row += 1

    # Résumé temporel
    if temporal:
        multi_n = len(temporal["multi"])
        wknd_n  = temporal["weekend_clicks"]
        med_min = temporal["median_reaction"]
        if med_min < 60:
            med_str = f"{round(med_min,0):.0f} min"
        else:
            med_str = f"{round(med_min/60,1):.1f}h"

        ws.merge_range(row,0,row,3,"COMPORTEMENTS AVANCÉS", section_fmt(fmt))
        ws.set_row(row,20); row += 1
        ws.write(row,0,"Indicateur", hdr_l(fmt))
        ws.write(row,1,"Valeur",     hdr_c(fmt))
        ws.set_row(row,18); row += 1

        adv = [
            ("🔁  Multi-cliqueurs (≥2 clics)",         str(multi_n)),
            ("🌙  Clics hors horaires (weekend)",       str(wknd_n)),
            ("⏱  Délai médian 1er clic après envoi",   med_str),
        ]
        for i,(lbl,val) in enumerate(adv):
            bg = C_LIGHT if i%2 else C_WHITE
            ws.write(row,0,lbl,fmt(bg_color=bg,font_color=C_SEC_FG,align="left",  valign="vcenter",border=1,indent=1))
            ws.write(row,1,val,fmt(bold=True,bg_color=bg,font_color=C_HDR_FG,align="center",valign="vcenter",border=1))
            ws.merge_range(row,2,row,3,"",fmt(bg_color=bg,border=1))
            ws.set_row(row,20); row += 1

    # ── Chart hidden data (col F+) ────────────────────────────────────────────
    dc = 5  # data col start

    # Funnel data
    f_labels = ["Cibles","Ouverts","Cliqués","Identifiants","Signalements"]
    f_vals   = [total, int(opened), int(clicked), int(submitted), rep_count]
    for i,(l,v) in enumerate(zip(f_labels,f_vals)):
        ws.write(1+i,dc,l,fmt(font_color=C_WHITE)); ws.write(1+i,dc+1,v,fmt(font_color=C_WHITE))

    # Segmentation data
    sc = dc+3
    for i,seg in enumerate([SEG_CREDS,SEG_CLICK,SEG_OPEN,SEG_NONE]):
        ws.write(1+i,sc,seg,fmt(font_color=C_WHITE))
        ws.write(1+i,sc+1,int(seg_counts.get(seg,0)),fmt(font_color=C_WHITE))

    # Canal data
    cc = sc+3
    canal_n = 0
    if reported_dict:
        for i,rtype in enumerate([t for t in ["jira","mail","teams",REPORT_UNKNOWN] if tc.get(t,0)>0]):
            ws.write(1+i,cc,TYPE_ICONS[rtype],fmt(font_color=C_WHITE))
            ws.write(1+i,cc+1,tc[rtype],fmt(font_color=C_WHITE))
            canal_n += 1

    # Behavior (signaleurs) data
    bc = cc+3
    if temporal and reported_dict:
        rep_v = victims[victims["Signalé"]=="✓"].copy()
        def rcat(r):
            if r["Identifiants"]=="✓" or r["Session"]=="✓": return "Après creds"
            if r["Cliqué"]=="✓": return "Après clic"
            return "Sans clic"
        rep_v["Cat"] = rep_v.apply(rcat,axis=1)
        bcats = ["Sans clic","Après clic","Après creds"]
        bcols = ["#A9DFBF","#FAD7A0","#F4CCCC"]
        bc_counts = rep_v["Cat"].value_counts()
        for i,cat in enumerate(bcats):
            ws.write(1+i,bc,cat,fmt(font_color=C_WHITE))
            ws.write(1+i,bc+1,int(bc_counts.get(cat,0)),fmt(font_color=C_WHITE))
    else:
        bcats=[]; bcols=[]

    # Reaction time pie data (for write_resume chart)
    rc = bc+3
    if temporal:
        buck_data = [(b,int(temporal["click_buckets"].get(b,0))) for b in BUCKET_ORDER if temporal["click_buckets"].get(b,0)>0]
        for i,(b,v) in enumerate(buck_data):
            ws.write(1+i,rc,b,fmt(font_color=C_WHITE)); ws.write(1+i,rc+1,v,fmt(font_color=C_WHITE))
    else:
        buck_data=[]

    # ── Charts ────────────────────────────────────────────────────────────────
    cw,ch = 400,260

    # 1. Funnel bar
    bar = wb.add_chart({"type":"bar"})
    bar.add_series({"name":"Entonnoir","categories":["Résumé Global",1,dc,5,dc],
                    "values":["Résumé Global",1,dc+1,5,dc+1],
                    "fill":{"color":C_HDR_BG},
                    "data_labels":{"value":True,"font":{"bold":True,"color":C_HDR_FG}}})
    bar.set_title({"name":"Entonnoir global"}); bar.set_legend({"none":True})
    bar.set_style(7); bar.set_size({"width":cw,"height":ch})
    ws.insert_chart(2,6,bar)

    # 2. Segmentation pie
    sp = wb.add_chart({"type":"pie"})
    sp.add_series({"name":"Segmentation","categories":["Résumé Global",1,sc,4,sc],
                   "values":["Résumé Global",1,sc+1,4,sc+1],
                   "data_labels":{"percentage":True,"category":True,"separator":"\n","font":{"size":8}},
                   "points":[{"fill":{"color":SEG_COLORS[s]}} for s in [SEG_CREDS,SEG_CLICK,SEG_OPEN,SEG_NONE]]})
    sp.set_title({"name":"Répartition comportementale"}); sp.set_legend({"position":"bottom"})
    sp.set_style(7); sp.set_size({"width":cw,"height":ch})
    ws.insert_chart(2,13,sp)

    # 3. Canal pie
    if canal_n > 0:
        cp = wb.add_chart({"type":"pie"})
        cp.add_series({"name":"Par canal","categories":["Résumé Global",1,cc,canal_n,cc],
                       "values":["Résumé Global",1,cc+1,canal_n,cc+1],
                       "data_labels":{"percentage":True,"category":True,"separator":"\n","font":{"size":8}},
                       "points":[{"fill":{"color":TYPE_COLORS[t]}} for t in [t for t in ["jira","mail","teams",REPORT_UNKNOWN] if tc.get(t,0)>0]]})
        cp.set_title({"name":"Signalements par canal"}); cp.set_legend({"position":"bottom"})
        cp.set_style(7); cp.set_size({"width":cw,"height":ch})
        ws.insert_chart(19,6,cp)

    # 4. Comportement signaleurs pie
    if bcats:
        bp = wb.add_chart({"type":"pie"})
        bp.add_series({"name":"Comportement","categories":["Résumé Global",1,bc,3,bc],
                       "values":["Résumé Global",1,bc+1,3,bc+1],
                       "data_labels":{"percentage":True,"category":True,"separator":"\n","font":{"size":8}},
                       "points":[{"fill":{"color":c}} for c in bcols]})
        bp.set_title({"name":"Comportement des signaleurs"}); bp.set_legend({"position":"bottom"})
        bp.set_style(7); bp.set_size({"width":cw,"height":ch})
        ws.insert_chart(19,13,bp)

    # 5. Reaction time pie (new)
    if buck_data:
        rp = wb.add_chart({"type":"pie"})
        rp.add_series({"name":"Délai","categories":["Résumé Global",1,rc,len(buck_data),rc],
                       "values":["Résumé Global",1,rc+1,len(buck_data),rc+1],
                       "data_labels":{"percentage":True,"category":True,"separator":"\n","font":{"size":8}},
                       "points":[{"fill":{"color":BUCKET_BG[b]}} for b,_ in buck_data]})
        rp.set_title({"name":"Délai de réaction — 1er clic"}); rp.set_legend({"none":True})
        rp.set_style(7); rp.set_size({"width":cw,"height":ch})
        ws.insert_chart(36,6,rp)


# ── Sheet: detail list ────────────────────────────────────────────────────────
def write_detail_sheet(wb, victims, fmt, sheet_name, segments, title, include_password=True):
    ws = wb.add_worksheet(sheet_name)
    ws.set_zoom(90)
    seg_color = SEG_COLORS.get(segments[0], C_SEC_BG) if segments else C_SEC_BG
    seg_fg    = SEG_FG.get(segments[0], C_SEC_FG) if segments else C_SEC_FG
    subset = victims[victims["Segment"].isin(segments)].copy()

    creds_only = include_password and set(segments) <= {SEG_CREDS}
    if creds_only:
        columns = ["Mot de passe"]
    elif include_password and SEG_CREDS in segments:
        columns = ["Email","Prénom","Nom","Poste","Ouvert","Cliqué","Identifiants","Session","Nb clics","Mot de passe","Segment"]
    elif set(segments) <= {SEG_CLICK,SEG_OPEN,SEG_NONE}:
        columns = ["Email","Prénom","Nom","Poste","Ouvert","Cliqué","Nb clics","Signalé","Segment"]
    else:
        columns = ["Email","Prénom","Nom","Poste","Ouvert","Cliqué","Identifiants","Session","Nb clics","Signalé","Segment"]

    n = len(columns)
    tf = fmt(bold=True,font_size=12,font_color=seg_fg,bg_color=seg_color,align="center",valign="vcenter")
    if n>1: ws.merge_range(0,0,0,n-1,title,tf)
    else:   ws.write(0,0,title,tf)
    ws.set_row(0,26)

    auto_col_widths(ws, subset, columns)

    hdr = fmt(bold=True,font_color=C_HDR_FG,bg_color=C_HDR_BG,align="center",valign="vcenter",border=1,text_wrap=True)
    for ci,col in enumerate(columns):
        ws.write(1,ci,col,hdr)
    ws.set_row(1,20)

    check_cols  = {"Ouvert","Cliqué","Identifiants","Session"}
    report_cols = {"Signalé"}
    cred_cols   = {"Mot de passe"}

    for ri,(_, row) in enumerate(subset.iterrows()):
        er = 2+ri
        seg    = row.get("Segment",SEG_NONE)
        row_bg = C_LIGHT if ri%2 else C_WHITE

        for ci,col in enumerate(columns):
            val = row.get(col,"")
            val = "" if pd.isna(val) else str(val)

            if col in check_cols:
                f = (fmt(align="center",valign="vcenter",border=1,font_color=C_CHECK_FG,bg_color=C_CHECK_BG,bold=True)
                     if val=="✓" else
                     fmt(align="center",valign="vcenter",border=1,bg_color=row_bg))
            elif col in report_cols:
                f = (fmt(align="center",valign="vcenter",border=1,font_color=C_REPORT_FG,bg_color=C_REPORT_BG,bold=True)
                     if val=="✓" else
                     fmt(align="center",valign="vcenter",border=1,bg_color=row_bg))
            elif col in cred_cols:
                f = fmt(align="left",valign="vcenter",border=1,
                        bg_color=C_CRED_BG if val else row_bg,
                        bold=bool(val),font_color=C_CRED_FG if val else C_EMPTY_FG,
                        font_name="Courier New" if val else "Calibri")
            elif col == "Nb clics":
                is_multi = str(val).isdigit() and int(val) > 1
                f = fmt(align="center",valign="vcenter",border=1,
                        bg_color="#FCE5CD" if is_multi else row_bg,
                        font_color="#784212" if is_multi else C_SEC_FG,
                        bold=is_multi)
            elif col == "Segment":
                bg,fg = SEG_COLORS.get(seg,C_SEC_BG), SEG_FG.get(seg,C_SEC_FG)
                f = fmt(align="center",valign="vcenter",border=1,bg_color=bg,font_color=fg,bold=True,font_size=9)
            else:
                f = fmt(align="left",valign="vcenter",border=1,bg_color=row_bg,font_color=C_SEC_FG)

            ws.write(er,ci,val,f)
        ws.set_row(er,17)

    fr = 2+len(subset)
    ff = fmt(bold=True,font_color=seg_fg,bg_color=seg_color,align="right",valign="vcenter",indent=1)
    text = f"Total : {len(subset)} cible(s)"
    if n>1: ws.merge_range(fr,0,fr,n-1,text,ff)
    else:   ws.write(fr,0,text,ff)
    ws.set_row(fr,18)


def write_all_sheet(wb, victims, fmt):
    write_detail_sheet(wb,victims,fmt,
                       sheet_name="Toutes les cibles",
                       segments=list(SEG_COLORS.keys()),
                       title="Toutes les cibles — Vue complète",
                       include_password=False)


# ── Sheet: Signalements ───────────────────────────────────────────────────────
def write_reported_sheet(wb, victims, fmt):
    from collections import Counter
    ws = wb.add_worksheet("Signalements")
    ws.set_zoom(90)

    C_REP_BG = "#BDD7EE"
    CAT_COLORS = {"Signalé sans clic":"#D5E8D4","Signalé après clic":"#FFE6CC","Signalé après creds":"#F4CCCC"}
    CAT_FG     = {"Signalé sans clic":"#1A5E1A","Signalé après clic":"#784212","Signalé après creds":"#7B241C"}
    CAT_ICONS  = {"Signalé sans clic":"✅ Sans clic","Signalé après clic":"⚠️  Après clic","Signalé après creds":"🔑 Après creds"}

    n_cols = 10
    ws.merge_range(0,0,0,n_cols-1,"Signalements — Utilisateurs ayant signalé l'email",
                   fmt(bold=True,font_size=12,font_color=C_HDR_FG,bg_color=C_REP_BG,align="center",valign="vcenter"))
    ws.set_row(0,26)

    rep = victims[victims["Signalé"]=="✓"].copy()
    total_rep = len(rep)

    def rcat(r):
        if r["Identifiants"]=="✓" or r["Session"]=="✓": return "Signalé après creds"
        if r["Cliqué"]=="✓": return "Signalé après clic"
        return "Signalé sans clic"
    rep["Catégorie"] = rep.apply(rcat,axis=1)

    def sec(label,row):
        ws.merge_range(row,0,row,n_cols-1,label,
                       fmt(bold=True,font_size=10,font_color=C_SEC_FG,bg_color=C_SUB_BG,
                           align="left",valign="vcenter",indent=1))
        ws.set_row(row,20); return row+1

    def tot_row(row,n):
        ws.write(row,0,"TOTAL",fmt(bold=True,font_color=C_HDR_FG,bg_color=C_HDR_BG,align="left",valign="vcenter",border=1,indent=1))
        ws.write(row,1,n,     fmt(bold=True,font_color=C_HDR_FG,bg_color=C_HDR_BG,align="center",valign="vcenter",border=1))
        ws.set_row(row,18); return row+2

    row=2; row=sec("STATISTIQUES PAR COMPORTEMENT",row)
    ws.write(row,0,"Catégorie",     hdr_l(fmt))
    ws.write(row,1,"Nombre",        hdr_c(fmt))
    ws.write(row,2,"% signalements",hdr_c(fmt))
    ws.write(row,3,"Description",   hdr_c(fmt))
    ws.set_row(row,18); row+=1

    for cat,desc in [("Signalé sans clic","N'a pas cliqué — bonne réaction"),
                     ("Signalé après clic","A cliqué le lien avant de signaler"),
                     ("Signalé après creds","A soumis ses identifiants avant de signaler")]:
        n=int((rep["Catégorie"]==cat).sum()); p=round(n/total_rep*100,1) if total_rep else 0.0
        bg,fg = CAT_COLORS[cat],CAT_FG[cat]
        ws.write(row,0,CAT_ICONS[cat],fmt(bold=True,font_color=fg,bg_color=bg,border=1,align="left",valign="vcenter",indent=1))
        ws.write(row,1,n,             fmt(bold=True,font_color=fg,bg_color=bg,border=1,align="center",valign="vcenter"))
        ws.write(row,2,p,             fmt(           font_color=fg,bg_color=bg,border=1,align="center",valign="vcenter",num_format='0.0"%"'))
        ws.write(row,3,desc,          fmt(italic=True,font_color=fg,bg_color=bg,border=1,align="left",valign="vcenter",indent=1))
        ws.set_row(row,20); row+=1
    row=tot_row(row,total_rep)

    row=sec("STATISTIQUES PAR CANAL",row)
    ws.write(row,0,"Canal",         hdr_l(fmt))
    ws.write(row,1,"Nombre",        hdr_c(fmt))
    ws.write(row,2,"% signalements",hdr_c(fmt))
    ws.set_row(row,18); row+=1

    tc=Counter()
    for val in rep["Type signalement"].fillna(""):
        for t in str(val).split(" / "):
            t=t.strip().lower()
            if t: tc[t]+=1

    for rtype in [t for t in ["jira","mail","teams",REPORT_UNKNOWN] if tc.get(t,0)]:
        n=tc[rtype]; p=round(n/total_rep*100,1) if total_rep else 0.0
        bg,fg = TYPE_COLORS[rtype],TYPE_FG[rtype]
        ws.write(row,0,TYPE_ICONS[rtype],fmt(bold=True,font_color=fg,bg_color=bg,border=1,align="left",valign="vcenter",indent=1))
        ws.write(row,1,n,               fmt(bold=True,font_color=fg,bg_color=bg,border=1,align="center",valign="vcenter"))
        ws.write(row,2,p,               fmt(           font_color=fg,bg_color=bg,border=1,align="center",valign="vcenter",num_format='0.0"%"'))
        ws.set_row(row,20); row+=1
    row=tot_row(row,total_rep)

    row=sec("DÉTAIL DES SIGNALEURS",row)
    cols=["Email","Prénom","Nom","Poste","Ouvert","Cliqué","Identifiants","Session","Catégorie","Type signalement"]
    ws.set_column(0,0,30); ws.set_column(1,1,14); ws.set_column(2,2,16)
    ws.set_column(3,3,28); [ws.set_column(i,i,9) for i in range(4,8)]
    ws.set_column(8,8,22); ws.set_column(9,9,18)

    hdr=fmt(bold=True,font_color=C_HDR_FG,bg_color=C_HDR_BG,align="center",valign="vcenter",border=1,text_wrap=True)
    for ci,col in enumerate(cols): ws.write(row,ci,col,hdr)
    ws.set_row(row,20); row+=1

    check_set={"Ouvert","Cliqué","Identifiants","Session"}
    cat_ord=["Signalé sans clic","Signalé après clic","Signalé après creds"]
    rep_sorted=rep.sort_values("Catégorie",key=lambda s:s.map({c:i for i,c in enumerate(cat_ord)}))

    for ri,(_,r) in enumerate(rep_sorted.iterrows()):
        cat=r.get("Catégorie","")
        rtype_raw=str(r.get("Type signalement","") or "")
        rtypes=[t.strip().lower() for t in rtype_raw.split(" / ") if t.strip()]
        cat_bg,cat_fg=CAT_COLORS.get(cat,C_LIGHT),CAT_FG.get(cat,C_SEC_FG)
        type_bg=(TYPE_COLORS.get(rtypes[0],"#D5D8DC") if len(rtypes)==1 else C_HDR_BG)
        type_fg=(TYPE_FG.get(rtypes[0],C_SEC_FG)      if len(rtypes)==1 else C_HDR_FG)
        row_bg=C_LIGHT if ri%2 else C_WHITE

        for ci,col in enumerate(cols):
            val=str(r.get(col,"") or "")
            if col in check_set:
                f=(fmt(align="center",valign="vcenter",border=1,font_color=C_CHECK_FG,bg_color=C_CHECK_BG,bold=True)
                   if val=="✓" else fmt(align="center",valign="vcenter",border=1,bg_color=row_bg))
            elif col=="Catégorie":
                f=fmt(align="center",valign="vcenter",border=1,bg_color=cat_bg,font_color=cat_fg,bold=True,font_size=9)
            elif col=="Type signalement":
                val=" + ".join(TYPE_ICONS.get(t,t) for t in rtypes) if rtypes else rtype_raw
                f=fmt(align="center",valign="vcenter",border=1,bg_color=type_bg,font_color=type_fg,bold=True,font_size=9)
            else:
                f=fmt(align="left",valign="vcenter",border=1,bg_color=row_bg,font_color=C_SEC_FG)
            ws.write(row,ci,val,f)
        ws.set_row(row,17); row+=1

    ws.merge_range(row,0,row,n_cols-1,f"Total signaleurs : {total_rep}",
                   fmt(bold=True,font_color=C_HDR_FG,bg_color=C_REP_BG,align="right",valign="vcenter",indent=1))
    ws.set_row(row,18)


# ── Sheet: Analyse Temporelle ─────────────────────────────────────────────────
def write_temporal_sheet(wb, temporal, victims, fmt):
    ws = wb.add_worksheet("Analyse Temporelle")
    ws.set_zoom(90)
    ws.set_column(0,0,18); ws.set_column(1,4,11)

    clicks = temporal["clicks"]
    subs   = temporal["subs"]
    total_clicks = temporal["total_clicks"]
    total_subs   = len(subs)

    ws.merge_range(0,0,0,9,"ANALYSE TEMPORELLE — COMPORTEMENTS AVANCÉS",
                   title_fmt(fmt))
    ws.set_row(0,30)

    row = 2
    # ── Section A: Délai de réaction ─────────────────────────────────────────
    ws.merge_range(row,0,row,4,"DÉLAI DE RÉACTION (depuis l'envoi)", section_fmt(fmt))
    ws.set_row(row,20); row+=1

    ws.write(row,0,"Fenêtre",         hdr_l(fmt))
    ws.write(row,1,"Clics",           hdr_c(fmt))
    ws.write(row,2,"% clics",         hdr_c(fmt))
    ws.write(row,3,"Creds saisis",    hdr_c(fmt))
    ws.write(row,4,"% creds",         hdr_c(fmt))
    ws.set_row(row,18); row+=1

    buck_data_rows = []   # (bucket, n_clicks, n_subs) for chart data
    for b in BUCKET_ORDER:
        nc = int(temporal["click_buckets"].get(b,0))
        ns = int(temporal["sub_buckets"].get(b,0))
        if nc==0 and ns==0: continue
        bg,fg = BUCKET_BG[b],BUCKET_FG[b]
        pc = round(nc/total_clicks*100,1) if total_clicks else 0.0
        ps = round(ns/total_subs*100,1)   if total_subs   else 0.0
        ws.write(row,0,b, fmt(bold=True,font_color=fg,bg_color=bg,align="left",  valign="vcenter",border=1,indent=1))
        ws.write(row,1,nc,fmt(bold=True,font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1))
        ws.write(row,2,pc,fmt(           font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1,num_format='0.0"%"'))
        ws.write(row,3,ns,fmt(bold=True,font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1))
        ws.write(row,4,ps,fmt(           font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1,num_format='0.0"%"'))
        ws.set_row(row,20); row+=1
        buck_data_rows.append((b,nc,ns))

    # Total
    ws.write(row,0,"TOTAL",        hdr_l(fmt))
    ws.write(row,1,total_clicks,   hdr_c(fmt))
    ws.write(row,2,"",             hdr_c(fmt))
    ws.write(row,3,total_subs,     hdr_c(fmt))
    ws.write(row,4,"",             hdr_c(fmt))
    ws.set_row(row,18); row+=2

    # ── Section B: Distribution horaire ──────────────────────────────────────
    ws.merge_range(row,0,row,4,"DISTRIBUTION HORAIRE (heure de Paris)", section_fmt(fmt))
    ws.set_row(row,20); row+=1

    ws.write(row,0,"Heure",hdr_l(fmt)); ws.write(row,1,"Clics",hdr_c(fmt))
    ws.write(row,2,"% clics",hdr_c(fmt))
    ws.set_row(row,18); row+=1

    hour_data=[]
    for hr in range(24):
        n=int(temporal["hour_counts"].get(hr,0))
        if n==0: continue
        p=round(n/total_clicks*100,1) if total_clicks else 0.0
        is_night = hr<7 or hr>=20
        bg = "#DEEAF1" if not is_night else "#FCE5CD"
        fg = C_SEC_FG
        ws.write(row,0,f"{hr:02d}h–{hr+1:02d}h",fmt(font_color=fg,bg_color=bg,align="left",  valign="vcenter",border=1,indent=1))
        ws.write(row,1,n,                        fmt(bold=True,font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1))
        ws.write(row,2,p,                        fmt(font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1,num_format='0.0"%"'))
        ws.set_row(row,18); row+=1
        hour_data.append((hr,n))
    row+=1

    # ── Section C: Distribution par jour ─────────────────────────────────────
    ws.merge_range(row,0,row,4,"DISTRIBUTION PAR JOUR", section_fmt(fmt))
    ws.set_row(row,20); row+=1

    ws.write(row,0,"Jour",   hdr_l(fmt)); ws.write(row,1,"Clics",hdr_c(fmt))
    ws.write(row,2,"% clics",hdr_c(fmt)); ws.write(row,3,"Week-end?",hdr_c(fmt))
    ws.set_row(row,18); row+=1

    day_data=[]
    for day_fr in WEEKDAY_ORDER:
        n=int(temporal["weekday_counts"].get(day_fr,0))
        if n==0: continue
        is_we = day_fr in ("Samedi","Dimanche")
        p=round(n/total_clicks*100,1) if total_clicks else 0.0
        bg = "#FCE5CD" if is_we else C_LIGHT
        fg = C_SEC_FG
        ws.write(row,0,day_fr,fmt(font_color=fg,bg_color=bg,align="left",  valign="vcenter",border=1,indent=1))
        ws.write(row,1,n,     fmt(bold=True,font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1))
        ws.write(row,2,p,     fmt(font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1,num_format='0.0"%"'))
        ws.write(row,3,"🌙 Oui" if is_we else "",
                              fmt(font_color="#784212" if is_we else C_EMPTY_FG,bg_color=bg,align="center",valign="vcenter",border=1))
        ws.set_row(row,18); row+=1
        day_data.append((day_fr,n,is_we))
    row+=1

    # ── Section D: Weekend vs semaine ─────────────────────────────────────────
    ws.merge_range(row,0,row,4,"WEEKEND vs SEMAINE", section_fmt(fmt))
    ws.set_row(row,20); row+=1

    wknd = temporal["weekend_clicks"]
    wkdy = temporal["weekday_clicks"]
    tot  = wknd+wkdy

    ws.write(row,0,"Période",hdr_l(fmt)); ws.write(row,1,"Clics",hdr_c(fmt))
    ws.write(row,2,"% clics",hdr_c(fmt))
    ws.set_row(row,18); row+=1

    for label,n,bg,fg in [
        ("📅 Semaine",wkdy,C_LIGHT,C_SEC_FG),
        ("🌙 Weekend",wknd,"#FCE5CD","#784212"),
        ("Total",    tot, C_HDR_BG, C_HDR_FG),
    ]:
        p=round(n/tot*100,1) if tot else 0.0
        ws.write(row,0,label,fmt(bold=True,font_color=fg,bg_color=bg,align="left",  valign="vcenter",border=1,indent=1))
        ws.write(row,1,n,    fmt(bold=True,font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1))
        ws.write(row,2,p,    fmt(           font_color=fg,bg_color=bg,align="center",valign="vcenter",border=1,num_format='0.0"%"'))
        ws.set_row(row,18); row+=1
    row+=1

    # ── Section E: Multi-cliqueurs ────────────────────────────────────────────
    ws.merge_range(row,0,row,4,"MULTI-CLIQUEURS (≥ 2 clics)", section_fmt(fmt))
    ws.set_row(row,20); row+=1

    multi = temporal["multi"]
    if len(multi) == 0:
        ws.write(row,0,"Aucun multi-cliqueur détecté.",
                 fmt(italic=True,font_color=C_EMPTY_FG,bg_color=C_WHITE,border=1))
        ws.set_row(row,18); row+=1
    else:
        # Join with victims for name/position
        vmap = {str(r["Email"]).lower(): r for _,r in victims.iterrows()}

        multi_cols=["Email","Prénom","Nom","Poste","Nb clics","Horodatages","Segment"]
        ws.set_column(5,5,38)  # horodatages column wider
        for ci,col in enumerate(multi_cols):
            ws.write(row,ci,col,hdr_c(fmt))
        ws.set_row(row,18); row+=1

        for ri,(_,mrow) in enumerate(multi.sort_values("nb_clics",ascending=False).iterrows()):
            email  = str(mrow["email"])
            nb     = int(mrow["nb_clics"])
            times  = mrow["click_times"]
            vdata  = vmap.get(email.lower(), {})
            seg    = vdata.get("Segment", SEG_NONE) if isinstance(vdata,dict) else vdata.get("Segment",SEG_NONE)
            bg     = "#FCE5CD" if nb==2 else "#F4CCCC" if nb>=3 else C_LIGHT
            fg     = "#784212" if nb==2 else "#7B241C" if nb>=3 else C_SEC_FG
            times_str = "  |  ".join(t.strftime("%d/%m %H:%M") for t in times)

            row_vals = [
                email,
                vdata.get("Prénom","") if not isinstance(vdata,dict) else vdata.get("Prénom",""),
                vdata.get("Nom","")    if not isinstance(vdata,dict) else vdata.get("Nom",""),
                vdata.get("Poste","")  if not isinstance(vdata,dict) else vdata.get("Poste",""),
                nb,
                times_str,
                str(seg),
            ]
            for ci,val in enumerate(row_vals):
                is_seg = ci==6
                is_nb  = ci==4
                cell_bg = (SEG_COLORS.get(seg,C_LIGHT) if is_seg else
                           bg)
                cell_fg = (SEG_FG.get(seg,C_SEC_FG) if is_seg else fg)
                f=fmt(bold=is_nb or is_seg, font_color=cell_fg,bg_color=cell_bg,
                      align="center" if (is_nb or is_seg) else "left",
                      valign="vcenter",border=1,
                      indent=0 if (is_nb or is_seg) else 1)
                ws.write(row,ci,val,f)
            ws.set_row(row,17); row+=1

    # ── Chart hidden data (col G+) ────────────────────────────────────────────
    dc=7  # hidden data start col

    # Bucket bar data
    for i,(b,nc,ns) in enumerate(buck_data_rows):
        ws.write(1+i,dc,  b,  fmt(font_color=C_WHITE))
        ws.write(1+i,dc+1,nc, fmt(font_color=C_WHITE))
        ws.write(1+i,dc+2,ns, fmt(font_color=C_WHITE))
    nb_buck = len(buck_data_rows)

    # Hour bar data
    hdc = dc+4
    for i,(hr,n) in enumerate(hour_data):
        ws.write(1+i,hdc,  f"{hr:02d}h",fmt(font_color=C_WHITE))
        ws.write(1+i,hdc+1,n,           fmt(font_color=C_WHITE))
    nb_hr = len(hour_data)

    # Day pie data
    ddc = hdc+3
    for i,(day,n,_) in enumerate(day_data):
        ws.write(1+i,ddc,  day,fmt(font_color=C_WHITE))
        ws.write(1+i,ddc+1,n,  fmt(font_color=C_WHITE))
    nb_day = len(day_data)

    # Weekend pie data
    wdc = ddc+3
    ws.write(1,wdc,  "Semaine",fmt(font_color=C_WHITE)); ws.write(1,wdc+1,wkdy,fmt(font_color=C_WHITE))
    ws.write(2,wdc,  "Weekend", fmt(font_color=C_WHITE)); ws.write(2,wdc+1,wknd,fmt(font_color=C_WHITE))

    cw,ch = 400,260

    # Chart 1: Délai réaction — grouped bar (clics + creds)
    if nb_buck > 0:
        b1=wb.add_chart({"type":"bar"})
        b1.add_series({"name":"Clics",
                       "categories":["Analyse Temporelle",1,dc,nb_buck,dc],
                       "values":    ["Analyse Temporelle",1,dc+1,nb_buck,dc+1],
                       "fill":{"color":C_HDR_BG},
                       "data_labels":{"value":True,"font":{"size":8,"color":C_HDR_FG}}})
        b1.add_series({"name":"Creds saisis",
                       "categories":["Analyse Temporelle",1,dc,nb_buck,dc],
                       "values":    ["Analyse Temporelle",1,dc+2,nb_buck,dc+2],
                       "fill":{"color":"#F4CCCC"},
                       "data_labels":{"value":True,"font":{"size":8,"color":"#7B241C"}}})
        b1.set_title({"name":"Délai de réaction — Clics & Credentials"})
        b1.set_legend({"position":"bottom"}); b1.set_style(7)
        b1.set_size({"width":cw,"height":ch})
        ws.insert_chart(2,15,b1)

    # Chart 2: Distribution horaire bar
    if nb_hr > 0:
        b2=wb.add_chart({"type":"column"})
        b2.add_series({"name":"Clics par heure",
                       "categories":["Analyse Temporelle",1,hdc,nb_hr,hdc],
                       "values":    ["Analyse Temporelle",1,hdc+1,nb_hr,hdc+1],
                       "fill":{"color":C_SEC_BG},
                       "data_labels":{"value":True,"font":{"size":8,"color":C_SEC_FG}}})
        b2.set_title({"name":"Distribution horaire des clics (heure Paris)"})
        b2.set_legend({"none":True}); b2.set_style(7)
        b2.set_size({"width":cw,"height":ch})
        ws.insert_chart(19,15,b2)

    # Chart 3: Jour de semaine pie
    if nb_day > 0:
        day_colors={"Lundi":"#BDD7EE","Mardi":"#BDD7EE","Mercredi":"#BDD7EE",
                    "Jeudi":"#BDD7EE","Vendredi":"#DEEAF1","Samedi":"#FCE5CD","Dimanche":"#F4CCCC"}
        p3=wb.add_chart({"type":"pie"})
        p3.add_series({"name":"Par jour",
                       "categories":["Analyse Temporelle",1,ddc,nb_day,ddc],
                       "values":    ["Analyse Temporelle",1,ddc+1,nb_day,ddc+1],
                       "data_labels":{"percentage":True,"category":True,"separator":"\n","font":{"size":8}},
                       "points":[{"fill":{"color":day_colors.get(d,C_LIGHT)}} for d,_,_ in day_data]})
        p3.set_title({"name":"Clics par jour de la semaine"})
        p3.set_legend({"none":True}); p3.set_style(7)
        p3.set_size({"width":cw,"height":ch})
        ws.insert_chart(36,15,p3)

    # Chart 4: Weekend pie
    p4=wb.add_chart({"type":"pie"})
    p4.add_series({"name":"Weekend vs Semaine",
                   "categories":["Analyse Temporelle",1,wdc,2,wdc],
                   "values":    ["Analyse Temporelle",1,wdc+1,2,wdc+1],
                   "data_labels":{"percentage":True,"category":True,"separator":"\n","font":{"size":9}},
                   "points":[{"fill":{"color":C_CHECK_BG}},{"fill":{"color":"#FCE5CD"}}]})
    p4.set_title({"name":"Weekend vs Semaine"})
    p4.set_legend({"none":True}); p4.set_style(7)
    p4.set_size({"width":cw,"height":ch})
    ws.insert_chart(53,15,p4)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    args = parse_args()

    # ── 1. Populate runtime config ────────────────────────────────────────────
    excluded = build_excluded_set(args)
    _CONFIG["excluded"]  = excluded
    _CONFIG["timezone"]  = args.timezone

    # ── 2. Resolve CSV pairs ──────────────────────────────────────────────────
    if args.events:
        pairs = pairs_from_explicit(args.events, args.results)
        csv_dir = os.path.dirname(os.path.abspath(args.events[0]))
    else:
        csv_dir = os.path.abspath(args.directory) if args.directory else os.path.dirname(os.path.abspath(__file__))
        pairs   = find_csv_pairs(csv_dir)

    if not pairs:
        print("[!] Aucun fichier CSV trouvé.", file=sys.stderr)
        print(f"    Répertoire scanné : {csv_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"[*] {len(pairs)} fichier(s) CSV — campagne unique")
    for name, ev, _ in pairs:
        print(f"    {name}  →  {os.path.basename(ev)}")

    if excluded:
        print(f"[*] Exclus ({len(excluded)}) : {', '.join(sorted(excluded))}")
    else:
        print("[*] Aucune exclusion configurée")

    print(f"[*] Fuseau horaire : {args.timezone}")

    # ── 3. Load & process data ────────────────────────────────────────────────
    events, results = load_all(pairs)
    campaign_start  = events[events["message"] == "Email/SMS Sent"]["time"].min()
    reported_set    = load_reported_users(csv_dir, explicit_path=args.reported)
    temporal        = build_temporal_stats(events, campaign_start)
    victims         = build_victim_table(events, results, reported_set)

    print(f"[*] Signaleurs : {len(reported_set)}  |  Multi-cliqueurs : {len(temporal['multi'])}")

    # ── 4. Write Excel ────────────────────────────────────────────────────────
    out = os.path.abspath(args.output)
    wb  = xlsxwriter.Workbook(out, {"nan_inf_to_errors": True})
    fmt = FmtCache(wb)

    write_resume(wb, victims, fmt, reported_dict=reported_set, temporal=temporal)
    write_detail_sheet(wb, victims, fmt, "Identifiants saisis", [SEG_CREDS], "Identifiants saisis")
    write_detail_sheet(wb, victims, fmt, "Cliqué sans creds",   [SEG_CLICK],
                       "Cliqué uniquement — Lien visité, aucun credentials soumis")
    if reported_set:
        write_reported_sheet(wb, victims, fmt)
    write_temporal_sheet(wb, temporal, victims, fmt)
    write_all_sheet(wb, victims, fmt)

    wb.close()
    print(f"\n[+] Rapport → {out}")

    # ── 5. Summary ────────────────────────────────────────────────────────────
    total     = len(victims)
    seg_cnt   = victims["Segment"].value_counts()
    opened    = (victims["Ouvert"]       == "✓").sum()
    clicked   = (victims["Cliqué"]       == "✓").sum()
    submitted = (victims["Identifiants"] == "✓").sum()
    captured  = (victims["Session"]      == "✓").sum()
    print(f"\n{'─'*52}")
    print(f"  Cibles          : {total}")
    print(f"  Ouverts         : {opened}  ({round(opened/total*100,1) if total else 0}%)")
    print(f"  Cliqué          : {clicked}  ({round(clicked/total*100,1) if total else 0}%)")
    print(f"  Creds saisis    : {submitted}  ({round(submitted/total*100,1) if total else 0}%)")
    print(f"  Session capturée: {captured}  ({round(captured/total*100,1) if total else 0}%)")
    print(f"  Multi-cliqueurs : {len(temporal['multi'])}")
    print(f"  Weekend clics   : {temporal['weekend_clicks']}")
    print(f"  Délai médian    : {round(temporal['median_reaction'],0):.0f} min")
    print(f"{'─'*52}")
    for seg in [SEG_CREDS, SEG_CLICK, SEG_OPEN, SEG_NONE]:
        n = int(seg_cnt.get(seg, 0))
        print(f"  {seg:<30}: {n:>3} ({round(n/total*100,1) if total else 0}%)")
    print(f"{'─'*52}")


if __name__ == "__main__":
    main()
