import asyncio
import contextlib
import shutil
import logging
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from config import STREAMLINK_BINARY, STREAMLINK_CHUNKSIZE

router = APIRouter()
logger = logging.getLogger("LocalStreams.streamlink")

# Solo se conserva el final de stderr: un stream largo no debe acumular memoria
STDERR_TAIL_BYTES = 8192

@router.get("/streamlink/video")
async def stream_video(request: Request):
    """Streaming mejorado con gestión de buffers y tiempo de espera"""
    url = request.query_params.get('url')
    if not url or not url.startswith(('http://', 'https://')):
        raise HTTPException(400, "URL inválida")

    # Verificar que streamlink esté disponible
    streamlink_path = shutil.which(STREAMLINK_BINARY)
    if not streamlink_path:
        logger.error(f"Streamlink no encontrado en PATH: {STREAMLINK_BINARY}")
        raise HTTPException(500, "Streamlink no está disponible")

    start_time = datetime.now()
    logger.info(f"Iniciando stream para {url} usando {streamlink_path}")
    
    quality = request.query_params.get('quality')
    if quality and quality.endswith('p'):
        quality = quality[:-1]
    else:
        quality = 'best'

    # Comando con parámetros adicionales para mejor compatibilidad
    cmd = [
        streamlink_path,
        url,
        quality,
        '--stdout',
        '--retry-streams', '3',
        '--retry-max', '5',
        '--http-timeout', '60'
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        logger.info(f"Proceso streamlink iniciado con PID: {proc.pid}")
        
    except Exception as e:
        logger.error(f"Error al iniciar streamlink: {str(e)}")
        raise HTTPException(500, f"Error al iniciar streamlink: {str(e)}")

    error_output = ""

    # Leer stderr en paralelo para capturar errores (solo se guarda el final)
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
            logger.warning(f"Streamlink stderr: {error_output}")

    async def terminate_process():
        if proc.returncode is None:
            try:
                logger.info("Terminando proceso streamlink...")
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("Forzando terminación de streamlink...")
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
        logger.error("Timeout esperando el primer chunk de streamlink")
        await cleanup()
        raise HTTPException(504, "Timeout esperando el stream")
    except BaseException:
        await cleanup()
        raise

    if not first_chunk:
        await cleanup()
        logger.error(f"Streamlink terminó sin emitir datos, código: {proc.returncode}")
        logger.error(f"Error output: {error_output}")
        raise HTTPException(500, f"Streamlink falló: {error_output}")

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
                        logger.info("Stream terminado - chunk vacío recibido")
                        break
                except asyncio.TimeoutError:
                    logger.error("Timeout leyendo del stream")
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
            logger.info(f"Stream finalizado - Duración: {duration}, Chunks enviados: {chunks_sent}")

    return StreamingResponse(
        stream_generator(),
        media_type='video/mp4',
        headers={
            "Connection": "keep-alive",
            'Cache-Control': 'no-store',
            'Accept-Ranges': 'none'
        }
    )
