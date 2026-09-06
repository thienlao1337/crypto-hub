FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Процессы работают не от root: панель ходит в интернет и разбирает
# внешние данные, и любая ошибка в этом коде не должна сразу давать
# полные права внутри контейнера. Записывать на диск приложению нечего,
# поэтому ограничение ничего не ломает.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app     && chown -R app:app /srv
USER app

# Образ один на все процессы — команда задаётся в docker-compose.
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]


# Стадия для тестов: тот же код плюс dev-зависимости. В прод-образ они
# не попадают, но прогон идёт в том же окружении, что и рантайм.
FROM base AS dev

# Установка пакетов — единственное, ради чего здесь нужен root.
USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt     && chown -R app:app /srv
USER app

CMD ["pytest", "-q"]
