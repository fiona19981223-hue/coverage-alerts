==================================================================
 DEPLOY: dashboard on your phone (Streamlit Cloud) + 24/7 alerts
 (GitHub Actions). One watchlist in GitHub = single source of truth.
==================================================================

THE END STATE
  - watchlist.csv lives in a PRIVATE GitHub repo (the one source of truth).
  - The dashboard runs on Streamlit Community Cloud (a https URL, opens on
    your phone). It READS the list from GitHub and WRITES your edits back.
  - The alerts run on GitHub Actions every 15 min and READ the same file.
  => Edit on your phone -> Save -> dashboard + alerts both follow. Automatic.

You do the account steps (I can't log in as you). I prepared all the code.

------------------------------------------------------------------
PART A — Private GitHub repo
------------------------------------------------------------------
A1. github.com -> New repository -> name e.g. "coverage-alerts" ->
    Visibility = PRIVATE -> Create.
A2. Upload these files (keep the folder paths):
        monitor.py
        alerts.py
        build_watchlist.py
        watchlist.csv
        alerts_state.json
        requirements.txt
        run_monitor.bat            (optional, for local use)
        .github/workflows/alerts.yml
    DO NOT upload:  alerts_config.json  or  .streamlit/secrets.toml
    (those hold secrets; .gitignore already excludes them).

------------------------------------------------------------------
PART B — A GitHub token (so the dashboard can save edits)
------------------------------------------------------------------
B1. github.com -> your avatar -> Settings -> Developer settings ->
    Personal access tokens -> Fine-grained tokens -> Generate new token.
B2. Resource owner = you;  Repository access = Only select repos ->
    pick "coverage-alerts".
B3. Permissions -> Repository permissions -> Contents = Read and write.
B4. Generate -> COPY the token (starts with github_pat_...). You'll paste
    it into Streamlit secrets in Part C.

------------------------------------------------------------------
PART C — Host the dashboard on Streamlit Community Cloud
------------------------------------------------------------------
C1. share.streamlit.io  ->  sign in with GitHub  ->  authorize.
C2. "Create app" -> pick repo coverage-alerts, branch main,
    main file = monitor.py  ->  Deploy.
C3. App -> Settings -> Secrets -> paste (TOML):
        app_password = "your-strong-password"
        github_token = "github_pat_...(from Part B)"
        github_repo  = "your-username/coverage-alerts"
    Save. The app reboots.
C4. Settings -> Sharing -> set the app PRIVATE / invite only your email
    (so the URL isn't open to anyone). The app_password is a second lock.
C5. Open the https URL on your phone -> enter the password -> you're in.
    Bookmark it / add to home screen.

------------------------------------------------------------------
PART D — Turn on 24/7 alerts (GitHub Actions)
------------------------------------------------------------------
D1. Repo -> Settings -> Secrets and variables -> Actions -> New secret:
        Name:  TEAMS_WEBHOOK_URL
        Value: (your Teams Workflows webhook URL)
D2. Repo -> Actions tab -> enable workflows -> "Coverage alerts" ->
    Run workflow (manual) to test. Then it runs on its schedule.

------------------------------------------------------------------
PART E — Avoid duplicates: turn OFF the local PC task
------------------------------------------------------------------
   PowerShell:
     Unregister-ScheduledTask -TaskName CoverageMonitorAlerts -Confirm:$false

==================================================================
NOTES
 - Free. Keep the repo + app PRIVATE (your coverage list).
 - The hosted app sleeps when idle and wakes in ~20-30s on open.
 - yfinance from cloud IPs can occasionally rate-limit; just refresh.
 - To edit the list: open the dashboard (phone ok) -> ✏️ Edit watchlist
   -> Save. It commits to GitHub; the alerts pick it up next run.
==================================================================
