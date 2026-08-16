FROM ghcr.io/astral-sh/uv:0.12.1 AS uv

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates software-properties-common \
    && add-apt-repository --yes ppa:kicad/kicad-10.0-releases \
    && apt-get update \
    && apt-get install --yes --no-install-recommends kicad kicad-libraries \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/
RUN groupadd --gid 10001 pcbdraft \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash pcbdraft \
    && install -d -o pcbdraft -g pcbdraft -m 0700 /data

WORKDIR /opt/pcbdraft
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY packaging/container/entrypoint.sh /usr/local/bin/pcbdraft-container-entrypoint
RUN chmod 0755 /usr/local/bin/pcbdraft-container-entrypoint

ENV HOME=/data \
    PATH=/opt/pcbdraft/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8765
USER pcbdraft

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=3).read(1)"

ENTRYPOINT ["pcbdraft-container-entrypoint"]
CMD ["app", "--host", "0.0.0.0", "--public-host", "127.0.0.1", "--port", "8765", "--no-open"]
