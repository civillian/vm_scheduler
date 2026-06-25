FROM python:3.12-slim

# Create non-root user — required for OpenShift restricted SCC compatibility
# and general security best practice. uid/gid 1000 is a common convention;
# OpenShift will override with a random uid when openshift.enabled=true in helm.
RUN groupadd -g 1000 scheduler && \
    useradd -u 1000 -g scheduler -s /bin/bash -m scheduler

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure the app files are readable by the scheduler user
RUN chown -R scheduler:scheduler /app

USER scheduler

ENV PYTHONUNBUFFERED=1
