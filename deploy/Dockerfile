# docker build -f deploy/Dockerfile -t ynab-approver .
# docker run --rm --env-file .env -v $PWD/state:/app/state -v $PWD/policy.yaml:/app/policy.yaml ynab-approver run
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
 && npm install -g @anthropic-ai/claude-code && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
ENTRYPOINT ["approver"]
