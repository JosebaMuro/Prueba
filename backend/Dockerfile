FROM python:3.12-slim
WORKDIR /srv/backend
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY backend/requirements.txt .
RUN pip install -r requirements.txt
COPY backend/app ./app
COPY frontend /srv/frontend
ENV FRONTEND_DIR=/srv/frontend DATA_DIR=/data WORKSPACE_DIR=/workspace HOST=0.0.0.0 PORT=8765
EXPOSE 8765
CMD ["python", "-m", "app.main"]
