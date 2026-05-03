FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ARG CACHEBUST=20260503_1840
COPY . .
CMD ["python", "bot.py"]
