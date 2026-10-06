FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
# 构建上下文里的文件权限可能带过来是 600，非 root 用户会读不了，统一放开读权限
RUN chmod -R a+rX /app

RUN mkdir -p /data/db && useradd -u 10001 -M -s /usr/sbin/nologin trader \
    && chown -R 10001:10001 /data

EXPOSE 8082
USER 10001:10001

CMD ["python", "server.py"]
