FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global cline@3.0.61

ADD https://github.com/microsoft/tgrep/releases/download/v1.0.8/tgrep-v1.0.8-x86_64-unknown-linux-musl.tar.gz /tmp/tgrep.tar.gz
RUN echo "2e3de5b7735eb84aa150d3a48e8cff8f27c05795bfccb8b52c9bd8881f7225ce  /tmp/tgrep.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/tgrep.tar.gz -C /usr/local/bin \
    && chmod 0555 /usr/local/bin/tgrep \
    && rm /tmp/tgrep.tar.gz

COPY requirements-locagent.txt /tmp/requirements-locagent.txt
RUN pip3 install --break-system-packages --no-cache-dir -r /tmp/requirements-locagent.txt

COPY entrypoint.sh /usr/local/bin/sandboxed-cline
RUN chmod 0555 /usr/local/bin/sandboxed-cline

ENTRYPOINT ["/usr/local/bin/sandboxed-cline"]
