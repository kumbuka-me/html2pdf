# syntax=docker/dockerfile:1.28

FROM alpine:3.24

RUN apk add --no-cache \
  weasyprint \
  py3-gunicorn \
  font-noto \
  font-noto-cjk \
  && adduser -S -D -H -u 65532 -G root html2pdf

# Keep runtime settings after dependency installation so version and config
# changes preserve the cached package-installation layer.
ARG VERSION=dev

ENV \
  HTML2PDF__VERSION=$VERSION \
  HTML2PDF__LISTEN_ADDRESS=0.0.0.0:8080 \
  HTML2PDF__WORKERS=2 \
  HTML2PDF__TIMEOUT=45 \
  HTML2PDF__ASSET_POLICY=embedded \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  HOME=/tmp \
  XDG_CACHE_HOME=/tmp/.cache

WORKDIR /app

COPY html2pdf/ ./html2pdf/
COPY server/gunicorn.conf.py ./server/gunicorn.conf.py

USER 65532:0

EXPOSE 8080

ENTRYPOINT ["gunicorn"]
CMD ["--config", "server/gunicorn.conf.py", "html2pdf.app:application"]
