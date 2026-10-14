# Betwatch LIVE — volume event journal and settlement

Includes LIVE collection, current score, odds-change signals, and detection of cumulative `volume` increases of at least €2,000 by default. New API-originated volume signals are persisted in SQLite as a journal. When the API marks a match finished and provides a final score, the app records the final score and match outcome; common Over/Under and Both Teams to Score markets are settled automatically where possible. Unsupported/ambiguous markets are marked for manual review rather than guessed.

Important assumptions:
- `volume` must be a cumulative amount in euros for a given runner.
- Results are only journaled for newly collected LIVE API data; imported CSV history is not treated as newly observed signals.
- Match settlement requires `live_info.finished` to become true-like and final goals to be present in the API response.
- First-half markets and some 1X2 runner mappings need half-time data or explicit team/outcome mapping; they are marked for review when the available data is insufficient.
- SQLite persistence depends on the hosting filesystem. If Streamlit Cloud restarts onto ephemeral storage, use an external persistent database for durable journaling.

Install by replacing `app.py`, `requirements.txt`, and `README.md` in GitHub, committing, then rebooting the Streamlit app. Keep `BETWATCH_API_KEY` only in Streamlit Secrets.
