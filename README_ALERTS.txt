============================================================
 Teams alerts for the Coverage Monitor — setup (one time)
============================================================

STEP 1 — Create the Teams webhook (Power Automate "Workflows")
-------------------------------------------------------------
Microsoft retired the old "Incoming Webhook" connector, so use Workflows:

  1. In Teams, open the channel you want alerts in.
  2. Click the "..." next to the channel name  ->  "Workflows"
     (or: Teams -> Apps -> search "Workflows").
  3. Pick the template:
        "Post to a channel when a webhook request is received"
  4. Name it (e.g. "Coverage Alerts"), confirm the Team + Channel, click Create/Add.
  5. It shows a URL ending in .../triggers/manual/.../run?...  -> COPY it.

STEP 2 — Paste the URL
----------------------
  Open alerts_config.json and replace the placeholder:
      "teams_webhook_url": "https://prod-XX.westus.logic.azure.com:443/workflows/..."

STEP 3 — Test it
----------------
  Open a terminal in C:\Users\jeffl\Claude project\5) Coverage-and-alert-agent and run:
      python alerts.py --test
  You should get HTTP 200/202 and a "✅ alerts connected" card in the channel.
  (If it fails, send me the HTTP code it prints.)

STEP 4 — Turn on the schedule
-----------------------------
  Right-click setup_alerts_schedule.ps1 -> "Run with PowerShell"
  (or run it from a terminal). It checks every 15 min, 09:00-16:00, Mon-Fri.

------------------------------------------------------------
What you'll receive: one card per check listing the names that
moved beyond threshold (1D >=5%, 1W >=10%, 1M >=20%, 3M >=30%,
1Yr >=50%). Each move alerts once per trading day (no spam).
Edit thresholds in alerts_config.json.
------------------------------------------------------------
