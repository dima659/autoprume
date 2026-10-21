FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --only-binary=:all: -r requirements.txt

COPY main.py shop_logic.py webapp_server.py index.html avatar.jpg ./

EXPOSE 10000
CMD ["python", "-u", "main.py"]