FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY roster_sync /app/roster_sync
EXPOSE 8000
CMD ["python", "-m", "roster_sync"]
