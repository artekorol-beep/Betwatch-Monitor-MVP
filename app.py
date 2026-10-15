from pathlib import Path
from datetime import datetime, timezone
import io
import os
import time
import sqlite3
import threading

import pandas as pd
import requests
import streamlit as st
import plotly.express as px

st.set_page_config(page_title="Betwatch LIVE", page_icon="📡", layout="wide")
st.title("Betwatch LIVE")
st.caption("Живой монитор коэффициентов: сбор, постоянная история, фильтрация и сигналы. Сигналы — статистические изменения, не прогноз и не рекомендация ставки.")

API_URL_DEFAULT = "https://api.betwatch.fr/api/v1/football/live"
DB_PATH = os.environ.get("BETWATCH_DB_PATH", "betwatch_live.sqlite3")
DEFAULT_CSV = "betwatch_all_markets_session_3.csv"
POLL_KEY = "BETWATCH_API_KEY"

def get_api_key():
    try:
        value = st.secrets.get(POLL_KEY, "")
        if value:
            return value
    except Exception:
        pass
    return os.environ.get(POLL_KEY, "")

def connect_db():
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time_utc TEXT NOT NULL,
            match_id TEXT, team_1 TEXT, team_2 TEXT, league TEXT,
            match_time TEXT, is_ht TEXT, score_home TEXT, score_away TEXT, finished TEXT,
            market_id TEXT, market_name TEXT, market_status TEXT,
            runner_id TEXT, runner_name TEXT, odd REAL, volume REAL,
            source TEXT DEFAULT 'api'
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_obs_time ON observations(snapshot_time_utc)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_obs_keys ON observations(match_id, market_id, runner_id)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS volume_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT UNIQUE,
            detected_at_utc TEXT NOT NULL,
            match_id TEXT, team_1 TEXT, team_2 TEXT, league TEXT,
            score_home_at_signal TEXT, score_away_at_signal TEXT, minute_at_signal TEXT,
            market_id TEXT, market_name TEXT, runner_id TEXT, runner_name TEXT,
            market_status TEXT, previous_volume REAL, current_volume REAL,
            volume_increase_eur REAL, seconds_between REAL,
            odd_at_signal REAL, previous_odd REAL,
            match_finished INTEGER DEFAULT 0,
            final_home_score TEXT, final_away_score TEXT,
            match_result TEXT, market_result TEXT, settled_at_utc TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_volume_events_match ON volume_events(match_id)")
    con.commit()
    return con

def normalize_dataframe(df):
    df = df.copy()
    rename = {"timestamp":"snapshot_time_utc", "home":"team_1", "away":"team_2",
              "minute":"match_time", "home_goals":"score_home", "away_goals":"score_away"}
    for old, new in rename.items():
        if new not in df.columns and old in df.columns:
            df.rename(columns={old:new}, inplace=True)
    if "snapshot_time_utc" not in df:
        raise ValueError("Не найдено время снимка: нужен snapshot_time_utc или timestamp.")
    for col in ["market_name","market_id","runner_name","market_status","odd"]:
        if col not in df:
            raise ValueError(f"Нет обязательного столбца: {col}")
    if "match_id" not in df: df["match_id"] = ""
    if "team_1" not in df: df["team_1"] = ""
    if "team_2" not in df: df["team_2"] = ""
    if "league" not in df: df["league"] = ""
    if "match_time" not in df: df["match_time"] = ""
    if "is_ht" not in df: df["is_ht"] = ""
    if "score_home" not in df: df["score_home"] = ""
    if "score_away" not in df: df["score_away"] = ""
    if "finished" not in df: df["finished"] = ""
    if "runner_id" not in df: df["runner_id"] = ""
    if "volume" not in df: df["volume"] = None
    df["snapshot_time_utc"] = pd.to_datetime(df["snapshot_time_utc"], utc=True, errors="coerce")
    df["odd"] = pd.to_numeric(df["odd"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["market_status"] = df["market_status"].fillna("UNKNOWN").astype(str).str.upper()
    df["match_id"] = df["match_id"].fillna("").astype(str)
    df["market_id"] = df["market_id"].fillna("").astype(str)
    df["runner_id"] = df["runner_id"].fillna("").astype(str)
    df["runner_name"] = df["runner_name"].fillna("").astype(str)
    df["match_label"] = df["team_1"].fillna("").astype(str) + " — " + df["team_2"].fillna("").astype(str)
    empty = df["match_label"].str.strip().isin(["—", ""])
    df.loc[empty, "match_label"] = df.loc[empty, "match_id"].astype(str)
    return df

def api_to_rows(payload, stamp):
    if isinstance(payload, list):
        matches = payload
    elif isinstance(payload, dict):
        matches = next((payload[k] for k in ("data","matches","results") if isinstance(payload.get(k), list)), [])
    else:
        matches = []
    rows = []
    for m in matches:
        teams = m.get("teams") or {}
        live = m.get("live_info") or {}
        for market in m.get("markets") or []:
            for runner in market.get("runners") or []:
                rows.append({
                    "snapshot_time_utc": stamp,
                    "match_id": m.get("match_id"),
                    "team_1": teams.get("v1"), "team_2": teams.get("v2"),
                    "league": m.get("league"), "match_time": live.get("time"),
                    "is_ht": live.get("is_ht"), "score_home": live.get("goal_v1"),
                    "score_away": live.get("goal_v2"), "finished": live.get("finished"),
                    "market_id": market.get("market_id"), "market_name": market.get("name"),
                    "market_status": market.get("status"), "runner_id": runner.get("runner_id"),
                    "runner_name": runner.get("name"), "odd": runner.get("odd"),
                    "volume": runner.get("volume"), "source": "api",
                })
    return rows, len(matches)

def save_rows(rows, source="api"):
    if not rows: return 0
    frame = pd.DataFrame(rows)
    frame = normalize_dataframe(frame)
    frame["source"] = source
    frame = frame.dropna(subset=["snapshot_time_utc"])
    if frame.empty: return 0
    cols = ["snapshot_time_utc","match_id","team_1","team_2","league","match_time","is_ht","score_home","score_away","finished",
            "market_id","market_name","market_status","runner_id","runner_name","odd","volume","source"]
    frame["snapshot_time_utc"] = frame["snapshot_time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    records = []
    for row in frame[cols].itertuples(index=False, name=None):
        records.append(tuple(None if pd.isna(v) else v for v in row))
    with connect_db() as con:
        con.executemany("""INSERT INTO observations
        (snapshot_time_utc,match_id,team_1,team_2,league,match_time,is_ht,score_home,score_away,finished,
         market_id,market_name,market_status,runner_id,runner_name,odd,volume,source)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", records)
    return len(records)

def read_history(limit=500000):
    with connect_db() as con:
        df = pd.read_sql_query("SELECT snapshot_time_utc,match_id,team_1,team_2,league,match_time,is_ht,score_home,score_away,finished,market_id,market_name,market_status,runner_id,runner_name,odd,volume,source FROM observations ORDER BY snapshot_time_utc DESC LIMIT ?", con, params=(int(limit),))
    if df.empty: return df
    df = normalize_dataframe(df)
    return df.sort_values("snapshot_time_utc")

def ensure_event_columns():
    # Safe migration for DBs created by earlier app versions.
    with connect_db() as con:
        existing = {row[1] for row in con.execute("PRAGMA table_info(volume_events)").fetchall()}
        additions = {
            "match_finished": "INTEGER DEFAULT 0",
            "final_home_score": "TEXT", "final_away_score": "TEXT",
            "match_result": "TEXT", "market_result": "TEXT", "settled_at_utc": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in existing:
                con.execute(f"ALTER TABLE volume_events ADD COLUMN {name} {sql_type}")
        con.commit()


def settle_volume_events(latest_matches):
    """Settle only after API marks match finished and provides final score."""
    if not latest_matches:
        return 0
    latest = {}
    for m in latest_matches:
        live = m.get("live_info") or {}
        mid = m.get("match_id")
        if mid is not None:
            latest[str(mid)] = {
                "finished": live.get("finished"),
                "home": live.get("goal_v1"),
                "away": live.get("goal_v2"),
            }
    settled = 0
    with connect_db() as con:
        events = pd.read_sql_query(
            "SELECT event_id, match_id, team_1, team_2, market_name, runner_name FROM volume_events WHERE match_finished=0",
            con
        )
        for event in events.itertuples(index=False):
            result = latest.get(str(event.match_id))
            if not result:
                continue
            finished = str(result["finished"]).lower() in ("true", "1", "yes", "finished", "complete", "completed")
            try:
                home, away = int(float(result["home"])), int(float(result["away"]))
            except (TypeError, ValueError):
                continue
            if not finished:
                continue
            match_result = "Ничья" if home == away else ("Победа хозяев" if home > away else "Победа гостей")
            market_result = resolve_market_result(event.market_name, event.runner_name, home, away, event.team_1, event.team_2)
            con.execute("""
                UPDATE volume_events
                SET match_finished=1, final_home_score=?, final_away_score=?, match_result=?,
                    market_result=?, settled_at_utc=?
                WHERE event_id=?
            """, (str(home), str(away), match_result, market_result, datetime.now(timezone.utc).isoformat(), int(event.event_id)))
            settled += 1
        con.commit()
    return settled


def resolve_market_result(market_name, runner_name, home, away, team_home="", team_away=""):
    """Settle common full-time football markets; leave ambiguous markets for review."""
    import re
    market = str(market_name or "").lower()
    runner = str(runner_name or "").strip().lower()
    home_name = str(team_home or "").strip().lower()
    away_name = str(team_away or "").strip().lower()
    total = home + away

    # Full-time 1X2 / match odds. Match team labels to the stored fixture names.
    if "match odds" in market or market in ("1x2", "match result", "full time result", "full-time result"):
        if runner in ("the draw", "draw", "tie", "x", "удалить", "ничья"):
            return "Выиграл" if home == away else "Проиграл"
        if runner in ("home", "home win", "1") or (home_name and runner == home_name):
            return "Выиграл" if home > away else "Проиграл"
        if runner in ("away", "away win", "2") or (away_name and runner == away_name):
            return "Выиграл" if away > home else "Проиграл"
        # Some providers include the team name plus extra text (e.g. "Team A (1)").
        if home_name and home_name in runner and away_name not in runner:
            return "Выиграл" if home > away else "Проиграл"
        if away_name and away_name in runner and home_name not in runner:
            return "Выиграл" if away > home else "Проиграл"
        return "Нужна проверка исхода"

    if "both teams to score" in market or "btts" in market:
        yes = home > 0 and away > 0
        if runner in ("yes", "да"):
            return "Выиграл" if yes else "Проиграл"
        if runner in ("no", "нет"):
            return "Выиграл" if not yes else "Проиграл"
        return "Нужна проверка исхода"

    if "over/under" in market or "goals" in market or "total goals" in market:
        # First-half markets require the halftime score; never use the final score as a substitute.
        if "first half" in market or "half time" in market or "1st half" in market:
            return "Нужны данные счёта на перерыв"
        match = re.search(r"(\d+(?:\.\d+)?)", str(market_name))
        if not match:
            match = re.search(r"(\d+(?:\.\d+)?)", runner)
        if not match:
            return "Не удалось определить линию"
        line = float(match.group(1))
        is_over = "over" in runner or "больше" in runner
        is_under = "under" in runner or "меньше" in runner
        if not (is_over or is_under):
            return "Нужна проверка исхода"
        if total == line:
            return "Возврат" if line.is_integer() else "Нужна проверка азиатской линии"
        won = total > line if is_over else total < line
        return "Выиграл" if won else "Проиграл"

    return "Нужна проверка исхода"


def persist_volume_events(signals):
    if signals is None or signals.empty:
        return 0
    inserted = 0
    with connect_db() as con:
        for row in signals.itertuples(index=False):
            key = "|".join(str(getattr(row, c, "")) for c in
                           ("snapshot_time_utc", "match_id", "market_id", "runner_id"))
            try:
                con.execute("""
                    INSERT OR IGNORE INTO volume_events (
                        event_key, detected_at_utc, match_id, team_1, team_2, league,
                        score_home_at_signal, score_away_at_signal, minute_at_signal,
                        market_id, market_name, runner_id, runner_name, market_status,
                        previous_volume, current_volume, volume_increase_eur, seconds_between,
                        odd_at_signal, previous_odd
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    key, str(row.snapshot_time_utc), str(row.match_id), str(row.team_1), str(row.team_2), str(row.league),
                    str(row.score_home), str(row.score_away), str(row.match_time),
                    str(row.market_id), str(row.market_name), str(row.runner_id), str(row.runner_name), str(row.market_status),
                    float(row.previous_volume), float(row.volume), float(row.volume_increase_eur), float(row.seconds_between),
                    (float(row.odd) if pd.notna(row.odd) else None),
                    (float(row.previous_odd) if hasattr(row, "previous_odd") and pd.notna(row.previous_odd) else None),
                ))
                inserted += con.execute("SELECT changes()").fetchone()[0]
            except Exception:
                continue
        con.commit()
    return inserted


def read_volume_events():
    with connect_db() as con:
        return pd.read_sql_query("SELECT * FROM volume_events ORDER BY detected_at_utc DESC", con)


def score_label(row):
    home_score = row.get("score_home")
    away_score = row.get("score_away")
    if pd.isna(home_score) or pd.isna(away_score) or str(home_score).strip() == "" or str(away_score).strip() == "":
        score = "счёт н/д"
    else:
        score = f"{home_score:g}:{away_score:g}" if isinstance(home_score, (int, float)) and isinstance(away_score, (int, float)) else f"{home_score}:{away_score}"
    minute = row.get("match_time")
    minute_text = ""
    if pd.notna(minute) and str(minute).strip() and str(minute).lower() not in ("none", "nan"):
        minute_text = f" · {minute}'"
    ht = row.get("is_ht")
    if str(ht).lower() in ("true", "1", "yes"):
        minute_text += " · ПЕРЕРЫВ"
    return score + minute_text


def make_volume_signals(data, min_volume_increase_eur, max_gap):
    """Detect cumulative matched-volume increases per match/market/runner between snapshots."""
    if "volume" not in data.columns:
        return pd.DataFrame()
    work = data.dropna(subset=["snapshot_time_utc", "volume"]).copy()
    work = work[work["volume"] >= 0].sort_values("snapshot_time_utc")
    key_cols = ["match_id", "market_id", "runner_id"]
    work = work.drop_duplicates(subset=["snapshot_time_utc"] + key_cols, keep="last")
    group = work.groupby(key_cols, dropna=False, sort=False)
    work["previous_volume"] = group["volume"].shift(1)
    work["previous_time"] = group["snapshot_time_utc"].shift(1)
    work["seconds_between"] = (work["snapshot_time_utc"] - work["previous_time"]).dt.total_seconds()
    work["volume_increase_eur"] = work["volume"] - work["previous_volume"]
    valid = (
        work["previous_volume"].notna()
        & work["volume_increase_eur"].ge(float(min_volume_increase_eur))
        & work["seconds_between"].between(0.001, float(max_gap))
    )
    signals = work.loc[valid].copy()
    if signals.empty:
        return signals
    signals["volume_status_note"] = signals["market_status"].map(
        lambda x: "SUSPENDED — рынок приостановлен" if x == "SUSPENDED" else "Рост объёма"
    )
    return signals.sort_values(["volume_increase_eur", "snapshot_time_utc"], ascending=[False, False])


def make_signals(data, threshold_pct, max_gap, max_odd, extreme_ratio):
    work = data.dropna(subset=["snapshot_time_utc","odd"]).copy()
    work = work[(work["odd"] > 1.0) & (work["odd"] <= max_odd)]
    work = work.sort_values("snapshot_time_utc")
    key_cols = ["match_id","market_id","runner_id"]
    work = work.drop_duplicates(subset=["snapshot_time_utc"] + key_cols, keep="last")
    group = work.groupby(key_cols, dropna=False, sort=False)
    work["previous_odd"] = group["odd"].shift(1)
    work["previous_time"] = group["snapshot_time_utc"].shift(1)
    work["seconds_between"] = (work["snapshot_time_utc"] - work["previous_time"]).dt.total_seconds()
    work["change_pct"] = (work["odd"] - work["previous_odd"]) / work["previous_odd"] * 100
    work["absolute_change_pct"] = work["change_pct"].abs()
    low = work[["odd","previous_odd"]].min(axis=1)
    high = work[["odd","previous_odd"]].max(axis=1)
    work["odd_ratio"] = high / low
    work["extreme_jump"] = work["odd_ratio"] >= extreme_ratio
    work["suspended_flag"] = work["market_status"].eq("SUSPENDED")
    valid = work["previous_odd"].notna() & (work["previous_odd"] > 0) & work["seconds_between"].between(0.001, max_gap) & (work["absolute_change_pct"] >= threshold_pct)
    signals = work.loc[valid].copy()
    signals["signal_note"] = "Обычное изменение"
    signals.loc[signals["extreme_jump"], "signal_note"] = "ЭКСТРЕМАЛЬНЫЙ СКАЧОК — проверить"
    signals.loc[signals["suspended_flag"], "signal_note"] = "SUSPENDED — рынок приостановлен"
    signals.loc[signals["suspended_flag"] & signals["extreme_jump"], "signal_note"] = "SUSPENDED + ЭКСТРЕМАЛЬНЫЙ СКАЧОК"
    return signals.sort_values(["absolute_change_pct","snapshot_time_utc"], ascending=[False,False])

# Initialize persistent local DB.
connect_db()
ensure_event_columns()

with st.sidebar:
    st.header("Источник данных")
    uploaded = st.file_uploader("Импортировать CSV в постоянную историю", type=["csv"])
    if uploaded is not None and st.button("Импортировать CSV"):
        try:
            imported = normalize_dataframe(pd.read_csv(io.BytesIO(uploaded.getvalue())))
            count = save_rows(imported.to_dict("records"), source="csv")
            st.success(f"Импортировано строк: {count}")
            st.rerun()
        except Exception as e:
            st.error(f"Не удалось импортировать CSV: {e}")

    st.divider()
    st.header("LIVE API")
    api_url = st.text_input("API URL", value=API_URL_DEFAULT)
    interval = st.number_input("Интервал опроса API (сек.)", min_value=15, max_value=3600, value=15, step=5)
    run_minutes = st.number_input("Продолжительность (мин.)", min_value=1, max_value=1440, value=30, step=5)
    key = get_api_key()
    if key:
        st.success("API-ключ найден в Secrets")
    else:
        st.warning("Добавь BETWATCH_API_KEY в Streamlit Secrets")
    start_live = st.button("▶ Запустить LIVE-сбор", type="primary", disabled=not bool(key))
    st.caption("Сбор запускается в текущем сеансе Streamlit и длится заданное время. При перезапуске приложения активный цикл может прерваться; сохранённая SQLite-история остаётся на диске, если хостинг сохраняет файловую систему.")
    st.divider()
    st.header("Фильтры сигналов")
    threshold = st.number_input("Порог изменения (%)", min_value=0.1, max_value=1000.0, value=5.0, step=1.0)
    max_gap = st.number_input("Макс. интервал между снимками (сек.)", min_value=1, max_value=86400, value=120, step=30)
    max_odd = st.number_input("Максимальный коэффициент", min_value=1.01, max_value=100000.0, value=100.0, step=10.0)
    extreme_ratio = st.number_input("Экстремальный скачок: отношение коэффициентов ≥", min_value=2.0, max_value=1000.0, value=5.0, step=1.0)
    min_volume_increase = st.number_input("Сигнал при росте объёма ставок (€)", min_value=1.0, max_value=10000000.0, value=2000.0, step=500.0)
    show_suspended = st.checkbox("Показывать SUSPENDED отдельно", value=True)
    refresh_seconds = st.selectbox("Автообновление панели", [5, 10, 15, 30, 60], index=2)

if start_live:
    st.session_state["live_running"] = True
    st.session_state["live_deadline"] = time.monotonic() + int(run_minutes * 60)
    st.session_state["live_url"] = api_url
    st.session_state["live_interval"] = int(interval)
    st.session_state["live_key"] = key
    st.session_state["live_last_error"] = ""
    st.session_state["live_backoff_seconds"] = 0
    st.session_state["live_started_at"] = datetime.now(timezone.utc).isoformat()
    st.session_state["live_next_run"] = time.monotonic()
    st.rerun()

if st.session_state.get("live_running", False):
    now_mono = time.monotonic()
    remaining = st.session_state.get("live_deadline", 0) - now_mono
    if remaining <= 0:
        st.session_state["live_running"] = False
        st.success("LIVE-сессия завершена. Все полученные снимки сохранены в историю.")
    else:
        # IMPORTANT: Streamlit reruns the script to refresh the UI.
        # Do not poll the API on every rerun; poll only when the scheduled time arrives.
        next_run = float(st.session_state.get("live_next_run", 0))
        if now_mono >= next_run:
            stamp = datetime.now(timezone.utc).isoformat()
            try:
                response = requests.get(
                    st.session_state["live_url"],
                    headers={"Authorization": f"Token {st.session_state['live_key']}"},
                    timeout=20,
                )
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        wait_seconds = max(60, int(float(retry_after)))
                    except (TypeError, ValueError):
                        prior_backoff = int(st.session_state.get("live_backoff_seconds", 0))
                        wait_seconds = min(max(60, prior_backoff * 2 if prior_backoff else 60), 900)
                    st.session_state["live_backoff_seconds"] = wait_seconds
                    st.session_state["live_last_error"] = (
                        f"HTTP 429: лимит запросов. Следующая попытка через {wait_seconds} сек."
                    )
                    st.session_state["live_next_run"] = time.monotonic() + wait_seconds
                else:
                    response.raise_for_status()
                    payload = response.json()
                    batch, matches_count = api_to_rows(payload, stamp)
                    raw_matches = payload if isinstance(payload, list) else next(
                        (payload.get(k) for k in ("data", "matches", "results")
                         if isinstance(payload, dict) and isinstance(payload.get(k), list)), []
                    )
                    saved = save_rows(batch, source="api")
                    settle_volume_events(raw_matches)
                    st.session_state["live_last_error"] = ""
                    st.session_state["live_backoff_seconds"] = 0
                    st.session_state["live_last_batch"] = saved
                    st.session_state["live_last_matches"] = matches_count
                    st.session_state["live_last_poll"] = stamp
                    st.session_state["live_last_success"] = stamp
                    st.session_state["live_next_run"] = time.monotonic() + st.session_state["live_interval"]
            except Exception as exc:
                st.session_state["live_last_error"] = f"{type(exc).__name__}: {exc}"
                prior_backoff = int(st.session_state.get("live_backoff_seconds", 0))
                wait_seconds = min(max(30, prior_backoff * 2 if prior_backoff else 30), 900)
                st.session_state["live_backoff_seconds"] = wait_seconds
                st.session_state["live_next_run"] = time.monotonic() + wait_seconds

history = read_history()
if not history.empty:
    if not show_suspended:
        filtered = history[history["market_status"] != "SUSPENDED"].copy()
    else:
        filtered = history.copy()
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Строк в истории", f"{len(history):,}".replace(",", " "))
    c2.metric("Снимков", f"{history['snapshot_time_utc'].nunique():,}".replace(",", " "))
    c3.metric("Матчей", f"{history['match_label'].nunique():,}".replace(",", " "))
    c4.metric("Рынков", f"{history['market_id'].nunique():,}".replace(",", " "))
    c5.metric("Пустой odd", f"{history['odd'].isna().sum():,}".replace(",", " "))
    if st.session_state.get("live_running", False):
        st.info(f"LIVE активен · последний пакет: {st.session_state.get('live_last_batch', 0)} строк · матчей: {st.session_state.get('live_last_matches', '—')} · интервал опроса API: {st.session_state.get('live_interval', interval)} сек.")
        if st.session_state.get("live_last_error"):
            st.warning("Статус последнего запроса: " + st.session_state["live_last_error"])
        next_in = max(0, int(st.session_state.get("live_next_run", time.monotonic()) - time.monotonic()))
        st.caption(f"Следующий запрос к API примерно через {next_in} сек. Интерфейс обновляется отдельно и не вызывает API-запрос сам по себе.")
        st.progress(max(0.0, min(1.0, 1 - max(0, st.session_state.get("live_deadline", 0) - time.monotonic()) / max(1, int(run_minutes * 60)))))
    elif st.session_state.get("live_started_at"):
        st.caption("Последняя LIVE-сессия: " + str(st.session_state["live_started_at"]))
    st.subheader("Фильтры данных")
    matches = sorted(filtered["match_label"].dropna().astype(str).unique())
    markets = sorted(filtered["market_name"].dropna().astype(str).unique())
    sel_matches = st.multiselect("Матчи", matches, default=matches)
    sel_markets = st.multiselect("Рынки", markets, default=markets)
    view = filtered[filtered["match_label"].isin(sel_matches) & filtered["market_name"].isin(sel_markets)].copy()

    st.subheader("Текущий счёт матчей")
    score_source = filtered.sort_values("snapshot_time_utc").drop_duplicates("match_id", keep="last")
    score_source = score_source[score_source["match_label"].isin(sel_matches)].copy()
    if score_source.empty:
        st.info("Нет данных о счёте для выбранных матчей.")
    else:
        score_cols = st.columns(min(3, len(score_source)))
        for i, (_, score_row) in enumerate(score_source.iterrows()):
            with score_cols[i % len(score_cols)]:
                st.metric(
                    label=str(score_row["match_label"]),
                    value=score_label(score_row),
                    help=f"Последний снимок: {score_row['snapshot_time_utc']}"
                )

    signals = make_signals(view, threshold, int(max_gap), float(max_odd), float(extreme_ratio))
    volume_signals = make_volume_signals(view, float(min_volume_increase), int(max_gap))
    # Journal API-originated signals from the full history, independent of UI filters.
    # Imported CSV rows remain excluded from the event journal.
    api_history = history[history["source"].eq("api")].copy() if "source" in history.columns else history.iloc[0:0].copy()
    api_volume_signals = make_volume_signals(api_history, float(min_volume_increase), int(max_gap))
    newly_logged = persist_volume_events(api_volume_signals)
    if not show_suspended:
        signals = signals[~signals["suspended_flag"]]
        if not volume_signals.empty:
            volume_signals = volume_signals[volume_signals["market_status"] != "SUSPENDED"]
    a,b,c,d = st.columns(4)
    a.metric("Сигналов", len(signals))
    b.metric("Обычных", int((~signals["extreme_jump"] & ~signals["suspended_flag"]).sum()))
    c.metric("Экстремальных", int(signals["extreme_jump"].sum()))
    d.metric("SUSPENDED", int(signals["suspended_flag"].sum()))
    st.subheader("Сигналы — сортировка по абсолютному изменению")
    if signals.empty:
        st.info("Сигналов пока нет: нужны как минимум два снимка одного исхода.")
    else:
        show_cols = ["snapshot_time_utc","match_label","score_home","score_away","match_time","market_name","market_id","runner_name","market_status","previous_odd","odd","change_pct","seconds_between","extreme_jump","signal_note"]
        signals_view = signals[show_cols].copy()
        signals_view["snapshot_time_utc"] = signals_view["snapshot_time_utc"].dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        st.dataframe(signals_view, use_container_width=True, hide_index=True, height=350)
        st.download_button("⬇ Скачать сигналы коэффициентов CSV", signals_view.to_csv(index=False).encode("utf-8-sig"), file_name="betwatch_live_odds_signals.csv", mime="text/csv")

    st.subheader(f"Резкий рост объёма ставок: +€{min_volume_increase:,.0f} и более")
    st.caption("Сравнивается поле volume одного и того же исхода между соседними снимками. Предполагается, что API отдаёт накопленный объём в евро; если volume означает другое, порог нужно интерпретировать соответственно.")
    if volume_signals.empty:
        st.info("Сигналов роста объёма пока нет. Нужны два снимка одного исхода и прирост volume выше заданного порога.")
    else:
        vol_cols = ["snapshot_time_utc","match_label","score_home","score_away","match_time","market_name","market_id","runner_name","market_status","previous_volume","volume","volume_increase_eur","seconds_between","volume_status_note"]
        vol_view = volume_signals[vol_cols].copy()
        vol_view["snapshot_time_utc"] = vol_view["snapshot_time_utc"].dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        vol_view = vol_view.rename(columns={"volume":"current_volume"})
        st.metric("Сигналов роста объёма", len(vol_view))
        st.dataframe(vol_view, use_container_width=True, hide_index=True, height=350)
        st.download_button("⬇ Скачать сигналы объёма CSV", vol_view.to_csv(index=False).encode("utf-8-sig"), file_name="betwatch_live_volume_signals.csv", mime="text/csv")

    st.subheader("Журнал скачков объёма и результаты")
    journal = read_volume_events()
    if journal.empty:
        st.info("Журнал пока пуст. Сигналы начнут записываться по новым LIVE-снимкам, когда прирост объёма достигнет порога.")
    else:
        total_events = len(journal)
        settled = int(journal["match_finished"].fillna(0).astype(int).sum())
        known_market = int(journal["market_result"].fillna("").isin(["Выиграл", "Проиграл", "Возврат"]).sum())
        wins = int(journal["market_result"].eq("Выиграл").sum())
        losses = int(journal["market_result"].eq("Проиграл").sum())
        m1,m2,m3,m4 = st.columns(4)
        m1.metric("Скачков в журнале", total_events)
        m2.metric("Завершённых матчей", settled)
        m3.metric("Рынок выиграл", wins)
        m4.metric("Рынок проиграл", losses)
        st.caption("Автоматически рассчитываются только поддерживаемые рынки. Для 1X2, рынков первого тайма и неизвестных рынков результат помечается для проверки, если данных недостаточно.")
        journal_view = journal.copy()
        journal_view["detected_at_utc"] = pd.to_datetime(journal_view["detected_at_utc"], utc=True, errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        st.dataframe(journal_view, use_container_width=True, hide_index=True, height=400)
        st.download_button("⬇ Скачать журнал скачков и расчётов CSV", journal_view.to_csv(index=False).encode("utf-8-sig"), file_name="betwatch_volume_event_journal.csv", mime="text/csv")

    st.subheader("Коэффициенты во времени (UTC)")
    chart = view.dropna(subset=["odd","snapshot_time_utc"]).copy()
    chart = chart[(chart["odd"] > 1) & (chart["odd"] <= max_odd)]
    if not chart.empty:
        chart["series"] = chart["match_label"].astype(str) + " | " + chart["market_name"].astype(str) + " | " + chart["runner_name"].astype(str)
        options = sorted(chart["series"].unique())
        chosen = st.multiselect("Линии графика (пустой выбор — первые 20)", options, default=[])
        chart = chart[chart["series"].isin(chosen if chosen else options[:20])]
        fig = px.line(chart.sort_values("snapshot_time_utc"), x="snapshot_time_utc", y="odd", color="series",
                      hover_data=[x for x in ["market_status","market_id","runner_id","volume"] if x in chart.columns])
        fig.update_layout(xaxis_title="Время UTC", yaxis_title="Коэффициент", legend_title="Матч / рынок / исход")
        st.plotly_chart(fig, use_container_width=True)
    st.download_button("⬇ Экспорт всей истории CSV", history.drop(columns=["match_label"], errors="ignore").to_csv(index=False).encode("utf-8-sig"),
                       file_name="betwatch_live_history.csv", mime="text/csv")
    with st.expander("Контроль качества"):
        qa = {
            "Полные дубликаты": int(history.duplicated().sum()),
            "Пустой odd": int(history["odd"].isna().sum()),
            "odd ≤ 1": int((history["odd"].notna() & (history["odd"] <= 1)).sum()),
            f"odd > {max_odd:g}": int((history["odd"].notna() & (history["odd"] > max_odd)).sum()),
            "SUSPENDED": int(history["market_status"].eq("SUSPENDED").sum()),
            "Источники": ", ".join(sorted(history["source"].dropna().astype(str).unique())),
        }
        st.dataframe(pd.DataFrame({"Проверка":list(qa), "Значение":list(qa.values())}), hide_index=True, use_container_width=True)
else:
    st.info("История пока пустая. Импортируй CSV слева или запусти LIVE-сбор.")

# Lightweight rerun loop while live collection is active; rerun also refreshes the display.
if st.session_state.get("live_running", False):
    time.sleep(min(2, max(0.5, st.session_state.get("live_next_run", time.monotonic()) - time.monotonic())))
    st.rerun()
