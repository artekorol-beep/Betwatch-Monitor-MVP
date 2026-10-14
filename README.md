# Betwatch LIVE — исправление частых API-запросов

Главное исправление: API вызывается только когда наступает `live_next_run`. Перерисовка Streamlit больше не запускает запрос сама по себе.

Обновление:
1. Замени `app.py` и `requirements.txt` в GitHub-репозитории.
2. Commit changes.
3. В Streamlit Cloud перезапусти приложение (Manage app → Reboot app).
4. Проверь короткую сессию и смотри строку «Следующий запрос к API примерно через … сек.».

Секрет `BETWATCH_API_KEY` оставь в Streamlit Secrets. Не добавляй ключ в репозиторий.
