FROM ubuntu:22.04

LABEL \
    com.centurylinklabs.watchtower.enable="false" \
    wud.watch="false" \
    org.opencontainers.image.authors="Fran Lerma" \
    org.opencontainers.image.url=""

ENV ACESTREAM_VERSION="3.2.3_ubuntu_22.04_x86_64_py3.10"
ENV ACESTREAM_TGZ="acestream_${ACESTREAM_VERSION}.tar.gz"
ENV ACESTREAM_TGZ_URL="https://download.acestream.media/linux/${ACESTREAM_TGZ}"

WORKDIR /tmp
COPY app /app
COPY data /data
COPY resources /tmp

SHELL ["/bin/bash", "-c" ]

#FIX DNS IN CASE DOMAINS ARE BLOCKED
RUN echo "67.215.246.10 router.bittorrent.com" >> /etc/hosts
RUN echo "67.215.246.10 router.utorrent.com" >> /etc/hosts
RUN echo "82.221.103.244 router.utorrent.com" >> /etc/hosts

RUN apt-get update
RUN apt-get install --no-install-recommends -y \
python3-pip libpython3.10 ffmpeg python3-pip python3-virtualenv python3-venv ca-certificates wget sudo\
  && rm -rf /var/lib/apt/lists/* \
  && mkdir acestream \
  && tar zxf "${ACESTREAM_TGZ}" -C acestream \
  && rm "${ACESTREAM_TGZ}" \
  && mv acestream /opt/acestream \
  && pushd /opt/acestream || exit \
  && bash ./install_dependencies.sh \
  && popd || exit

RUN virtualenv -p python3.10 /app/venv
RUN /app/venv/bin/pip install -r /app/requirements.txt
ENTRYPOINT /app/venv/bin/python -u /app/localstream.py

EXPOSE 15123/tcp 15123/udp

