FROM python:3.11-slim

WORKDIR /app

# Copy only the registry service requirements
COPY packages/device-connect-server/device_connect_server/registry/service/requirements.txt .
RUN pip install --upgrade pip setuptools
RUN pip install --no-cache-dir --only-binary eclipse-zenoh -r requirements.txt

# Install device-connect-edge from sibling package
COPY packages/device-connect-edge/ /tmp/device-connect-edge/
RUN pip install --no-cache-dir /tmp/device-connect-edge/ && rm -rf /tmp/device-connect-edge/

# Copy device-connect-server library
COPY packages/device-connect-server/device_connect_server/ ./device_connect_server/

# Copy registry service code
COPY packages/device-connect-server/device_connect_server/registry/service/*.py ./

RUN useradd -m -u 1000 appuser
USER appuser

CMD ["python", "-u", "main.py"]
