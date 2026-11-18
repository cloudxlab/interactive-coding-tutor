# The multi-user web app. It starts per-user containers through the host's Docker daemon (socket mounted by
# docker-compose.yml), so treat this container as highly privileged and keep it patched.
FROM docker:27-cli AS dockercli

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY requirements-server.txt .
RUN pip install -r requirements-server.txt \
 && useradd -m -u 10002 tutor && mkdir /data && chown 10002:10002 /data
COPY app.py auth.py proxy.py sandbox.py store.py runner.py ./
COPY static ./static
ENV TUTOR_MODE=multiuser TUTOR_HOST=0.0.0.0 TUTOR_DATA_DIR=/data TUTOR_PORT=8765
USER 10002
EXPOSE 8765
CMD ["python", "app.py"]
