# LocalStreams 📺

LocalStreams es una aplicación FastAPI diseñada para facilitar la generación y streaming de playlists M3U de canales de TV. Este proyecto permite a los usuarios acceder fácilmente a una playlist personalizada con canales de televisión que emiten por internet, así como a través de AceStream y StreamLink, todo a través de una arquitectura de microservicios containerizada. 🌐

## Características Principales ✨

- **FastAPI** como framework web principal para alto rendimiento
- **Streaming en tiempo real** de contenido AceStream y StreamLink
- **Templates M3U dinámicos** con soporte para variables Jinja2
- **Arquitectura multi-contenedor** con Docker Compose
- **Health checks** integrados para monitoreo
- **Plugins de StreamLink** personalizados (ej: Mitele)
- **Middleware de seguridad** y gestión robusta de errores
- **Cache management** optimizado para streaming
- **Soporte para iconos** de canales (picons)

## Arquitectura 🏗️

El proyecto consta de tres servicios principales:

- **localstreams**: Aplicación FastAPI principal (puerto 15123)
- **acexy**: Proxy para AceStream (puerto 8080)
- **acestream**: Motor AceStream HTTP (puerto interno 6878)

## Instalación ⚙️

### Requisitos previos

- 🐳 Docker
- 🐙 Docker Compose
- 🛠️ Make (opcional, para comandos simplificados)

### Instalación paso a paso

1. **Clona el repositorio:**
   ```bash
   git clone https://github.com/franlerma/localstreams.git
   cd localstreams
   ```

2. **Crea los directorios de volúmenes:**
   ```bash
   make volumes-create
   # O manualmente:
   sudo mkdir -p /opt/docker/volumes/localstreams/{m3u,picon,tmp}
   ```

3. **Construye la imagen:**
   ```bash
   make build
   # O con docker directamente:
   docker build -t franlerma/localstreams .
   ```

4. **Inicia los servicios:**
   ```bash
   make up
   # O con docker-compose:
   docker compose up -d
   ```

## Comandos del Makefile 🛠️

El proyecto incluye un Makefile para simplificar las operaciones comunes:

```bash
make help              # Mostrar ayuda
make build             # Construir imagen Docker
make up                # Levantar servicios
make down              # Detener servicios
make restart           # Reiniciar servicios
make logs              # Ver logs en tiempo real
make status            # Ver estado de servicios
make shell             # Acceder al contenedor
make volumes-create    # Crear directorios de volúmenes
make clean             # Limpieza de Docker
make info              # Información del sistema
```

## Configuración 📋

### Variables de entorno principales

```bash
# Puerto de la aplicación FastAPI
ACESTREAM_APP_PORT=15123

# Configuración AceStream
ACESTREAM_PROXY_HOST=acexy
ACESTREAM_PROXY_PORT=8080
ACESTREAM_RETRY_TOTAL=10
ACESTREAM_ARGS=--live-cache-type memory

# Directorios
APP_M3U_DIR=/data/m3u
APP_LOG_LEVEL=INFO
APP_MAX_CONNECTIONS=100

# StreamLink
STREAMLINK_BINARY=streamlink
```

### Estructura de volúmenes

```
/opt/docker/volumes/localstreams/
├── m3u/          # Templates de playlists M3U
├── picon/        # Iconos de canales
└── tmp/          # Cache temporal de AceStream
```

## Uso 🖥️

### Acceso a la aplicación

Una vez iniciados los servicios, la aplicación estará disponible en:
- **Puerto principal**: http://localhost:15123
- **Health check**: http://localhost:15123/check_health

### Templates M3U dinámicos

Los templates M3U soportan Jinja2 y pueden usar variables predefinidas:

- `{{scheme}}` - Protocolo (http/https)
- `{{hostname}}` - Nombre del host
- `{{port}}` - Puerto
- `{{base_url}}` - URL base completa
- Cualquier parámetro pasado por query string

### Endpoints de streaming

#### AceStream
```
{{base_url}}/acestream/video?id={acestream_id}
```

#### StreamLink
```
{{base_url}}/streamlink/video?url={url}&quality={quality}
```

#### Picons (iconos)
```
{{base_url}}/picon/{filename}
```

### Ejemplo de template M3U

**Archivo**: `/opt/docker/volumes/localstreams/m3u/example.m3u`

```m3u
#EXTM3U
#EXTVLCOPT--http-reconnect=true

#EXTINF:-1 tvg-logo="{{base_url}}/picon/la1.png" tvg-name="LA 1 HD" tvg-id="LA1.es", La 1
{{base_url}}/streamlink/video?url=https://www.rtve.es/play/videos/directo/canales-lineales/la-1/

#EXTINF:-1 tvg-logo="{{base_url}}/picon/telecinco.png" tvg-name="Telecinco" tvg-id="TELE5.es", Telecinco
{{base_url}}/acestream/video?id=b897de3e62d7c6bee9ef1107d972f3d1075e03ff

#EXTINF:-1 tvg-logo="{{base_url}}/picon/external.png" tvg-name="Servidor Externo" tvg-id="EXT.es", Canal Externo
http://{{iptvserver}}/stream.ts
```

**Acceso con variables**:
```
http://localhost:15123/m3u/example.m3u?iptvserver=192.168.1.100:8080
```

### Plugins de StreamLink

El proyecto soporta plugins personalizados de StreamLink ubicados en `/resources/plugins/`. Actualmente incluye:

- **mitele.py**: Plugin para Mediaset España (Mitele)

## Desarrollo 🔧

### Estructura del proyecto

```
localstreams/
├── app/
│   ├── localstream.py      # Aplicación FastAPI principal
│   └── requirements.txt    # Dependencias Python
├── data/
│   └── m3u/               # Templates M3U de ejemplo
├── resources/
│   └── plugins/           # Plugins StreamLink personalizados
├── docker-compose.yml     # Configuración multi-servicio
├── Dockerfile            # Imagen de la aplicación
└── Makefile              # Comandos simplificados
```

### Desarrollo local

1. **Setup inicial**:
   ```bash
   make dev-up
   ```

2. **Ver logs**:
   ```bash
   make logs
   ```

3. **Acceso al contenedor**:
   ```bash
   make shell
   ```

### Personalización

- **Añadir plugins**: Coloca archivos `.py` en `resources/plugins/`
- **Modificar templates**: Edita archivos en `/opt/docker/volumes/localstreams/m3u/`
- **Configurar variables**: Modifica `docker-compose.yml`

## Troubleshooting 🔍

### Problemas comunes

1. **Error de permisos en volúmenes**:
   ```bash
   sudo chown -R 1001:1001 /opt/docker/volumes/localstreams/
   ```

2. **Puerto ya en uso**:
   ```bash
   make down
   # Cambiar puerto en docker-compose.yml si es necesario
   make up
   ```

3. **Problemas de red**:
   ```bash
   make logs
   # Verificar conectividad entre servicios
   ```

4. **Cache de AceStream lleno**:
   ```bash
   make volumes-clean
   ```

### Logs y monitoreo

```bash
make logs              # Logs en tiempo real
make logs-tail         # Últimas 100 líneas
make status            # Estado de servicios
```

## API Endpoints 🔌

- `GET /m3u/{filename}.m3u` - Generar playlist M3U
- `GET /acestream/video?id={id}` - Stream AceStream
- `GET /streamlink/video?url={url}` - Stream StreamLink
- `GET /picon/{filename}` - Servir iconos
- `GET /check_health` - Health check

## Licencia 📜

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Contribuir 🤝

Las contribuciones son bienvenidas. Por favor:

1. Fork del proyecto
2. Crea una rama para tu feature
3. Commit de tus cambios
4. Push a la rama
5. Abre un Pull Request

## Soporte 💬

Si encuentras problemas o tienes preguntas:
- Abre un [issue](https://github.com/franlerma/localstreams/issues)
- Revisa la documentación y logs
- Verifica la configuración de Docker y permisos
