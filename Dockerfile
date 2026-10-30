FROM python:3.12-slim
WORKDIR /app
COPY roster_sync /app/roster_sync
EXPOSE 8000
CMD ["python", "-m", "roster_sync"]
