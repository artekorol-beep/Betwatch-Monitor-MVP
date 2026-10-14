from pathlib import Path
import io
import pandas as pd
import streamlit as st
import plotly.express as px

st.set_page_config(page_title="Betwatch Monitor", page_icon="📊", layout="wide")
st.title("Betwatch Monitor")
st.caption("Исторический монитор рынков · Статистические изменения коэффициентов, не прогноз и не рекомендация ставки.")

DEFAULT_CSV = "betwatch_all_markets_session_3.csv"
REQUIRED = {"snapshot_time_utc", "market_name", "market_id", "runner_name", "market_status", "odd"}

@st.cache_data(show_spinner=False)
def load_csv(file_bytes: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(file_bytes))
    missing = sorted(REQUIRED - set(df.columns))
    if missing:
        raise ValueError("В CSV отсутствуют обязательные столбцы: " + ", ".join(missing))
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
    signals.loc[signals["suspended_flag"], "signal_note"] = signals.loc[signals["suspended_flag"], "signal_note"].map(
        lambda note: "SUSPENDED · " + note)
    signals["previous_time"] = signals["previous_time"].dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    signals["snapshot_time_utc"] = signals["snapshot_time_utc"].dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    return signals.sort_values(["absolute_change_pct", "snapshot_time_utc"], ascending=[False, False])

st.sidebar.header("Данные")
uploaded = st.sidebar.file_uploader("Загрузите CSV", type=["csv"])
csv_path = st.sidebar.text_input("Или путь к CSV рядом с app.py", value=DEFAULT_CSV)
df = None
try:
    if uploaded is not None:
        df = load_csv(uploaded.getvalue())
    elif Path(csv_path).is_file():
        df = load_csv(Path(csv_path).read_bytes())
    else:
        st.info("Загрузите CSV слева или положите его рядом с app.py. Streamlit Cloud не имеет автоматического доступа к вашему Google Drive.")
except Exception as exc:
    st.error(f"Не удалось загрузить CSV: {exc}")

st.sidebar.header("Фильтры")
threshold_pct = st.sidebar.number_input("Порог изменения (%)", min_value=0.1, max_value=1000.0, value=5.0, step=1.0)
max_gap = st.sidebar.number_input("Макс. интервал между снимками (сек.)", min_value=1, max_value=86400, value=120, step=30)
max_odd = st.sidebar.number_input("Максимальный коэффициент", min_value=1.01, max_value=100000.0, value=100.0, step=10.0)
show_suspended = st.sidebar.checkbox("Показывать SUSPENDED с отдельной пометкой", value=True)
extreme_ratio = st.sidebar.number_input("Помечать скачок при отношении ≥", min_value=2.0, max_value=1000.0, value=5.0, step=1.0)
hide_extreme = st.sidebar.checkbox("Скрывать экстремальные скачки из основных сигналов", value=False)
top_n = st.sidebar.selectbox("Размер рейтинга изменений", [10, 20, 50], index=1)

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
    ordinary_count = int((~signals["extreme_jump"] & ~signals["suspended_flag"]).sum())
    extreme_count = int(signals["extreme_jump"].sum())
    suspended_count = int(signals["suspended_flag"].sum())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Сигналов после фильтров", len(primary))
    m2.metric("Обычные изменения", ordinary_count)
    m3.metric("Экстремальные скачки", extreme_count)
    m4.metric("SUSPENDED-сигналы", suspended_count)

    st.subheader(f"Топ-{top_n} изменений")
    st.caption("Рейтинг отсортирован по абсолютному проценту изменения. Экстремальные скачки и приостановленные рынки отмечены отдельно.")
    if primary.empty:
        st.info("Сигналов с текущими фильтрами не найдено.")
    else:
        display_cols = ["snapshot_time_utc", "match_label", "market_name", "market_id", "runner_name",
                        "market_status", "previous_odd", "odd", "change_pct", "seconds_between",
                        "extreme_jump", "signal_note"]
        display_cols = [col for col in display_cols if col in primary.columns]
        top = primary.head(int(top_n))[display_cols]
        st.dataframe(top, use_container_width=True, hide_index=True)
        st.download_button("Скачать топ изменений CSV", top.to_csv(index=False).encode("utf-8-sig"),
                           file_name="betwatch_top_changes.csv", mime="text/csv")

        st.subheader("Все сигналы")
        st.dataframe(primary[display_cols], use_container_width=True, hide_index=True, height=360)
        st.download_button("Скачать все сигналы CSV", primary[display_cols].to_csv(index=False).encode("utf-8-sig"),
                           file_name="betwatch_signals.csv", mime="text/csv")

    st.subheader("Коэффициент во времени (UTC)")
    if view.empty:
        st.warning("Нет данных для выбранных фильтров.")
    else:
        # Limit the initial chart to the first 20 series for readability; filters still affect which series are shown.
        chart = view.copy()
        chart["series"] = chart["match_label"].astype(str) + " | " + chart["market_name"].astype(str) + " | " + chart["runner_name"].astype(str)
        series_options = sorted(chart["series"].unique())
        selected_series = st.multiselect("Линии на графике (пустой выбор = первые 20)", series_options, default=[])
        if selected_series:
            chart = chart[chart["series"].isin(selected_series)]
        else:
            chart = chart[chart["series"].isin(series_options[:20])]
            st.caption("Для читаемости показаны первые 20 линий. Выбери нужные линии в фильтре выше.")
        hover_cols = [col for col in ["market_status", "market_id", "runner_id", "volume"] if col in chart.columns]
        fig = px.line(chart.sort_values("snapshot_time_utc"), x="snapshot_time_utc", y="odd", color="series", hover_data=hover_cols)
        fig.update_layout(xaxis_title="Время UTC", yaxis_title="Коэффициент", legend_title="Матч / рынок / исход")
        st.plotly_chart(fig, use_container_width=True)

    with st.expander("Контроль качества данных"):
        qa = {
            "Всего строк": len(df),
            "Полные дубликаты": int(df.duplicated().sum()),
            "Пустой odd": int(df["odd"].isna().sum()),
            "odd ≤ 1": int((df["odd"].notna() & (df["odd"] <= 1)).sum()),
            f"odd > {max_odd:g}": int((df["odd"].notna() & (df["odd"] > max_odd)).sum()),
            "SUSPENDED строк": int(df["market_status"].eq("SUSPENDED").sum()),
            "Невалидное время": int(df["snapshot_time_utc"].isna().sum()),
        }
        st.dataframe(pd.DataFrame({"Проверка": list(qa), "Значение": list(qa.values())}), hide_index=True, use_container_width=True)
else:
    st.warning("Нет загруженных данных. Загрузите CSV слева, чтобы открыть графики и сигналы.")
