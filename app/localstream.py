import subprocess
import requests
import os
import time
import shutil
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.templating import Jinja2Templates


app = FastAPI()

ACESTREAM_CACHE_DIR = "/tmp/acestream-cache"
APP_PORT=int(os.getenv("APP_PORT", "15123"))
STREAMLINK_BINARY = os.getenv("STREAMLINK_BINARY", "/app/venv/bin/streamlink")
ACESTREAM_BINARY = os.getenv("ACESTREAM_BINARY", "/opt/acestream/acestreamengine")
ACESTREAM_CACHE_LIMIT = os.getenv("ACESTREAM_CACHE_LIMIT", "1")
ACESTREAM_ARGS = os.getenv("ACESTREAM_ARGS", "") 
M3U_DIR = os.getenv("M3U_DIR", "/data/m3u")

LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG")

ACESTREAM_RETRY_BACKOFF_FACTOR = os.getenv("ACESTREAM_RETRY_BACKOFF_FACTOR", "2")
ACESTREAM_RETRY_STATUS_FORCELIST = os.getenv("ACESTREAM_RETRY_STATUS_FORCELIST", "500,502,503,504").split(",")
ACESTRAM_RETRY_TOTAL = os.getenv("ACESTREAM_RETRY_TOTAL", "10")
ACESTREAM_POLL_TIME = os.getenv("ACESTREAM_POLL_TIME", "0.10")
ACESTREAM_STREAM_CHUNKSIZE = os.getenv("ACESTREAM_STREAM_CHUNKSIZE", "1024")

shutil.rmtree(ACESTREAM_CACHE_DIR, ignore_errors=True)
templates = Jinja2Templates(directory=M3U_DIR)

#LOGGER
logger = logging.getLogger('uvicorn.error')
logger.setLevel(logging.getLevelName(LOG_LEVEL))

###################### STREAMLINK ######################

@app.get("/m3u/{m3uFileName}.m3u")
async def m3u(request: Request, m3uFileName: str):
    hostname = request.base_url.hostname
    port = request.base_url.port
    scheme = request.base_url.scheme

    port = port if port != None else "443" if scheme == "https" else "80"
    
    params = request.query_params
    base_url = f"{scheme}://{hostname}:{port}"
    args = {    
        "request": request, 
        "hostname": hostname, 
        "port": port, 
        "scheme": scheme,
        "base_url": base_url
    }
    args.update(params)

    return templates.TemplateResponse(f"{m3uFileName}.m3u", args, media_type='text/plain')

@app.get("/picon/{piconFileName}")
async def piconFile(piconFileName: str):
    filename = f"/data/picon/{piconFileName}"
    return FileResponse(filename, media_type='image/gif')

@app.get("/streamlink/video")
async def stream(request: Request):
    url = request.query_params.get('url')
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is missing")

    streamlink_process = subprocess.Popen([STREAMLINK_BINARY, url, 'best', '--stdout'], 
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        def generate():
            
            try :
                while True:
                    output = streamlink_process.stdout.read(1024)
                    if not output:
                        logger.info("Streamlink process terminated pid: %s", streamlink_process.pid)
                        break
                    yield output
            finally:
                streamlink_process.terminate()
                streamlink_process.wait()
                
        class CustomStreamingResponse(StreamingResponse):
            async def listen_for_disconnect(self, receive) -> None:
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        streamlink_process.terminate()
                        streamlink_process.wait()
                        logger.info("Streamlink process terminated pid: %s", streamlink_process.pid)
                        break
            
        return CustomStreamingResponse(generate(), media_type='video/mp4')
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/streamlink/audio")
async def get_audio(request: Request):
    url = request.query_params.get('url')
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is missing")

    try:
        streamlink_process = subprocess.Popen(
            [STREAMLINK_BINARY, url, "worst", "--stdout"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        ffmpeg_process = subprocess.Popen(
            ["ffmpeg", "-i", "pipe:0", "-f", "mp3", "-"],
            stdin=streamlink_process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        def iter_audio():
            while True:
                chunk = ffmpeg_process.stdout.read(1024)
                if not chunk:
                    ffmpeg_process.kill()
                    streamlink_process.kill()
                    break
                yield chunk

        class CustomStreamingResponse(StreamingResponse):
            async def listen_for_disconnect(self, receive) -> None:
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        ffmpeg_process.kill()
                        streamlink_process.kill()
                        ffmpeg_process.wait()
                        logger.info("FFMPEG process terminated pid: %s", ffmpeg_process.pid)
                        streamlink_process.wait()
                        logger.info("Streamlink process terminated pid: %s", streamlink_process.pid)
                        break

        return CustomStreamingResponse(iter_audio(), media_type="audio/mpeg")
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))
    
###################### ACESTREAM ######################        

command = [ ACESTREAM_BINARY, "--client-console", "--http-port", "33666", 
                   "--cache-dir", f"{ACESTREAM_CACHE_DIR}", #"--cache-limit", f"{ACESTREAM_CACHE_LIMIT}", 
                   "", "--bind-all", ACESTREAM_ARGS]
acestream_process = subprocess.Popen(command, 
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)

# Primero, añadimos una función para reiniciar el proceso acestream
def restart_acestream():
    global acestream_process
    try:
        if acestream_process:
            acestream_process.terminate()
            acestream_process.wait()
            logger.info("Acestream process terminated")
    except Exception as e:
        logger.error(f"Error terminating acestream process: {e}")
    
    acestream_process = subprocess.Popen(command, 
                                       stdout=subprocess.PIPE, 
                                       stderr=subprocess.PIPE)
    logger.info("Acestream process restarted")
    time.sleep(2)

def stream_acestream_content(id):
    ace_url = f"http://127.0.0.1:33666/ace/getstream?id={id}"
    session = requests.Session()
    retry = Retry(
        total = int(ACESTRAM_RETRY_TOTAL),
        backoff_factor = float(ACESTREAM_RETRY_BACKOFF_FACTOR),
        status_forcelist = ACESTREAM_RETRY_STATUS_FORCELIST
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    try:
        response = session.get(ace_url, stream=True)
        empty_chunks_count = 0
        
        for chunk in response.iter_content(chunk_size=int(ACESTREAM_STREAM_CHUNKSIZE)):
            if chunk:  # Si el chunk tiene contenido
                empty_chunks_count = 0
                yield chunk
            else:
                empty_chunks_count += 1
                
                # Si recibimos varios chunks vacíos consecutivos, consideramos que el stream está muerto
                if empty_chunks_count >= 5:  # Puedes ajustar este número según necesites
                    logger.warning("Stream appears to be dead, restarting acestream...")
                    restart_acestream()
                    # Intentar reconectar al stream
                    response = session.get(ace_url, stream=True)
                    empty_chunks_count = 0
                    
            time.sleep(float(ACESTREAM_POLL_TIME))
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Stream connection error: {e}")
        restart_acestream()
        raise

@app.get("/acestream/video")
async def acestream(request: Request):
    id = request.query_params.get('id')
    if not id:
        raise HTTPException(status_code=400, detail="id parameter is missing")

    if not acestream_process or acestream_process.poll() is not None:
        logger.warning("Acestream process not running, starting it...")
        restart_acestream()

    try:
        return StreamingResponse(stream_acestream_content(id), media_type='video/mp4')
    except Exception as e:
        logger.error(f"Error in acestream endpoint: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
#TEST:
#http://192.168.2.100:15123/acestream/video?id=1969c27658d4c8333ab2c0670802546121a774a5
   
if __name__ == '__main__':
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG
    LOGGING_CONFIG["formatters"]["default"]["fmt"] = "%(asctime)s [%(name)s] %(levelprefix)s %(message)s"
    uvicorn.run(app, host='0.0.0.0', port=APP_PORT)

