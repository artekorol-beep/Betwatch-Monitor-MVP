"""
Betwatch Monitor — local Streamlit dashboard for historical CSV and optional live adapter.
Live mode is disabled until a real API adapter is configured from the original collector.
"""
from __future__ import annotations
import os
from pathlib import Path
import pandas as pd
import streamlit as st
import plotly.express as px

st.set_page_config(page_title="Betwatch Monitor", page_icon="📊", layout="wide")
st.title("Betwatch Monitor")
st.caption("Исторический монитор рынков · Live-подключение включается только после настройки реального API")

DEFAULT_DATA = os.getenv("BETWATCH_DATA", "betwatch_all_markets_session_3.csv")
DEFAULT_LOG = os.getenv("BETWATCH_API_LOG", "betwatch_session_3_api_log.csv")

with st.sidebar:
    st.header("Данные")
    data_path = st.text_input("Путь к CSV", DEFAULT_DATA)
    uploaded_csv = st.file_uploader("Или загрузите CSV", type=["csv"])
    st.divider()
    st.header("Фильтры")
    threshold_pct = st.number_input("Порог изменения (%)", min_value=0.1, max_value=100.0, value=5.0, step=0.5)
    max_gap = st.number_input("Макс. интервал между снимками (сек.)", min_value=1, max_value=3600, value=120, step=15)
    include_suspended = st.checkbox("Показывать приостановленные рынки", value=True)

@st.cache_data(show_spinner=False)
def load_csv(path: str, file_bytes: bytes | None = None):
    if file_bytes is not None:
        from io import BytesIO
        frame = pd.read_csv(BytesIO(file_bytes))
    else:
        frame = pd.read_csv(path)
    required = [
        "snapshot_time_utc", "match_id", "team_1", "team_2", "market_id",
        "market_name", "market_status", "runner_id", "runner_name", "odd", "volume"
    ]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(f"В CSV отсутствуют обязательные поля: {missing}")
    frame["snapshot_time_utc"] = pd.to_datetime(frame["snapshot_time_utc"], utc=True, errors="coerce")
    frame["odd"] = pd.to_numeric(frame["odd"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame["match_label"] = frame["team_1"].astype(str) + " — " + frame["team_2"].astype(str)
    return frame.sort_values("snapshot_time_utc").reset_index(drop=True)

try:
    file_bytes = uploaded_csv.getvalue() if uploaded_csv else None
    df = load_csv(data_path, file_bytes)
except Exception as exc:
    st.error(f"Не удалось загрузить CSV: {exc}")
    st.info("Загрузите CSV в боковой панели или положите его рядом с app.py.")
    st.stop()

entity = ["match_id", "market_id", "runner_id"]
full_dups = int(df.duplicated().sum())
key_dups = int(df.duplicated(["snapshot_time_utc"] + entity).sum())
n_snaps = int(df["snapshot_time_utc"].nunique())
n_matches = int(df["match_id"].nunique())
n_markets = int(df["market_id"].nunique())

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Строк", f"{len(df):,}".replace(",", " "))
c2.metric("Снимков", n_snaps)
c3.metric("Матчей", n_matches)
c4.metric("Market ID", n_markets)
c5.metric("Дубликаты ключа", key_dups)

with st.expander("Контроль качества данных", expanded=False):
    quality = pd.DataFrame([
        ("Период UTC", f"{df['snapshot_time_utc'].min()} — {df['snapshot_time_utc'].max()}"),
        ("Полные дубликаты", full_dups),
        ("Дубликаты snapshot + match + market + runner", key_dups),
        ("Пустые коэффициенты", int(df["odd"].isna().sum())),
        ("Пустые volume", int(df["volume"].isna().sum())),
    ], columns=["Проверка", "Результат"])
    st.dataframe(quality, use_container_width=True, hide_index=True)
    st.write("Статусы рынков")
    st.dataframe(df["market_status"].value_counts(dropna=False).rename_axis("status").reset_index(name="rows"),
                 use_container_width=True, hide_index=True)

matches = sorted(df["match_label"].dropna().unique().tolist())
selected_matches = st.sidebar.multiselect("Матчи", matches, default=matches)
x = df[df["match_label"].isin(selected_matches)].copy()
markets = sorted(x["market_name"].dropna().unique().tolist())
selected_markets = st.sidebar.multiselect("Названия рынков", markets, default=markets)
x = x[x["market_name"].isin(selected_markets)]
if not include_suspended:
    x = x[x["market_status"].astype(str).str.upper() != "SUSPENDED"]

if x.empty:
    st.warning("Нет строк для выбранных фильтров.")
    st.stop()

st.subheader("История коэффициентов")
runner_labels = (
    x[["match_label", "market_name", "market_id", "runner_name", "runner_id"]]
    .drop_duplicates()
    .assign(series=lambda z: z["match_label"].astype(str) + " | " + z["market_name"].astype(str)
            + " [market " + z["market_id"].astype(str) + "] | " + z["runner_name"].astype(str)
            + " [runner " + z["runner_id"].astype(str) + "]")
)
series_options = runner_labels["series"].tolist()
selected_series = st.multiselect("Серии для графика (выберите несколько)", series_options,
                                 default=series_options[:min(5, len(series_options))])
chosen = runner_labels[runner_labels["series"].isin(selected_series)]
plot_data = x.merge(chosen[["match_label", "market_name", "market_id", "runner_name", "runner_id", "series"]],
                    on=["match_label", "market_name", "market_id", "runner_name", "runner_id"], how="inner")
plot_data = plot_data.dropna(subset=["odd", "snapshot_time_utc"])
if not plot_data.empty:
    fig = px.line(plot_data, x="snapshot_time_utc", y="odd", color="series",
                  markers=True, title="Коэффициент во времени (UTC)")
    fig.update_layout(xaxis_title="Время UTC", yaxis_title="Odd", legend_title="Матч / рынок / исход")
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Выберите серии с доступными коэффициентами для построения графика.")

st.subheader("Изменения между соседними наблюдениями")
valid = x.dropna(subset=["snapshot_time_utc", "odd"]).sort_values(entity + ["snapshot_time_utc"]).copy()
valid["previous_time_utc"] = valid.groupby(entity)["snapshot_time_utc"].shift(1)
valid["previous_odd"] = valid.groupby(entity)["odd"].shift(1)
valid["gap_seconds"] = (valid["snapshot_time_utc"] - valid["previous_time_utc"]).dt.total_seconds()
valid["change_pct"] = (valid["odd"] / valid["previous_odd"] - 1.0) * 100.0
changes = valid.dropna(subset=["previous_odd"]).copy()
signals = changes[
    (changes["change_pct"].abs() >= threshold_pct)
    & (changes["gap_seconds"] > 0)
    & (changes["gap_seconds"] <= max_gap)
].copy()
# Signal candidate table is informational; suspended rows remain visibly labelled.
show_cols = [c for c in [
    "snapshot_time_utc", "match_label", "market_name", "market_id", "runner_name",
    "market_status", "previous_odd", "odd", "change_pct", "gap_seconds",
    "score_home", "score_away", "match_time"
] if c in signals.columns]
st.metric("Кандидатов на сигнал", len(signals))
st.caption("Сигналы — только статистические изменения коэффициентов, не прогноз и не рекомендация ставки.")
if not signals.empty:
    st.dataframe(signals.assign(abs_change=signals["change_pct"].abs())
                 .sort_values("abs_change", ascending=False)[show_cols],
                 use_container_width=True, hide_index=True)
    st.download_button("Скачать сигналы CSV", signals.to_csv(index=False).encode("utf-8-sig"),
                       file_name="betwatch_candidate_signals.csv", mime="text/csv")
else:
    st.info("При текущих фильтрах и порогах кандидатов на сигнал нет.")

with st.expander("Таблица наблюдений", expanded=False):
    cols = [c for c in ["snapshot_time_utc", "match_label", "league", "market_name", "market_id",
                        "market_status", "runner_name", "runner_id", "odd", "volume",
                        "score_home", "score_away", "match_time", "is_ht", "finished"] if c in x.columns]
    st.dataframe(x.sort_values("snapshot_time_utc", ascending=False)[cols].head(2000),
                 use_container_width=True, hide_index=True)
    st.download_button("Скачать отфильтрованные наблюдения CSV", x.to_csv(index=False).encode("utf-8-sig"),
                       file_name="betwatch_filtered_observations.csv", mime="text/csv")

st.divider()
st.subheader("Live-режим")
st.warning(
    "Live-опрос отключён: в предоставленных CSV нет endpoint, формата запроса и авторизации API. "
    "Чтобы включить его, нужно перенести безопасный адаптер из исходного сборщика. "
    "Этот интерфейс не выполняет фиктивные запросы и не показывает выдуманный статус подключения."
)
st.code(
    "Следующий шаг: перенести из исходного сборщика функцию HTTP-запроса и JSON parsing, "
    "убрав секреты в переменные окружения/Colab Secrets.",
    language="text"
)
