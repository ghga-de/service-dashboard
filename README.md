# GHGA Service Dashboard

A single page dashboard for the GHGA service repository landscape.

- **Latest release** — version + date (GitHub's pinned "Latest", not newest-by-date)
- **Test-bed** — the version pinned in the integration test-bed compose, marked
  ✓ latest / ▼ behind / ▲ ahead relative to the latest release
- **Latest pre-release** — version + date
- **Open PRs** — count (expand a row to see them)
- **Main vs release** — whether `main` has commits not yet released (▲ N unreleased)

For the **`file-services-backend`** monorepo it also lists each service's
version (from `services/<svc>/pyproject.toml`) at the latest release tag vs. on
`main` vs. the test-bed, so you can see which service drifted.

## Using the dashboard

**Hosted:** open <https://ghga-de.github.io/service-dashboard/>. The page talks to the 
GitHub API directly from the user's browser.

It runs in one of two modes:

- **Lite (no token):** can only collect information from public respositories, releases and open-PR counts only. Limited with the GitHub's 60/hour unauthenticated budget (uses ≈35 API calls per refresh). The main-vs-release, archive-test-bed and per-service columns need GitHub token.
- **Full (token):** click **Token** (top right) and paste a PAT. Rate limit increases to 5,000 requests/hour. Full set of features are active, Lite + authoritative latest releases, main-vs-release comparison, per-service monorepo versions, and the test-bed column (the `archive-test-bed` repo is private).

**Token scope**: public repos and read-only. A classic PAT with `repo`, or a fine-grained token granting read-only access to the relevant `ghga-de` repositories. Create one at
<https://github.com/settings/tokens>.

The token is kept in the browser's `sessionStorage` only. Responses are cached with ETags, and a full payload is reused for 60 s across page reloads, so refreshes are cheaper.

## Local backend (optional)

`dashboard.py` provides a Python backend works as a local server if you prefer to run it locally while keeping the token out of the browser entirely:

```bash
export GITHUB_TOKEN=ghp_xxxxxxxx
python3 dashboard.py                  # stdlib only — no pip install
```

Then open **http://127.0.0.1:8888**. The page auto-detects the backend (via
`/healthz`) and uses its `/api/data` endpoint; the token stays in the Python
process. The same `index.html` powers both modes.

### Backend options (environment variables)

| Var | Default | Meaning |
|-----|---------|---------|
| `GITHUB_TOKEN` / `GH_TOKEN` | – | API token |
| `PORT` | `8888` | Port to serve on |
| `HOST` | `127.0.0.1` | Bind address |
| `CACHE_TTL` | `60` | Seconds to cache results (use **Refresh** to force) |
| `MONOREPO_DETAIL` | `1` | Set `0` to skip per-service version lookups |
| `TESTBED_REPO` | `archive-test-bed` | Repo holding the integration test-bed compose |
| `TESTBED_REF` | `main` | Branch/ref to read the compose from |
| `TESTBED_COMPOSE_PATH` | `.devcontainer/docker-compose.yml` | Path to the compose file |
| `HTTP_RETRIES` | `3` | Attempts per request on transient errors |
| `REQUEST_SPACING` | `0.05` | Min seconds between outgoing request starts |
| `LOG_LEVEL` | `INFO` | Backend log verbosity (`DEBUG`/`INFO`/`WARNING`) |

Note: the static page only honours the defaults above; the environment knobs
apply to the Python backend. To change the repo list for both, edit
`STANDALONE_REPOS` / `FSB_SERVICES` in **both** `dashboard.py` and
`index.html` (the lists are mirrored).

## Features

### Export

The **Export** button (top right) opens a copy/download panel with a plain-text
list of every service's latest release (the monorepo is expanded into its
services; repos without a release are omitted). Three formats: `image:tag`
(default, e.g. `ghga/auth-service:10.0.0`), `name: version`, and aligned
`name  version`.

### Test-bed column

The version each service runs in the inter-service integration test bed is read
once per refresh from the **`archive-test-bed`** repo's
`.devcontainer/docker-compose.yml` (`main`). Every `ghga/<name>:<version>` image
is matched to its repo (or monorepo service) and compared to the latest release,
so **▼ behind** flags a service the test-bed hasn't been bumped to yet. Repeated
instances (rest/consumer/init) collapse to one unique version.

**Branch override.** The **Test-bed branch** box (top right) reads the compose
from any branch on demand, type a branch and press Enter / **Load**, and only
the Test-bed column re-computes (one extra API call, reusing the cached release
data). The active branch is shown in the column header (`@<branch>`) and the subtitle; 
**reset to main** clears it.