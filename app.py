from pathlib import Path
import io
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st
import plotly.express as px

st.set_page_config(page_title="Betwatch Live Monitor", page_icon="📊", layout="wide")
st.title("Betwatch Live Monitor")
st.caption("Исторические данные + опциональный ручной опрос Betwatch API. Сигналы — статистические изменения, не прогноз и не рекомендация ставки.")

API_URL_DEFAULT = "https://api.betwatch.fr/api/v1/football/live"
REQUIRED = {"snapshot_time_utc", "market_name", "market_id", "runner_name", "market_status", "odd"}

@st.cache_data(show_spinner=False)
def load_csv(file_bytes: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(file_bytes))
    return normalize_dataframe(df)

def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    rename = {
        "timestamp": "snapshot_time_utc",
        "home": "team_1", "away": "team_2",
        "minute": "match_time",
        "home_goals": "score_home", "away_goals": "score_away",
    }
    for old, new in rename.items():
        if new not in df.columns and old in df.columns:
            df = df.rename(columns={old: new})
    if "snapshot_time_utc" not in df.columns:
        raise ValueError("Не найдено время снимка: нужен snapshot_time_utc или timestamp.")
    for col in ["market_name", "market_id", "runner_name", "market_status", "odd"]:
        if col not in df.columns:
            raise ValueError(f"В данных отсутствует обязательный столбец: {col}")
    df["snapshot_time_utc"] = pd.to_datetime(df["snapshot_time_utc"], errors="coerce", utc=True)
    df["odd"] = pd.to_numeric(df["odd"], errors="coerce")
    df["market_status"] = df["market_status"].fillna("UNKNOWN").astype(str).str.upper()
    if "match_label" not in df:
        if {"team_1", "team_2"}.issubset(df.columns):
            df["match_label"] = df["team_1"].fillna("?").astype(str) + " — " + df["team_2"].fillna("?").astype(str)
        elif "match_id" in df:
            df["match_label"] = df["match_id"].astype(str)
        else:
            df["match_label"] = "Unknown match"
    return df

def api_payload_to_rows(payload, stamp):
    if isinstance(payload, list):
        matches = payload
    elif isinstance(payload, dict):
        matches = next((payload[k] for k in ("data", "matches", "results") if isinstance(payload.get(k), list)), [])
    else:
        matches = []
    rows = []
    for m in matches:
        teams = m.get("teams") or {}
        live = m.get("live_info") or {}
        for market in m.get("markets") or []:
            # Deliberately no market-name filter: collect all markets returned by the API.
            for runner in market.get("runners") or []:
                rows.append({
                    "snapshot_time_utc": stamp,
                    "match_id": m.get("match_id"),
                    "team_1": teams.get("v1"),
                    "team_2": teams.get("v2"),
                    "league": m.get("league"),
                    "match_time": live.get("time"),
                    "is_ht": live.get("is_ht"),
                    "score_home": live.get("goal_v1"),
                    "score_away": live.get("goal_v2"),
                    "finished": live.get("finished"),
                    "market_id": market.get("market_id"),
                    "market_name": market.get("name"),
                    "market_status": market.get("status"),
                    "runner_id": runner.get("runner_id"),
                    "runner_name": runner.get("name"),
                    "odd": runner.get("odd"),
                    "volume": runner.get("volume"),
                })
    return rows, len(matches)

def make_signals(data, threshold_pct, max_gap_seconds, max_odd, show_suspended, extreme_ratio):
    keys = ["match_label", "market_id", "runner_name"]
    work = data.dropna(subset=["snapshot_time_utc", "odd"]).copy()
    work = work[(work["odd"] > 1.0) & (work["odd"] <= max_odd)]
    if not show_suspended:
        work = work[work["market_status"] != "SUSPENDED"]
    work = work.sort_values("snapshot_time_utc")
    work = work.drop_duplicates(subset=["snapshot_time_utc"] + keys, keep="last")
    groups = work.groupby(keys, dropna=False, sort=False)
    work["previous_odd"] = groups["odd"].shift(1)
    work["previous_time"] = groups["snapshot_time_utc"].shift(1)
    work["seconds_between"] = (work["snapshot_time_utc"] - work["previous_time"]).dt.total_seconds()
    work["change_pct"] = (work["odd"] - work["previous_odd"]) / work["previous_odd"] * 100
    work["absolute_change_pct"] = work["change_pct"].abs()
    low = work[["odd", "previous_odd"]].min(axis=1)
    high = work[["odd", "previous_odd"]].max(axis=1)
    work["odd_ratio"] = high / low
    work["extreme_jump"] = work["odd_ratio"] >= extreme_ratio
    work["suspended_flag"] = work["market_status"].eq("SUSPENDED")
    valid = (work["previous_odd"].notna()
             & work["seconds_between"].between(0.001, max_gap_seconds)
             & (work["absolute_change_pct"] >= threshold_pct))
    signals = work.loc[valid].copy()
    signals["signal_note"] = "Обычное изменение"
    signals.loc[signals["extreme_jump"], "signal_note"] = "Экстремальный скачок — проверить"
    signals.loc[signals["suspended_flag"], "signal_note"] = signals.loc[signals["suspended_flag"], "signal_note"].map(lambda x: "SUSPENDED · " + x)
    signals["previous_time"] = signals["previous_time"].dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    signals["snapshot_time_utc"] = signals["snapshot_time_utc"].dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    return signals.sort_values(["absolute_change_pct", "snapshot_time_utc"], ascending=[False, False])

st.sidebar.header("Данные")
uploaded = st.sidebar.file_uploader("Загрузить CSV", type=["csv"])
csv_path = st.sidebar.text_input("Путь к CSV рядом с app.py", value="betwatch_all_markets_session_3.csv")
df = None
try:
    if uploaded is not None:
        df = load_csv(uploaded.getvalue())
    elif Path(csv_path).is_file():
        df = load_csv(Path(csv_path).read_bytes())
except Exception as exc:
    st.sidebar.error(f"CSV: {exc}")

with st.sidebar.expander("Подключение к Betwatch API", expanded=False):
    st.caption("API-ключ вводи только в секреты Streamlit, а не в публичный GitHub. Для Cloud: App settings → Secrets.")
    st.code('BETWATCH_API_KEY = "вставь_ключ_в_секреты"', language="toml")
    api_url = st.text_input("API URL", value=API_URL_DEFAULT)
    interval = st.number_input("Интервал опроса (сек.)", min_value=10, max_value=3600, value=30, step=10)
    duration_min = st.number_input("Длительность сессии (мин.)", min_value=1, max_value=240, value=10, step=5)
    start_live = st.button("Запустить опрос API")
    stop_note = "Опрос выполняется в рамках текущего запуска приложения. При остановке/перезагрузке Streamlit сессия прервётся."

st.sidebar.header("Фильтры")
threshold_pct = st.sidebar.number_input("Порог изменения (%)", min_value=0.1, max_value=1000.0, value=5.0, step=1.0)
max_gap = st.sidebar.number_input("Макс. интервал между снимками (сек.)", min_value=1, max_value=86400, value=120, step=30)
max_odd = st.sidebar.number_input("Максимальный коэффициент", min_value=1.01, max_value=100000.0, value=100.0, step=10.0)
show_suspended = st.sidebar.checkbox("Показывать SUSPENDED с отдельной пометкой", value=True)
extreme_ratio = st.sidebar.number_input("Помечать скачок при отношении ≥", min_value=2.0, max_value=1000.0, value=5.0, step=1.0)
hide_extreme = st.sidebar.checkbox("Скрывать экстремальные скачки из основных сигналов", value=False)
top_n = st.sidebar.selectbox("Размер рейтинга изменений", [10, 20, 50], index=1)

if start_live:
    try:
        api_key = st.secrets.get("BETWATCH_API_KEY", "")
    except Exception:
        api_key = os.environ.get("BETWATCH_API_KEY", "")
    if not api_key:
        st.error("Не найден BETWATCH_API_KEY. Добавь ключ в Streamlit App settings → Secrets, затем перезапусти приложение.")
    else:
        new_rows = []
        progress = st.progress(0)
        status = st.empty()
        deadline = time.monotonic() + int(duration_min * 60)
        headers = {"Authorization": f"Token {api_key}"}
        total_matches = 0
        errors = 0
        try:
            while time.monotonic() < deadline:
                stamp = datetime.now(timezone.utc).isoformat()
                try:
                    response = requests.get(api_url, headers=headers, timeout=20)
                    response.raise_for_status()
                    batch, match_count = api_payload_to_rows(response.json(), stamp)
                    new_rows.extend(batch)
                    total_matches = match_count
                    status.info(f"API OK · матчей в последнем ответе: {match_count} · строк за сессию: {len(new_rows)} · ошибок: {errors}")
                except Exception as exc:
                    errors += 1
                    status.warning(f"Ошибка API ({type(exc).__name__}): {exc}")
                elapsed = int(duration_min * 60 - max(0, deadline - time.monotonic()))
                progress.progress(min(1.0, elapsed / max(1, int(duration_min * 60))))
                time.sleep(min(int(interval), max(0, deadline - time.monotonic())))
        except Exception as exc:
            st.error(f"Опрос остановлен: {exc}")
        if new_rows:
            live_df = pd.DataFrame(new_rows)
            live_csv = live_df.to_csv(index=False).encode("utf-8-sig")
            st.session_state["live_csv_bytes"] = live_csv
            st.session_state["live_rows_count"] = len(new_rows)
            st.session_state["live_match_count"] = total_matches
            st.success(f"Опрос завершён: {len(new_rows)} строк. Скачай CSV ниже и загрузи его для анализа.")
        else:
            st.warning("Опрос завершён, но строки не получены. Проверь API URL, секрет и формат ответа.")
if "live_csv_bytes" in st.session_state:
    st.download_button("Скачать CSV из последнего API-опроса", st.session_state["live_csv_bytes"], file_name="betwatch_live_capture.csv", mime="text/csv")
    st.caption(f"Последний опрос: {st.session_state.get('live_rows_count', 0)} строк; матчей в последнем ответе: {st.session_state.get('live_match_count', 0)}.")

if df is not None:
    st.sidebar.success(f"Загружено строк: {len(df):,}".replace(",", " "))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Строк", f"{len(df):,}".replace(",", " "))
    c2.metric("Снимков", f"{df['snapshot_time_utc'].nunique():,}".replace(",", " "))
    c3.metric("Матчей", f"{df['match_label'].nunique():,}".replace(",", " "))
    c4.metric("Пустой odd", f"{df['odd'].isna().sum():,}".replace(",", " "))
    usable = df.dropna(subset=["snapshot_time_utc", "odd"]).copy()
    usable = usable[(usable["odd"] > 1.0) & (usable["odd"] <= max_odd)]
    if not show_suspended:
        usable = usable[usable["market_status"] != "SUSPENDED"]
    match_options = sorted(usable["match_label"].dropna().astype(str).unique())
    market_options = sorted(usable["market_name"].dropna().astype(str).unique())
    selected_matches = st.multiselect("Матчи", match_options, default=match_options)
    selected_markets = st.multiselect("Рынки", market_options, default=market_options)
    view = usable[usable["match_label"].isin(selected_matches) & usable["market_name"].isin(selected_markets)].copy()
    signals = make_signals(view, threshold_pct, int(max_gap), float(max_odd), show_suspended, float(extreme_ratio))
    primary = signals[~signals["extreme_jump"]].copy() if hide_extreme else signals.copy()
    st.subheader("Краткая сводка сигналов")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Сигналов после фильтров", len(primary))
    m2.metric("Обычные изменения", int((~signals["extreme_jump"] & ~signals["suspended_flag"]).sum()))
    m3.metric("Экстремальные скачки", int(signals["extreme_jump"].sum()))
    m4.metric("SUSPENDED-сигналы", int(signals["suspended_flag"].sum()))
    st.subheader(f"Топ-{top_n} изменений")
    if not primary.empty:
        cols = ["snapshot_time_utc", "match_label", "market_name", "market_id", "runner_name", "market_status",
                "previous_odd", "odd", "change_pct", "seconds_between", "extreme_jump", "signal_note"]
        cols = [col for col in cols if col in primary.columns]
        top = primary.head(int(top_n))[cols]
        st.dataframe(top, use_container_width=True, hide_index=True)
        st.download_button("Скачать топ изменений CSV", top.to_csv(index=False).encode("utf-8-sig"),
                           file_name="betwatch_top_changes.csv", mime="text/csv")
        st.subheader("Все сигналы")
        st.dataframe(primary[cols], use_container_width=True, hide_index=True, height=360)
        st.download_button("Скачать все сигналы CSV", primary[cols].to_csv(index=False).encode("utf-8-sig"),
                           file_name="betwatch_signals.csv", mime="text/csv")
    else:
        st.info("Сигналов с текущими фильтрами не найдено.")
    st.subheader("Коэффициент во времени (UTC)")
    if not view.empty:
        chart = view.copy()
        chart["series"] = chart["match_label"].astype(str) + " | " + chart["market_name"].astype(str) + " | " + chart["runner_name"].astype(str)
        series_options = sorted(chart["series"].unique())
        selected_series = st.multiselect("Линии на графике (пустой выбор = первые 20)", series_options, default=[])
        chart = chart[chart["series"].isin(selected_series if selected_series else series_options[:20])]
        hover_cols = [col for col in ["market_status", "market_id", "runner_id", "volume"] if col in chart.columns]
        fig = px.line(chart.sort_values("snapshot_time_utc"), x="snapshot_time_utc", y="odd", color="series", hover_data=hover_cols)
        fig.update_layout(xaxis_title="Время UTC", yaxis_title="Коэффициент", legend_title="Матч / рынок / исход")
        st.plotly_chart(fig, use_container_width=True)
    with st.expander("Контроль качества данных"):
        qa = {"Всего строк": len(df), "Полные дубликаты": int(df.duplicated().sum()),
              "Пустой odd": int(df["odd"].isna().sum()), "odd ≤ 1": int((df["odd"].notna() & (df["odd"] <= 1)).sum()),
              f"odd > {max_odd:g}": int((df["odd"].notna() & (df["odd"] > max_odd)).sum()),
              "SUSPENDED строк": int(df["market_status"].eq("SUSPENDED").sum()),
              "Невалидное время": int(df["snapshot_time_utc"].isna().sum())}
        st.dataframe(pd.DataFrame({"Проверка": list(qa), "Значение": list(qa.values())}), hide_index=True, use_container_width=True)
else:
    st.info("Загрузи CSV слева для исторического анализа. API-опрос можно запустить отдельно в боковой панели.")
