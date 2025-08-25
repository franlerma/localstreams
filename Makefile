# Makefile para localstreams
.PHONY: help build up down restart logs status clean volumes-create volumes-clean shell clean-old-images

# Variables
MAKEFILE_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
COMPOSE_FILE = $(MAKEFILE_DIR)docker-compose.yml
SERVICE_NAME = localstreams
IMAGE_NAME = franlerma/localstreams

# Ayuda por defecto
help: ## Mostrar esta ayuda
	@echo "Comandos disponibles para localstreams:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

# Construcción de imagen
build: clean-old-images ## Construir la imagen Docker (limpia imágenes viejas primero)
	cd $(MAKEFILE_DIR) && docker build -t $(IMAGE_NAME) .

clean-old-images: ## Limpiar imágenes viejas de localstreams
	@echo "Limpiando imágenes viejas de $(IMAGE_NAME)..."
	@docker images $(IMAGE_NAME) --format "table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}" || true
	@docker rmi $(docker images $(IMAGE_NAME) -q) 2>/dev/null || echo "No hay imágenes viejas que limpiar"
	@echo "Limpieza completada"

# Docker Compose
up: ## Levantar los servicios con docker compose
	docker compose -f $(COMPOSE_FILE) up -d

down: ## Detener y eliminar los servicios
	docker compose -f $(COMPOSE_FILE) down

restart: down up ## Reiniciar los servicios (down + up)

# Logs y monitoreo
logs: ## Ver logs del servicio
	docker compose -f $(COMPOSE_FILE) logs -f $(SERVICE_NAME)

logs-tail: ## Ver últimas 100 líneas de logs
	docker compose -f $(COMPOSE_FILE) logs --tail=100 $(SERVICE_NAME)

status: ## Ver estado del servicio
	docker compose -f $(COMPOSE_FILE) ps

# Acceso al contenedor
shell: ## Acceder al shell del contenedor
	docker compose -f $(COMPOSE_FILE) exec $(SERVICE_NAME) /bin/bash

shell-root: ## Acceder al shell como root
	docker compose -f $(COMPOSE_FILE) exec --user root $(SERVICE_NAME) /bin/bash

# Gestión de volúmenes
volumes-create: ## Crear directorios de volúmenes
	@echo "Creando directorios de volúmenes..."
	sudo mkdir -p /opt/docker/volumes/localstreams/m3u
	sudo mkdir -p /opt/docker/volumes/localstreams/picon
	sudo mkdir -p /opt/docker/volumes/localstreams/tmp
	@echo "Directorios creados en /opt/docker/volumes/localstreams/"

volumes-check: ## Verificar existencia de volúmenes
	@echo "Verificando volúmenes:"
	@ls -la /opt/docker/volumes/localstreams/ 2>/dev/null || echo "Los volúmenes no existen. Ejecuta 'make volumes-create'"

volumes-clean: ## Limpiar contenido de volúmenes (¡CUIDADO!)
	@echo "¿Estás seguro de que quieres limpiar los volúmenes? [y/N]" && read ans && [ $${ans:-N} = y ]
	sudo rm -rf /opt/docker/volumes/localstreams/tmp/*
	@echo "Cache temporal limpiado"

# Limpieza
clean: down ## Detener servicios y limpiar contenedores/imágenes no utilizados
	docker system prune -f

clean-all: down clean-old-images ## Limpieza completa (contenedores, imágenes, volúmenes)
	docker system prune -a -f
	docker volume prune -f

# Desarrollo
dev-up: volumes-create up ## Setup completo para desarrollo
	@echo "Servicios levantados. Puertos disponibles:"
	@echo "  - 15123: Puerto principal"
	@echo "  - 8621: Puerto secundario"

# Actualizaciones
pull: ## Actualizar imagen desde registry
	docker pull $(IMAGE_NAME)

update: pull down up ## Actualizar imagen y reiniciar servicios

# Backup
backup-config: ## Backup de configuraciones
	@mkdir -p $(MAKEFILE_DIR)backups
	sudo tar -czf $(MAKEFILE_DIR)backups/localstreams-config-$(shell date +%Y%m%d-%H%M%S).tar.gz /opt/docker/volumes/localstreams/m3u /opt/docker/volumes/localstreams/picon

# Información del sistema
info: ## Mostrar información del sistema
	@echo "=== Información del sistema ==="
	@echo "Docker version: $(shell docker --version)"
	@echo "Docker Compose version: $(shell docker compose --version)"
	@echo "Imagen: $(IMAGE_NAME)"
	@echo "Estado del servicio:"
	@docker compose -f $(COMPOSE_FILE) ps 2>/dev/null || echo "Servicios no iniciados"
	@echo "Volúmenes:"
	@ls -la /opt/docker/volumes/localstreams/ 2>/dev/null || echo "Volúmenes no creados"