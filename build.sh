#!/bin/bash
PROJECT_NAME="localstreams"
IMAGE_NAME="franlerma/$PROJECT_NAME"
#export DOCKER_BUILDKIT=1
docker build . -t $IMAGE_NAME || exit 1
docker run -it --name localstreams \
    --network host \
    --dns 1.1.1.1 \
    --dns 1.0.0.1 \
    -l com.centurylinklabs.watchtower.enable=false -l wud.watch=false --restart always \
    -v /dev/dri:/dev/dri -v /opt/docker/volumes/localstreams/m3u:/data/m3u  \
    -v /opt/docker/volumes/localstreams/picon:/data/picon \
    --tmpfs /tmp/acestream-cache:exec,rw,size=500M \
    -e ACESTREAM_POLL_TIME=0 -e ACESTRAM_RETRY_TOTAL=10 -e ACESTREAM_ARGS="--live-cache-type memory" \
    --platform=linux/amd64 $IMAGE_NAME \
    --add-host=67.215.246.10:router.bittorrent.com --add-host=82.221.103.244:router.utorrent.com 
    
