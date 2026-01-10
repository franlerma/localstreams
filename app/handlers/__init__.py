from .streamlink import router as streamlink_router
from .acestream import router as acestream_router
from .hls_mux import router as hls_router
from .brightcove import router as brightcove_router

__all__ = ['streamlink_router', 'acestream_router', 'hls_router', 'brightcove_router']
