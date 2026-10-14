# Betwatch LIVE

## Features
- Polls `https://api.betwatch.fr/api/v1/football/live` using `Authorization: Token ...`.
- Collects all markets and runners returned by the API.
- Stores observations in a local SQLite database (`betwatch_live.sqlite3`).
- Signals sorted by absolute percent change; flags extreme jumps and SUSPENDED markets.
- Exports signals and full history to CSV.
- Can import the earlier session CSV or a live capture CSV.

## Streamlit Secrets
In Streamlit Cloud → Manage app → Settings → Secrets:
```toml
BETWATCH_API_KEY = "YOUR_REAL_KEY"
```
Never commit the real key to GitHub.

## Deploy
Replace `app.py`, `requirements.txt`, and optionally `README.md` in the existing GitHub repo. Keep the app entry path `app.py`.

## Persistence caveat
SQLite persists only as long as the deployment filesystem persists. Some hosted environments may reset local files during rebuilds/redeployments. For durable cross-redeploy storage, use an external database or persistent volume.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```

The LIVE collection is a bounded foreground loop in the app session, not a guaranteed always-on background service. A process restart can interrupt the active polling loop.
