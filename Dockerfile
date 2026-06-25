FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY loom.example.yaml ./loom.example.yaml

RUN pip install --no-cache-dir .

EXPOSE 4000

CMD ["uvicorn", "loom.gateway.app:app", "--host", "0.0.0.0", "--port", "4000"]
