# Base image for sandboxed scenario targets.
# Build:   rm -rf dist && uv build && docker build -f docker/Dockerfile -t seatbelt-target .
# Extend:  FROM seatbelt-target
#          RUN pip install --no-cache-dir anthropic   # whatever your target imports
# The runner mounts your code at /target (read-only) and writes ledgers to /out,
# and refuses an image whose seatbelt version differs from the host's.
# the digest pins the image; Dependabot keeps it current
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
