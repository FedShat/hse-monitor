FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 monitor \
    && useradd --uid 10001 --gid monitor --no-create-home monitor \
    && mkdir /app/data \
    && chown monitor:monitor /app/data
COPY hse_monitor ./hse_monitor
USER 10001:10001
ENTRYPOINT ["python", "-m", "hse_monitor"]
CMD ["run"]
