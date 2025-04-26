clean:
	docker rm -f localstreams
run-test:clean
	export DOCKER_BUILDKIT=0
	docker build . -t localstreams || exit 1
	docker run -it --name localstreams \
		-p 15123:15123 -p 33666:33666 -p 8621:8621 \
		--dns 1.1.1.1 \
		--dns 1.0.0.1 \
		-l com.centurylinklabs.watchtower.enable=false -l wud.watch=false --restart unless-stopped \
		-v ./data/m3u:/data/m3u  \
		-v ./data/picon:/data/picon \
		--tmpfs /tmp/acestream-cache:exec,rw,size=500M \
		-e ACESTREAM_POLL_TIME=0 -e ACESTRAM_RETRY_TOTAL=10 -e ACESTREAM_ARGS="--live-cache-type memory" \
		--platform=linux/amd64 localstreams \
		--add-host=67.215.246.10:router.bittorrent.com --add-host=82.221.103.244:router.utorrent.com 	

run: clean
	IMAGE_NAME="franlerma/localstreams"
	export DOCKER_BUILDKIT=0
	docker build . -t localstreams || exit 1
	docker run -it --name localstreams \
		--network host \
		--dns 1.1.1.1 \
		--dns 1.0.0.1 \
		-l com.centurylinklabs.watchtower.enable=false -l wud.watch=false --restart always \
		-v /dev/dri:/dev/dri -v /opt/docker/volumes/localstreams/m3u:/data/m3u  \
		-v /opt/docker/volumes/localstreams/picon:/data/picon \
		--tmpfs /tmp/acestream-cache:exec,rw,size=500M \
		-e ACESTREAM_POLL_TIME=0 -e ACESTRAM_RETRY_TOTAL=10 -e ACESTREAM_ARGS="--live-cache-type memory" \
		--platform=linux/amd64 localstreams \
		--add-host=67.215.246.10:router.bittorrent.com --add-host=82.221.103.244:router.utorrent.com 
