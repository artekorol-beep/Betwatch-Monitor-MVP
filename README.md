# Betwatch Live Monitor MVP

- Accepts the existing historical CSV schema and the collector schema (`timestamp`, `home`, `away`, `minute`, `home_goals`, `away_goals`).
- Optional manual API polling against `https://api.betwatch.fr/api/v1/football/live`.
- Collects all market names returned by API (no Over/Under-only filter).
- API key must be configured as Streamlit secret `BETWATCH_API_KEY`; never hardcode it or commit it to GitHub.
- API polling is a bounded foreground run initiated by button; it is not a permanent background service and app reruns/restarts may interrupt it.
- Captured rows can be downloaded as CSV and re-uploaded for signal analysis.

Streamlit Cloud Secrets TOML:
```toml
BETWATCH_API_KEY = "YOUR_KEY_HERE"
```
Use app settings → Secrets. Do not share or commit the real key.

Run locally:
```bash
pip install -r requirements.txt
streamlit run app.py
```
