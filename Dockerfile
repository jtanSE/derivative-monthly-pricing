# Month-end derivative pricing — internal web app (Streamlit front+back in one).
FROM python:3.12-slim

WORKDIR /app

# uv for fast, locked installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install third-party deps first (cached layer). The project itself is imported
# from ./src via the app's sys.path, so we don't need to build it here.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group ui --no-install-project

# Application code only — NO broker data is ever copied into the image
# (.dockerignore enforces this).
COPY src ./src
COPY ui ./ui

ENV PATH="/app/.venv/bin:$PATH"
# Optional: a mounted folder of statements to pre-load; otherwise users upload.
ENV STATEMENTS_DIR=""
# Optional: set ANTHROPIC_API_KEY at runtime to enable self-service onboarding
# of brand-new brokers. The app is fully functional without it.

EXPOSE 8501

CMD ["streamlit", "run", "ui/app.py", \
     "--server.headless=true", "--server.port=8501", "--server.address=0.0.0.0"]
