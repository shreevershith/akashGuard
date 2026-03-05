FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV AGENT_PORT=8000

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Keep the source under /app/agent so `uvicorn agent.api:app` works.
COPY . /app/agent

# Normalize possible Windows CRLF line endings copied from host.
RUN sed -i 's/\r$//' /app/agent/start.sh

EXPOSE 8000

CMD ["sh", "/app/agent/start.sh"]
