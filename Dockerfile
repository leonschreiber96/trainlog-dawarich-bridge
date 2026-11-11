FROM python:3.12-slim

# tzdata so TZ (e.g. Europe/Berlin) works for the nightly run time and displayed times.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY trackfixer ./trackfixer

ENV PYTHONUNBUFFERED=1 \
    DATA_DIRECTORY=/data
EXPOSE 8000
# The port can be changed with PORT in .env.
CMD ["python", "-m", "trackfixer"]
