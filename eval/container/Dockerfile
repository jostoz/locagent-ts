FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global cline@3.0.61

COPY requirements-locagent.txt /tmp/requirements-locagent.txt
RUN pip3 install --break-system-packages --no-cache-dir -r /tmp/requirements-locagent.txt

COPY entrypoint.sh /usr/local/bin/sandboxed-cline
RUN chmod 0555 /usr/local/bin/sandboxed-cline

ENTRYPOINT ["/usr/local/bin/sandboxed-cline"]
