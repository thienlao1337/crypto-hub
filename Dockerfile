FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Образ один на все процессы — команда задаётся в docker-compose.
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]


# Стадия для тестов: тот же код плюс dev-зависимости. В прод-образ они
# не попадают, но прогон идёт в том же окружении, что и рантайм.
FROM base AS dev

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

CMD ["pytest", "-q"]
