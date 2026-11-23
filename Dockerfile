FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/hartk1213/hearth" \
      org.opencontainers.image.description="Hearth: DIY family wall calendar" \
      org.opencontainers.image.licenses="MIT"

# tzdata so TZ=America/Los_Angeles etc. works (calendar times are shown in local time)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY static ./static
RUN mkdir -p /config && chmod 777 /config
VOLUME /config

ENV HEARTH_SETTINGS=/config/settings.json \
    HEARTH_DATA=/config/data.json \
    PYTHONUNBUFFERED=1

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health', timeout=4)"
CMD ["python", "server.py"]
