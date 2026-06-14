FROM mcr.microsoft.com/playwright/python:v1.60.0

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .
RUN uv run playwright install firefox chromium

EXPOSE 8788
CMD ["uv", "run", "python", "app/server.py"]