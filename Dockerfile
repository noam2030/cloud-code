FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json ./
RUN npm install
COPY frontend ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git gh && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt backend/
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend ./backend
COPY --from=web /web/dist ./frontend/dist
ENV PORT=8080
CMD uvicorn backend.main:app --host 0.0.0.0 --port $PORT
