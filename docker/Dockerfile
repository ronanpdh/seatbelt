# Base image for sandboxed scenario targets.
# Build:   rm -rf dist && uv build && docker build -f docker/Dockerfile -t seatbelt-target .
# Extend:  FROM seatbelt-target
#          RUN pip install --no-cache-dir anthropic   # whatever your target imports
# The runner mounts your code at /target (read-only) and writes ledgers to /out,
# and refuses an image whose seatbelt version differs from the host's.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
