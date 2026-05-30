import asyncio
import shutil
import logging
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from config import STREAMLINK_BINARY, STREAMLINK_CHUNKSIZE

router = APIRouter()
logger = logging.getLogger("LocalStreams.streamlink")

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

    async def stream_generator():
        error_output = ""
        chunks_sent = 0
        
        try:
            # Leer stderr en paralelo para capturar errores
            async def read_stderr():
                nonlocal error_output
                try:
                    stderr_data = await proc.stderr.read()
                    if stderr_data:
                        error_output = stderr_data.decode('utf-8', errors='ignore')
                        logger.warning(f"Streamlink stderr: {error_output}")
                except Exception as e:
                    logger.error(f"Error leyendo stderr: {str(e)}")

            # Iniciar lectura de stderr en background
            stderr_task = asyncio.create_task(read_stderr())
            
            # Esperar un poco para que streamlink se inicialice
            await asyncio.sleep(2)
            
            # Verificar si el proceso sigue vivo
            if proc.returncode is not None:
                await stderr_task
                logger.error(f"Streamlink terminó prematuramente con código: {proc.returncode}")
                logger.error(f"Error output: {error_output}")
                raise HTTPException(500, f"Streamlink falló: {error_output}")

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
