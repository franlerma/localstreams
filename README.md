# LocalStreams 📺

LocalStreams es un contenedor de Docker diseñado para facilitar la generación de playlists m3u de canales de TV. Este proyecto permite a los usuarios acceder fácilmente a una playlist personalizada con los canales de televisión que emiten por internet, así como a través de Acestream y StreamLink. 🌐

## Instalación ⚙️

Para instalar LocalStreams, sigue estos pasos:

1. 🐳 Asegúrate de tener Docker instalado.

2. 📂 Clona el repositorio:
    ```bash
    git clone https://github.com/franlerma/localstreams.git
    ```
3. 🛠️ Construye la imagen:
    ```bash
    chmod +x build.sh
    ./build.sh
    ```
4. ▶️ Ejecuta el contenedor:
    ```bash
    docker run -it --name localstreams \
        --network host \
        -l com.centurylinklabs.watchtower.enable=false -l wud.watch=false --restart always \
        -v /dev/dri:/dev/dri -v /opt/docker/volumes/localstreams/m3u:/data/m3u  \
        -v /opt/docker/volumes/localstreams/picon:/data/picon \
        --tmpfs /tmp/acestream-cache:exec,rw,size=500M \
        -e ACESTREAM_POLL_TIME=0 -e ACESTRAM_RETRY_TOTAL=10 -e ACESTREAM_ARGS="--live-cache-type memory" --platform=linux/amd64 $IMAGE_NAME \
        --add-host=67.215.246.10:router.bittorrent.com --add-host=82.221.103.244:router.utorrent.com 
    ```

## Uso 🖥️

Las plantillas m3u admiten el formato jinja2, por lo que puedes usar las variables de la aplicación como `{{scheme}}`, `{{hostname}}`, `{{port}}` o `{{base_url}}`, además de cualquier parámetro que pases por URL. 🌟

Puedes acceder a streams de Acestream y Streamlink en los siguientes endpoints especiales:

    {{scheme}}://{{hostname}}:{{port}}/acestream/video?id={id_acestream}
    {{base_url}}/streamlink/video?url={url_streamlink} # Soporta cualquier URL soportada por los plugins de Streamlink

Por ejemplo, para la lista que se obtiene en esta URL:

    http://127.0.0.1:15123/m3u/test.m3u?iptvserver=192.168.1.11:8080

puedes usar la variable `iptvserver` en la plantilla (además de las variables por defecto):

    #EXTM3U
    #EXTVLCOPT--http-reconnect=true

    #EXTINF:-1 tvg-logo="https://upload.wikimedia.org/wikipedia/commons/thumb/8/83/Logo_TVE-Internacional.svg/1403px-Logo_TVE-Internacional.svg.png" tvg-name="LA 1 HD" tvg-id="LA1.es", La 1
    http://{{iptvserver}}/stream.ts

    #EXTINF:-1 tvg-logo="https://upload.wikimedia.org/wikipedia/commons/thumb/8/83/Logo_TVE-Internacional.svg/1403px-Logo_TVE-Internacional.svg.png" tvg-name="LA 1 HD" tvg-id="LA1.es", La 1
    {{schema}}://{{hostname}}:{{port}}/streamlink/video?url=https://www.rtve.es/play/videos/directo/canales-lineales/la-1/

    #EXTINF:-1 tvg-logo="https://upload.wikimedia.org/wikipedia/commons/thumb/8/83/Logo_TVE-Internacional.svg/1403px-Logo_TVE-Internacional.svg.png" tvg-name="LA 1 HD" tvg-id="LA1.es", La 1
    {{schema}}://{{hostname}}:{{port}}/acestream/video?id=b897de3e62d7c6bee9ef1107d972f3d1075e03ff

Se pueden pasar tantas variables en el query string como se desee. ✨

## Licencia 📜
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)