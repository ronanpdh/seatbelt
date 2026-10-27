# Base image for sandboxed scenario targets.
# Build:   rm -rf dist && uv build && docker build -f docker/Dockerfile -t seatbelt-target .
# Extend:  FROM seatbelt-target
#          RUN pip install --no-cache-dir anthropic   # whatever your target imports
# The runner mounts your code at /target (read-only) and writes ledgers to /out,
# and refuses an image whose seatbelt version differs from the host's.
# the digest pins the image; Dependabot keeps it current
FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
