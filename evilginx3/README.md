# evilginx3 — IOC-Cleaned Build

Forked from [fin3ss3g0d/evilgophish](https://github.com/fin3ss3g0d/evilgophish), this is a hardened version of evilginx3 with known detection indicators removed for authorized internal phishing campaigns.

---

## IOC Removals

### 1. `X-Evilginx` HTTP Header (Critical)
**File:** `core/http_proxy.go`

The original code injected an `X-Evilginx` header into every proxied request toward the target site via `getHomeDir()` (`strings.Replace(".evilginx", ".e", "X-E", 1)`). This header is a primary network IOC referenced in detection rulesets (Suricata, Zeek, SIEM signatures).

**Fix:** Both `req.Header.Set(p.getHomeDir(), o_host)` calls removed.

---

### 2. Self-Signed Certificate Identity Strings
**File:** `core/certdb.go`

The auto-generated CA and leaf certificates in developer mode contained:
- Organization: `Evilginx Signature Trust Co.`
- CommonName: `Evilginx Super-Evil Root CA`

These strings are trivially detectable in TLS inspection or certificate scanning.

**Fix:** Replaced with `Sectigo Limited` / `Sectigo RSA Domain Validation CA`.

---

### 3. ACME Client User-Agent `CertMagic`
**File:** `main.go`, `vendor/github.com/caddyserver/certmagic/acmeclient.go`

The certmagic library sends `User-Agent: CertMagic` to Let's Encrypt ACME endpoints during certificate issuance. This string is correlatable across ACME logs and passive DNS.

**Fix:** `certmagic.UserAgent = "Mozilla/5.0"` set at startup.

---

### 4. DNS SOA Signature (TTL fingerprint)
**File:** `core/nameserver.go`

The built-in DNS server responded with a fixed SOA record matching known evilginx defaults:
- TTL: `300`, Refresh: `900`, Retry: `900`, Expire: `1800`, MinTTL: `60`
- Mbox: `hostmaster.<domain>`

These values appear in public evilginx detection signatures.

**Fix:** Values changed to standard registrar-like values (TTL: `3600`, Refresh: `7200`, Expire: `86400`, MinTTL: `300`, Mbox: `admin.<domain>`).

---

### 5. Forced `Cache-Control: no-cache, no-store` on All Responses
**File:** `core/http_proxy.go`

The proxy unconditionally overwrote `Cache-Control` on all `text/html`, `application/javascript`, and `application/json` responses. This blanket override is detectable by proxy inspection and inconsistent with legitimate site behavior.

**Fix:** Header is now only set when the upstream response does not already provide a `Cache-Control` value, and restricted to `text/html` only.

---

### 6. Config Directory `.evilginx`
**Files:** `core/http_proxy.go`, `main.go`

The default config directory was `~/.evilginx`, visible on disk and used internally as the basis for the `X-Evilginx` header key construction.

**Fix:** Renamed to `~/.config`.

---

### 7. Identifying Strings in Logs and Binary
**Files:** `main.go`, `core/banner.go`, `core/phishlet.go`, `core/config.go`, `core/terminal.go`

Several user-facing strings referenced "evilginx" or "@mrgretzky" which could leak into SIEM-forwarded logs or be extracted via `strings` on the binary.

**Fix:** All references replaced with neutral wording.

---

## Residual IOCs (Architectural)

| IOC | Notes |
|-----|-------|
| Simultaneous removal of CSP + HSTS + X-Frame-Options + X-XSS-Protection + X-Content-Type-Options | Required for MITM to function. Mitigation: reinject plausible fake values instead of deleting |
| Session cookie name format `[a-f0-9]{4}-[a-f0-9]{4}` | Detectable by regex. Can be further randomized |
| `SameSite=None` added to all cookies | Required for iframe-based session propagation |
| `module github.com/kgretzky/evilginx2` embedded in binary | Requires full `go.mod` + import path refactor to remove |
| All subdomains visible in Certificate Transparency logs | Use DNS-01 challenge with wildcard certs to avoid enumerating subdomains |

---

## Build

```bash
go build -o proxy .
```

## Usage

```bash
./proxy -g /path/to/gophish.db -p /path/to/phishlets/
```

---

## References

- [aalex954/evilginx2-TTPs](https://github.com/aalex954/evilginx2-TTPs) — IOC research this hardening is based on
- [fin3ss3g0d/evilgophish](https://github.com/fin3ss3g0d/evilgophish) — upstream project
