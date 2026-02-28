# Security Policy — BTC Prediction Bot

## Responsible Disclosure

If you discover a security vulnerability, please contact:
**signal@btcpredictor.io**

Do not open a public GitHub issue for security vulnerabilities.

---

## Git History Notice

**2026-02-28** — A security audit identified three credentials
accidentally committed to the public repository:

| Credential | File | Status |
|------------|------|--------|
| `BOT_API_KEY` (old) | `retrain_pipeline.sh` | ✅ Invalidated before discovery |
| `N8N_API_KEY` JWT | `opencode.json` | ✅ Rotated immediately |
| `SUPABASE_ANON_KEY` JWT | `app.py`, `index.html` | ✅ Rotated, RLS enforced |

**Remediation:**
- All credentials were rotated or invalidated
- Git history was rewritten with `git-filter-repo` to remove all secrets
- Force-push performed: commit SHAs before 2026-02-28 have changed
- GitHub cache purge requested

**Impact assessment:** No unauthorized access detected. The `BOT_API_KEY`
was already rotated before discovery. The Supabase anon key had RLS
policies in place limiting read access.

---

## Current Security Controls

- All secrets stored exclusively in Railway environment variables
- No `.env` file in repository (`.env.example` contains only placeholders)
- Supabase Row Level Security (RLS) enabled on all tables
- API endpoints protected by HMAC key comparison (`hmac.compare_digest`)
- Rate limiting: 50 req/min on trading endpoints
- On-chain audit trail on Polygon PoS — immutable record independent of git

---

## Audit Trail Integrity

The git history rewrite does **not** affect the project's verifiability.
The immutable audit trail lives on Polygon PoS:

- Contract: `BTCBotAudit.sol`
- Address: `0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55`
- Every prediction is committed on-chain **before** execution
- Every outcome is resolved on-chain **after** closing

The Polygon blockchain cannot be rewritten. Timestamps and hashes
are independently verifiable at `polygonscan.com`.
