FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN set -eux; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i -e 's|http://deb.debian.org|https://deb.debian.org|g' \
               -e 's|http://security.debian.org|https://security.debian.org|g' /etc/apt/sources.list; \
    fi; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i -e 's|http://deb.debian.org/debian|https://deb.debian.org/debian|g' \
               -e 's|http://security.debian.org/debian-security|https://security.debian.org/debian-security|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates ffmpeg openssl; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY app /app/app

# Копируем сертификаты НУЦ Минцифры и собираем общий бандл вместе с certifi
COPY ca/russian_trusted_root_ca.cer /app/ca/russian_trusted_root_ca.cer
COPY ca/russian_trusted_sub_ca.cer /app/ca/russian_trusted_sub_ca.cer
RUN python - <<'PY'
import certifi, os
dst = '/app/ca/ru_bundle.pem'
os.makedirs('/app/ca', exist_ok=True)
with open(certifi.where(), 'rb') as base, open('/app/ca/russian_trusted_root_ca.cer', 'rb') as r, open('/app/ca/russian_trusted_sub_ca.cer', 'rb') as s, open(dst,'wb') as out:
    out.write(base.read()); out.write(b'\n'); out.write(r.read()); out.write(b'\n'); out.write(s.read()); out.write(b'\n')
print('bundle_written', dst)
PY

# Generate gRPC stubs for SaluteSpeech v2 AFTER sources are copied,
# so the generated files persist in the final image
RUN python -m grpc_tools.protoc -I/app/app/stt/proto \
    --python_out=/app/app/stt \
    --grpc_python_out=/app/app/stt \
    /app/app/stt/proto/recognitionv2.proto

EXPOSE 8000


