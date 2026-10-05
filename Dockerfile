# survey-agent respondent runtime image — the pure-Python stdlib runtime (no Django).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY . .
# stdlib engine + the LLM and Mongo extras; add [api] if this image also runs the gateway.
RUN pip install --no-cache-dir ".[llm,mongo]"

EXPOSE 8000

# Serve every survey in SURVEY_DIR at /s/<id> (hosted multi-survey runtime; --public binds 0.0.0.0).
# Other roles from the SAME image, pick one as the CMD:
#   survey-agent api --host 0.0.0.0 --port 8001     # the authenticated data-backend gateway ([api] extra)
#   python sidecar/runner.py                        # the trigger sidecar (separate task)
CMD ["survey-agent", "serve", "--public", "--multi", "--port", "8000"]
