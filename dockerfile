FROM --platform=linux/amd64 ubuntu:22.04

LABEL \
    com.centurylinklabs.watchtower.enable="false" \
    wud.watch="false" \
    org.opencontainers.image.authors="Fran Lerma" \
    org.opencontainers.image.url=""

ENV LC_ALL="C.UTF-8" 
ENV LANG="C.UTF-8" 
ENV ACESTREAM_VERSION="3.2.3_ubuntu_22.04_x86_64_py3.10" 
ENV ACESTREAM_TGZ="acestream_${ACESTREAM_VERSION}.tar.gz"
ENV ACESTREAM_TGZ_URL="https://download.acestream.media/linux/${ACESTREAM_TGZ}"

WORKDIR /tmp
COPY app /app
COPY data /data
COPY resources /tmp

SHELL ["/bin/bash", "-c" ]

RUN sed -i 's/deb http:\/\/security.ubuntu.com/#/g' /etc/apt/sources.list
RUN apt-get update
RUN apt-get install --no-install-recommends -yq \
    ffmpeg python3-pip libpython3.10 python3-pip python3-virtualenv python3-venv ca-certificates wget sqlite3 net-tools \
      && rm -rf /var/lib/apt/lists/* \
      && mkdir /opt/acestream \
      && tar zxf "${ACESTREAM_TGZ}" -C /opt/acestream \
      && rm "${ACESTREAM_TGZ}" \
      && pushd /opt/acestream || exit \
      && python3.10 -m pip install -r requirements.txt \
      && /opt/acestream/start-engine --version \
      && popd || exit

RUN mv /tmp/player.html /opt/acestream/data/webui/html/player.html

RUN virtualenv -p python3.10 /app/venv
RUN /app/venv/bin/pip install -r /app/requirements.txt

EXPOSE 15123
EXPOSE 8621

ENTRYPOINT /app/venv/bin/python -u /app/localstream.py

HEALTHCHECK CMD wget -q -t1 -O- 'http://127.0.0.1:15123/check_health' | grep '{"healthy":true}'
