ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache \
  bash \
  ffmpeg \
  coreutils \
  findutils \
  jq \
  python3

COPY run.sh /run.sh
COPY app.py /app.py
COPY defaults /defaults

RUN chmod +x /run.sh

CMD [ "/run.sh" ]
