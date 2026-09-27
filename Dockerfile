FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent/ agent/
COPY jobs/ jobs/
EXPOSE 8080
# Same image serves the agent (default) and the RCA job:
#   docker run ... sre-agent python -m jobs.rca_job
CMD ["uvicorn", "agent.server:app", "--host", "0.0.0.0", "--port", "8080"]
