#!/bin/bash
PROJECT_NAME="localstreams"
IMAGE_NAME="franlerma/$PROJECT_NAME"
docker build . -t $IMAGE_NAME || exit 1
#docker run -it --name $PROJECT_NAME --publish 15123:15123 -l com.centurylinklabs.watchtower.enable=false -l wud.watch=false --restart always  $IMAGE_NAME
docker run -it --name localstreams --publish 15123:15123 -l com.centurylinklabs.watchtower.enable=false -l wud.watch=false --restart always -v /dev/dri:/dev/dri -v /opt/docker/volumes/localstreams/m3u:/data/m3u  -v /opt/docker/volumes/localstreams/picon:/data/picon -v /opt/docker/volumes/localstreams/tmp:/tmp/acestream-cache -e ACESTREAM_POLL_TIME=0 -e ACESTRAM_RETRY_TOTAL=10 -e ACESTREAM_ARGS="--live-cache-type memory" $IMAGE_NAME
