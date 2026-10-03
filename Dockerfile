FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 GATEWAY_CONFIG=/etc/gateway/config.yaml

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gateway ./gateway
COPY run.py config.example.yaml ./

# Конфиг монтируют на /etc/gateway (ConfigMap в Kubernetes или -v в docker run);
# без монтирования берётся config.example.yaml.
EXPOSE 8090

CMD ["python", "run.py"]
