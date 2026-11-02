FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer libreoffice-core fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m botuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
RUN chown -R botuser:botuser /app
USER botuser
ENV HOME=/home/botuser PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
