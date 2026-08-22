FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache \
  bash \
  coreutils \
  findutils \
  jq \
  python3

COPY run.sh /run.sh
COPY app.py /app.py
COPY defaults /defaults

RUN chmod +x /run.sh

CMD [ "/run.sh" ]
