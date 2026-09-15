KENNER GROUP OWNER DASHBOARD - UPLOAD THESE FILES TO GITHUB

Repository: mattkenner/Kenner-toast
Branch: main

1. Upload app.py to the repository root.
2. Replace the existing requirements.txt with the requirements.txt in this package.
3. Commit both changes to main.
4. Do NOT replace sync.py; keep your currently working sync.py.

After uploading, return to ChatGPT and say: uploaded

The dashboard web service will use these environment variables in Render:
- DATABASE_URL
- DASHBOARD_USER
- DASHBOARD_PASSWORD
- DASHBOARD_TIMEZONE=America/New_York

The Toast sync cron job stays separate and continues running hourly.
