FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY src ./src

# Upstream runs as root. Kubernetes runs this under runAsNonRoot with a
# read-only root filesystem, so declare a matching uid/gid here -- a numeric
# USER also lets the kubelet verify non-root before the container starts,
# which it cannot do for a name it would have to resolve inside the image.
RUN groupadd --gid 1000 mcp && useradd --uid 1000 --gid 1000 --no-create-home mcp
USER 1000:1000

EXPOSE 8000

CMD ["python", "src/server.py"]
