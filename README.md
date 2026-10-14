# Betwatch Monitor — updated MVP

- Maximum odds default: 100.
- SUSPENDED rows can be shown and are marked separately.
- Extreme jumps are marked when the ratio between adjacent odds reaches the configured threshold (default 5x).
- Signals sorted by absolute percentage change.
- Signal CSV export.
- Historical CSV analysis only; live API polling is not connected.

Run locally:
```bash
pip install -r requirements.txt
streamlit run app.py
```

For Streamlit Cloud, replace `app.py` and `requirements.txt` in the GitHub repository and commit changes. Keep API keys out of the public repository.
