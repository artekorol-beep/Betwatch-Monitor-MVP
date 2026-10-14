# Betwatch LIVE — API interval fix + live score

Включает исправление расписания API-опроса и блок «Текущий счёт матчей». Счёт и игровая минута берутся из `live_info.goal_v1`, `live_info.goal_v2`, `live_info.time`; отображается последнее полученное значение по каждому матчу.

## Установка
1. Замени `app.py` и `requirements.txt` в GitHub-репозитории этими файлами.
2. Commit changes.
3. Streamlit Cloud → Manage app → Reboot app.
4. `BETWATCH_API_KEY` оставь в Streamlit Secrets.

Если API не присылает голы/минуту для матча, интерфейс покажет «счёт н/д». Значение обновляется при очередном успешном API-опросе.
