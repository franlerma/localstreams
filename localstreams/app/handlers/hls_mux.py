import asyncio
import shutil
import logging
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from config import STREAMLINK_CHUNKSIZE

router = APIRouter()
logger = logging.getLogger("LocalStreams.hls_mux")

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
    
    async def stream_generator():
        error_output = ""
        chunks_sent = 0
        
        try:
            # Leer stderr en paralelo
            async def read_stderr():
                nonlocal error_output
                try:
                    stderr_data = await proc.stderr.read()
                    if stderr_data:
                        error_output = stderr_data.decode('utf-8', errors='ignore')
                        logger.debug(f"FFmpeg stderr: {error_output}")
                except Exception as e:
                    logger.error(f"Error leyendo stderr: {str(e)}")
            
            stderr_task = asyncio.create_task(read_stderr())
            
            # Dar tiempo a FFmpeg para inicializarse
            await asyncio.sleep(3)
            
            # Verificar si el proceso sigue vivo
            if proc.returncode is not None:
                await stderr_task
                logger.error(f"FFmpeg terminó prematuramente con código: {proc.returncode}")
                logger.error(f"Error output: {error_output}")
                raise HTTPException(500, f"FFmpeg falló: {error_output}")
            
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
                    await asyncio.sleep(2)
                    break
                except Exception as e:
                    logger.error(f"Error leyendo chunk: {str(e)}")
                    break
                    
        except Exception as e:
            logger.error(f"Error en stream generator: {str(e)}")
            raise
        finally:
            # Cleanup del proceso
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
