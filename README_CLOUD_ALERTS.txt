==================================================================
 ALWAYS-ON alerts via GitHub Actions (runs in the cloud, 24/7,
 free, no tokens — independent of your PC being on)
==================================================================

WHAT RUNS IN THE CLOUD
  GitHub runs `python alerts.py` on a schedule (every 15 min,
  ~08:00-16:45 HK time, Mon-Fri). The Teams webhook is stored as an
  encrypted GitHub "secret" (never in the code). The de-dupe state
  (alerts_state.json) is committed back each run so no move repeats.

----------------------------------------------------------------
STEP 1 — GitHub account
  Have one? skip.  Else: github.com -> Sign up (free).

STEP 2 — Create a PRIVATE repository
  github.com -> New repository
    Name: coverage-alerts   (anything)
    Visibility: PRIVATE  <-- important (your coverage list lives here)
    Create.

STEP 3 — Upload these files (keep the folder path for the workflow)
  From C:\Users\jeffl\Test, upload:
    - alerts.py
    - watchlist.csv
    - alerts_state.json          (your already-seeded state -> quiet start)
    - requirements.txt
    - .github/workflows/alerts.yml   (the .github/workflows path matters!)
  DO NOT upload alerts_config.json (it holds your webhook). The cloud
  reads the webhook from the secret in Step 4; thresholds default to
  the same 5/10/20/30/50 in code.

  Easiest web method: repo -> "Add file" -> "Upload files" for the
  first four. For the workflow, "Add file" -> "Create new file" ->
  type the name exactly:  .github/workflows/alerts.yml
  then paste the contents of that file -> Commit.

STEP 4 — Add the webhook as a secret
  Repo -> Settings -> Secrets and variables -> Actions
    -> New repository secret
    Name:  TEAMS_WEBHOOK_URL
    Value: (paste your Teams Workflows webhook URL)
    Add secret.

STEP 5 — Enable + test
  Repo -> Actions tab -> (enable workflows if prompted)
    -> "Coverage alerts" -> "Run workflow" (manual).
  Check the run log + your Teams channel. (If already seeded, it may
  say "No new outsized moves" — that's correct.)
  From then on it runs automatically on the schedule.

STEP 6 — Turn OFF the local PC task (so you don't get duplicates)
  In PowerShell:
    Unregister-ScheduledTask -TaskName CoverageMonitorAlerts -Confirm:$false

----------------------------------------------------------------
NOTES / HONEST CAVEATS
  - Cost: free. Private-repo Actions get 2,000 min/month free; this
    uses ~1,000-1,200 min/month. Public repo = unlimited (but then
    your coverage list is public — keep it PRIVATE).
  - Timing: GitHub cron is "best-effort" and can be delayed a few
    minutes (occasionally skipped under heavy load). Fine for move
    alerts; not for second-level precision.
  - To change thresholds in the cloud: easiest is to also upload
    alerts_config.json WITHOUT the url line, e.g. {"thresholds": {...}}.
  - To update the watchlist: edit it locally, then re-upload
    watchlist.csv to the repo (or use git).
==================================================================
