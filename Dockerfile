FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-controller.txt .
RUN pip install --no-cache-dir -r requirements-controller.txt
# Playwright optional on controller (for Test Search on Lamix)
RUN playwright install --with-deps chromium || true
COPY shared_db.py panels.py github_actions.py controller_bot.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 10000
CMD ["python", "controller_bot.py"]
