# Betwatch LIVE v2 — rate-limit protection

This version handles HTTP 429 responses by honoring `Retry-After` when provided, otherwise applying exponential backoff up to 15 minutes. Existing SQLite history is retained.

Configure Streamlit Cloud → Manage app → Settings → Secrets:
```toml
BETWATCH_API_KEY = "YOUR_REAL_KEY"
```

Replace `app.py` and `requirements.txt` in the existing repo, commit changes, then reboot the Streamlit app. Do not commit the API key.

The live polling loop remains a bounded foreground session. SQLite persistence depends on the hosting filesystem.
