# Intranet AI service: BM25 RAG + OpenAI-compatible LLM proxy.
FROM python:3.11-slim

WORKDIR /app

# Install deps first for better layer caching.
# Use a China PyPI mirror for reliable installs on the intranet build host.
COPY requirements.txt .
RUN pip install --no-cache-dir \
      -i https://pypi.tuna.tsinghua.edu.cn/simple \
      -r requirements.txt

# App code (rag.py, llm.py, app.py, config_store.py, admin_auth.py, importer.py, skills_store.py).
COPY *.py ./

# Admin UI (static HTML/CSS/JS served at /admin).
COPY admin ./admin

# Public skill library UI (served at /skills).
COPY skills ./skills

# /data is the shared volume: content/*.md (built by import.sh from the .zdoc)
VOLUME ["/data"]

EXPOSE 8000

# uvicorn with a single worker is plenty for an internal assistant.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
