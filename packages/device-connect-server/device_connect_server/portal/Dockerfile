FROM python:3.11-slim

# Install nsc and nats CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip docker.io \
    && rm -rf /var/lib/apt/lists/*

# Install nsc
RUN curl -fsSL https://github.com/nats-io/nsc/releases/download/v2.12.2/nsc-linux-amd64.zip -o /tmp/nsc.zip \
    && unzip /tmp/nsc.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/nsc \
    && rm /tmp/nsc.zip

# Install nats CLI
RUN curl -fsSL https://github.com/nats-io/natscli/releases/download/v0.3.2/nats-0.3.2-linux-amd64.zip -o /tmp/nats.zip \
    && unzip /tmp/nats.zip -d /tmp/nats \
    && mv /tmp/nats/nats-0.3.2-linux-amd64/nats /usr/local/bin/nats \
    && chmod +x /usr/local/bin/nats \
    && rm -rf /tmp/nats /tmp/nats.zip

WORKDIR /app

# Install Python dependencies
# Use --only-binary for eclipse-zenoh (requires Rust compiler to build from source)
COPY packages/device-connect-edge /app/packages/device-connect-edge
RUN pip install --no-cache-dir --only-binary eclipse-zenoh /app/packages/device-connect-edge

COPY packages/device-connect-server /app/packages/device-connect-server
RUN pip install --no-cache-dir "/app/packages/device-connect-server[portal]"

# Create credential and security dirs
RUN mkdir -p /root/.device-connect/credentials

EXPOSE 8080

CMD ["python", "-m", "device_connect_server.portal"]
