FROM python:3.11-slim-bookworm
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home bot
COPY --chown=bot:bot bot ./bot
COPY --chown=bot:bot main.py markets.json ./
COPY --chown=bot:bot scripts/healthcheck.py ./scripts/healthcheck.py
RUN mkdir -p data logs reports && chown bot:bot data logs reports
USER bot
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py", "run", "--record"]
