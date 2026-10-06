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

EXPOSE 8081
USER 65534:65534

CMD ["python", "server.py"]
