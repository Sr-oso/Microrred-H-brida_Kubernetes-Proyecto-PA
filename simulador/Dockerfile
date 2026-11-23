# docker build -f simulador/Dockerfile -t opencem-simulador:1.0 .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY simulador/requirements.txt .
RUN pip install -r requirements.txt
COPY simulador/simulador.py .

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sim
USER 10001

CMD ["python", "simulador.py"]