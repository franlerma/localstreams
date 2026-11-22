import asyncio
import contextlib
import shutil
import logging
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from config import STREAMLINK_CHUNKSIZE

router = APIRouter()
logger = logging.getLogger("LocalStreams.hls_mux")

# Solo se conserva el final de stderr: un stream largo no debe acumular memoria
STDERR_TAIL_BYTES = 8192

@router.get("/hls/mux")
async def mux_hls_streams(request: Request):
    """
    Combina dos streams HLS separados (video + audio) en uno solo usando FFmpeg.
    Parámetros:
    - video: URL del stream M3U8 de video
    - audio: URL del stream M3U8 de audio
    """
    video_url = request.query_params.get('video')
    audio_url = request.query_params.get('audio')
    
    if not video_url or not audio_url:
        raise HTTPException(400, "Se requieren los parámetros 'video' y 'audio'")
    
    if not (video_url.startswith(('http://', 'https://')) and audio_url.startswith(('http://', 'https://'))):
        raise HTTPException(400, "URLs inválidas")
    
    # Verificar que ffmpeg esté disponible
    ffmpeg_path = shutil.which('ffmpeg')
    if not ffmpeg_path:
        logger.error("FFmpeg no encontrado en PATH")
        raise HTTPException(500, "FFmpeg no está disponible")
    
    start_time = datetime.now()
    logger.info(f"Iniciando mux HLS - Video: {video_url[:100]}... Audio: {audio_url[:100]}...")
    
    # Comando FFmpeg para remultiplexar streams HLS
    cmd = [
        ffmpeg_path,
        '-hide_banner',
        '-loglevel', 'warning',  # Solo avisos y errores
        '-nostats',  # Sin línea de progreso en stderr
        '-i', video_url,
        '-i', audio_url,
        '-c', 'copy',  # Copy sin recodificar
        '-f', 'mpegts',  # Formato MPEG-TS para mejor compatibilidad con streaming
        '-'  # Output a stdout
    ]
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        logger.info(f"Proceso FFmpeg iniciado con PID: {proc.pid}")
        
    except Exception as e:
        logger.error(f"Error al iniciar FFmpeg: {str(e)}")
        raise HTTPException(500, f"Error al iniciar FFmpeg: {str(e)}")
    
    error_output = ""

    # Leer stderr en paralelo (solo se guarda el final)
    async def read_stderr():
        nonlocal error_output
        tail = b""
        try:
            while True:
                data = await proc.stderr.read(4096)
                if not data:
                    break
                tail = (tail + data)[-STDERR_TAIL_BYTES:]
        except Exception as e:
            logger.error(f"Error leyendo stderr: {str(e)}")
        if tail:
            error_output = tail.decode('utf-8', errors='ignore')
            logger.debug(f"FFmpeg stderr: {error_output}")

    async def terminate_process():
        if proc.returncode is None:
            try:
                logger.info("Terminando proceso FFmpeg...")
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("Forzando terminación de FFmpeg...")
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.error(f"Error terminando proceso: {str(e)}")

    stderr_task = asyncio.create_task(read_stderr())

    async def cleanup():
        await terminate_process()
        # Dejar que stderr termine de leerse; si no acaba, wait_for cancela la tarea
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stderr_task, timeout=5.0)

    # Leer el primer chunk antes de responder: el tiempo hasta el primer byte es el real
    # y un fallo de arranque se devuelve como error HTTP (con el 200 ya enviado no se puede).
    try:
        first_chunk = await asyncio.wait_for(
            proc.stdout.read(STREAMLINK_CHUNKSIZE),
            timeout=30.0
        )
    except asyncio.TimeoutError:
        logger.error("Timeout esperando el primer chunk de FFmpeg")
        await cleanup()
        raise HTTPException(504, "Timeout esperando el stream")
    except BaseException:
        await cleanup()
        raise

    if not first_chunk:
        await cleanup()
        logger.error(f"FFmpeg terminó sin emitir datos, código: {proc.returncode}")
        logger.error(f"Error output: {error_output}")
        raise HTTPException(500, f"FFmpeg falló: {error_output}")

    async def stream_generator():
        chunks_sent = 1
        
        try:
            yield first_chunk

            while not proc.stdout.at_eof():
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(STREAMLINK_CHUNKSIZE),
                        timeout=30.0
                    )
                    if chunk:
                        chunks_sent += 1
                        if chunks_sent % 100 == 0:
                            logger.debug(f"Enviados {chunks_sent} chunks")
                        yield chunk
                    else:
                        logger.info("Stream HLS mux terminado")
                        break
                except asyncio.TimeoutError:
                    logger.error("Timeout leyendo del stream muxeado")
                    break
                except Exception as e:
                    logger.error(f"Error leyendo chunk: {str(e)}")
                    break
                    
        except Exception as e:
            logger.error(f"Error en stream generator: {str(e)}")
            raise
        finally:
            await cleanup()
            
            duration = datetime.now() - start_time
            logger.info(f"Stream HLS mux finalizado - Duración: {duration}, Chunks enviados: {chunks_sent}")
    
    return StreamingResponse(
        stream_generator(),
        media_type='video/mp2t',
        headers={
            "Connection": "keep-alive",
            'Cache-Control': 'no-store',
            'Accept-Ranges': 'none'
        }
    )
