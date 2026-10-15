FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY wealth_advisor ./wealth_advisor
COPY data ./data
RUN pip install --no-cache-dir ".[api]"
ENV WA_DATA_DIR=/app/data WA_STATE_DIR=/app/.state WA_LOG_DIR=/app/logs
EXPOSE 8000
CMD ["uvicorn", "wealth_advisor.api:app", "--host", "0.0.0.0", "--port", "8000"]
