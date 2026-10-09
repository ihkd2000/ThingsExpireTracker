FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 THINGSEXPIRE_DB=/data/thingsexpiretracker.db
WORKDIR /app
COPY pyproject.toml README.md ./
COPY thingsexpiretracker ./thingsexpiretracker
RUN pip install --no-cache-dir . && useradd --system --create-home app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8080
# Listening beyond localhost requires THINGSEXPIRE_TOKEN; pass it with -e. Run `thingsexpiretracker remind` daily (see README).
CMD ["thingsexpiretracker", "serve", "--host", "0.0.0.0", "--port", "8080"]
