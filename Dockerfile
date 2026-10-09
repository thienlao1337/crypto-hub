FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Processes don't run as root: the panel talks to the internet and parses
# external data, and any bug in that code shouldn't immediately grant full
# privileges inside the container. The app has nothing to write to disk, so the
# restriction breaks nothing.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app     && chown -R app:app /srv
USER app

# One image for all processes - the command is set in docker-compose.
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]


# Test stage: the same code plus dev dependencies. They don't end up in the
# production image, but tests run in the same environment as the runtime.
FROM base AS dev

# Installing packages is the only reason root is needed here.
USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt     && chown -R app:app /srv
USER app

CMD ["pytest", "-q"]
