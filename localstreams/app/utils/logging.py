import logging

def setup_logging(log_level: str = "INFO"):
    """Configura el sistema de logging de la aplicación"""
    logging.basicConfig(
        format='{"time": "%(asctime)s", "level": "%(levelname)s", "module": "%(name)s", "message": "%(message)s"}',
        level=log_level
    )
    return logging.getLogger("LocalStreams")
