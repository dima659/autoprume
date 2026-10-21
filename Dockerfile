FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --only-binary=:all: -r requirements.txt

COPY main.py webapp.py index.html ./

EXPOSE 10000
CMD ["python", "-u", "main.py"]