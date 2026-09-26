# Despliegue en Producción y Aprovisionamiento del Servidor

Este documento describe la vía operativa de despliegue para `agents-system`, cubriendo topologías contenerizadas y con systemd, migraciones automáticas en cada deploy, contratos de chequeo de salud, registros de logs, actualizaciones, rollbacks y dimensionamiento de capacidad.

Esta especificación implementa el issue #12 (rastreado como D-036) siguiendo las decisiones de topología de ejecución de [ADR-001](../architecture_es/adr-001-runtime-topology.md).

---

## 1. Topología de Ejecución y Arquitectura

Según ADR-001, el despliegue en producción se organiza en dos etapas:

- **Etapa A (Lanzamiento — 10 conversaciones concurrentes):** Proceso de servidor de aplicación único. Un bucle de eventos asíncrono gestiona con holgura las llamadas de LLM limitadas por I/O. El proceso de servidor aloja:
  - La aplicación HTTP FastAPI (`/health`, `/ready`, `/webhook`, `/v1/*`, `/metrics`).
  - El vaciador en proceso de `AuditSink` (worker en segundo plano que escribe eventos de auditoría en PostgreSQL).
  - El worker en segundo plano `DeferredWebhookWorker` (bucle de sondeo que procesa mensajes entrantes durables de WhatsApp desde la tabla `outbox_work`).
  - Dos pools de conexiones asíncronas SQLAlchemy: base de datos operativa y base de datos BI de solo lectura opcional.
- **Etapa B (Objetivo — 100 conversaciones concurrentes):** Escalamiento horizontal con múltiples procesos de servidor detrás de un proxy inverso o balanceador de carga, utilizando un proveedor de embeddings remoto (como OpenAI o un microservicio dedicado) para evitar copias duplicadas del modelo BGE-M3 en memoria.

### Orden de Inicio de Procesos

Los servicios deben iniciar estrictamente en esta secuencia de dependencias:
1. **PostgreSQL:** Inicia y reporta estado saludable mediante `pg_isready`.
2. **Redis:** Inicia y reporta estado saludable mediante `redis-cli ping` (requiere Redis 8+ por el motor de búsqueda RediSearch).
3. **Migraciones de Base de Datos:** Tarea puntual que ejecuta `alembic upgrade head`. Si las migraciones fallan, el despliegue aborta de inmediato con código de salida distinto de cero.
4. **Servidor de Aplicación (App):** Inicia únicamente después de verificar que la base de datos, Redis y las migraciones están completos y saludables.

---

## 2. Dimensionamiento de Recursos y Pools de Conexiones

Los pools de conexiones a la base de datos se instancian por proceso. Para evitar agotar `max_connections` en PostgreSQL, la configuración debe respetar las reglas de dimensionamiento establecidas en ADR-001.

### Fórmula del Límite Superior

$$\text{total de conexiones} = \text{procesos} \times \sum (\text{pool\_size} + \text{max\_overflow}) + \text{procesos concurrentes de sync} < \text{max\_connections} - 3$$

*(3 conexiones reservadas para el superusuario de PostgreSQL).*

### Restricción del Pool Operativo en el Proceso

Dentro de cada proceso de servidor, el pool operativo debe cumplir:

$$\text{database\_pool\_size} + \text{database\_max\_overflow} \ge \text{max\_concurrent\_turns} + 2$$

Cada turno conversacional admitido retiene como máximo una conexión durante sus consultas. El margen de 2 reserva conexiones dedicadas para el vaciador de `AuditSink` y el worker `DeferredWebhookWorker` que se ejecutan en el mismo proceso.

### Valores Predeterminados (Etapa A)

| Parámetro | Predeterminado | Propósito |
|---|---|---|
| `MAX_CONCURRENT_TURNS` | `10` | Límite de turnos conversacionales concurrentes en todo el proceso |
| `DATABASE_POOL_SIZE` | `5` | Tamaño base del pool operativo |
| `DATABASE_MAX_OVERFLOW` | `10` | Conexiones máximas de desbordamiento operativo (capacidad total 15) |
| `DATABASE_POOL_TIMEOUT_S` | `30` | Tiempo de espera para obtener una conexión del pool |
| `BI_POOL_SIZE` / `BI_MAX_OVERFLOW` | `3` / `0` | Pool dedicado para reportes de solo lectura |
| `SQL_POOL_SIZE` / `SQL_MAX_OVERFLOW` | `2` / `0` | Pool dedicado para la herramienta de consulta SQL |
| `max_connections` en PostgreSQL | `100` | Configuración mínima recomendada en PostgreSQL |

---

## 3. Métodos de Despliegue

Se proporcionan dos opciones de despliegue:
1. **Docker Compose (`docker-compose.prod.yml`):** Recomendado para infraestructura basada en contenedores y entornos CI/CD automatizados.
2. **Unidades de Systemd (`deploy/systemd/`):** Recomendado para máquinas virtuales Linux donde los servicios se gestionan directamente a nivel de sistema operativo.

### Opción A: Despliegue con Docker Compose

El stack está definido en `docker-compose.prod.yml` con cuatro servicios coordinados: `postgres`, `redis`, `migrate` y `app`.

#### Inyección de Entorno y Secretos
La configuración y los secretos se cargan desde `.env.prod` (o una ruta personalizada mediante `ENV_FILE`, por defecto `${ENV_FILE:-.env.prod}`) y se inyectan en los servicios `migrate` y `app` mediante `env_file`.

#### Línea Base de Seguridad
Todos los puertos externos se vinculan a `127.0.0.1`. Nunca expongas puertos de bases de datos o de la aplicación directamente a `0.0.0.0`. Un proxy inverso en el host (Nginx, Caddy, Cloudflare Tunnel, AWS ALB) debe encargarse de la terminación TLS y enrutar el tráfico externo.

#### Pasos de Despliegue

1. **Configurar el Entorno:**
   ```bash
   cp .env.example .env.prod
   chmod 600 .env.prod
   # Completar secretos (POSTGRES_PASSWORD, META_WEBHOOK_SECRET, API keys de LLMs)
   ```

2. **Validar la Configuración de Compose:**
   ```bash
   docker compose -f docker-compose.prod.yml config
   ```

3. **Compilar y Desplegar el Stack:**
   ```bash
   docker compose -f docker-compose.prod.yml up -d --build
   ```

4. **Verificar la Salud del Despliegue:**
   ```bash
   docker compose -f docker-compose.prod.yml ps
   scripts/healthcheck.sh all
   ```

### Opción B: Despliegue con Unidades de Systemd

Las unidades de systemd se encuentran en `deploy/systemd/`:
- `agents-system-migrate.service`: Unidad `Type=oneshot` que ejecuta las migraciones de base de datos antes de que inicie la aplicación.
- `agents-system-app.service`: Unidad `Type=simple` que gestiona el servidor Uvicorn con reinicio automático ante caídas.

Ambas unidades se ejecutan bajo el usuario y grupo dedicados y sin privilegios `agents-system`, configuran `PATH` para incluir `/opt/agents-system/.venv/bin`, y cargan variables de entorno desde `EnvironmentFile=/etc/agents-system/agents-system.env` (sin el prefijo `-`, garantizando que la ausencia del archivo de configuración falle de inmediato en el arranque).

#### Pasos de Instalación

1. **Crear Usuario de Servicio y Directorios:**
   ```bash
   sudo useradd -r -s /bin/false -d /opt/agents-system agents-system
   sudo mkdir -p /opt/agents-system /etc/agents-system /var/log/agents-system
   sudo chown -R agents-system:agents-system /opt/agents-system /var/log/agents-system
   ```

2. **Instalar Código y Entorno Virtual:**
   ```bash
   sudo -u agents-system uv sync --frozen --no-dev
   ```

3. **Configurar el Archivo de Entorno:**
   ```bash
   sudo cp deploy/systemd/agents-system.env.example /etc/agents-system/agents-system.env
   sudo chown root:agents-system /etc/agents-system/agents-system.env
   sudo chmod 640 /etc/agents-system/agents-system.env
   # Editar /etc/agents-system/agents-system.env con credenciales reales de producción
   ```

4. **Instalar y Habilitar Unidades:**
   ```bash
   sudo cp deploy/systemd/agents-system-migrate.service /etc/systemd/system/
   sudo cp deploy/systemd/agents-system-app.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable agents-system-app.service
   sudo systemctl start agents-system-app.service
   ```

### Punto de Entrada de la Aplicación y Resolución del Registro de Herramientas (`REGISTRY_FACTORY`)

Los despliegues en producción ejecutan el servidor ASGI Uvicorn apuntando a `agents_system.server:app`. La fábrica de la aplicación resuelve las herramientas de los conectores según las siguientes reglas:

1. **Cero Agentes Registrados:** Si `AGENT_REGISTRATIONS` está vacío o no configurado, la aplicación arranca limpiamente con un cache de runtimes vacío (atendiendo `/health` e ingesta de webhooks).
2. **Agentes Registrados Requieren `REGISTRY_FACTORY`:** Cuando `AGENT_REGISTRATIONS` (o `create_app(agents=...)`) especifica uno o más agentes, el despliegue **debe** configurar `REGISTRY_FACTORY` apuntando a un ejecutable (formato: `modulo.submodulo:nombre_funcion`) que retorne un `ToolRegistry` equipando todas las herramientas declaradas por dichos agentes.
3. **Registro por Defecto con Fallo Cerrado:** Cuando `REGISTRY_FACTORY` no está configurado, se utiliza `default_registry_factory`. Dado que `default_registry_factory` solo registra herramientas base de la plataforma (`order_writer`, `knowledge_retrieval`, `conversation_summarizer`, `escalation_notifier`, `run_report`) y no puede equipar roles predefinidos que requieren herramientas específicas (como `session_state`, `catalog_search`, `client_lookup` o `message_sender`), el arranque aborta inmediatamente con un `DefinitionError` que detalla las herramientas faltantes.
4. **Implementación de Referencia:** Consultá `examples/demo/app.py` para ver cómo un despliegue real conecta los conectores a su almacenamiento y modelo de lenguaje mediante `build_live_registry_factory`.

---

## 4. Chequeos de Salud y Políticas de Reinicio

Según el issue #109 y ADR-001, el monitoreo de salud utiliza dos endpoints distintos con semántica operativa precisa:

### `/health` — Sonda de Vitalidad (Liveness)
- **Contrato:** Devuelve siempre HTTP 200 con diagnóstico estructurado de componentes mientras el proceso Python y el bucle de eventos sigan respondiendo.
- **Consumidor:** Gestores de procesos (chequeo de salud del demonio Docker en `docker-compose.prod.yml`, watchdog de systemd, sondas liveness de Kubernetes).
- **Garantía de Reinicio:** Si un proveedor externo de modelos (Anthropic, Groq, OpenAI) se degrada o cae, el estado reportado en el JSON refleja la degradación, pero **nunca** devuelve un error HTTP 5xx. Los supervisores no reiniciarán una aplicación sana cuando las APIs externas sufran cortes transitorios.

### `/ready` — Sonda de Disponibilidad (Readiness)
- **Contrato:**
  - Devuelve HTTP 200 cuando PostgreSQL, Redis y el proveedor de modelos están saludables y no hay backlog pendiente desatendido.
  - Devuelve HTTP 503 cuando alguna dependencia central está caída, el proveedor de modelos es inalcanzable o existen tareas en la outbox con leases vencidos sin un worker activo para procesarlas.
- **Consumidor:** Balanceadores de carga, proxies inversos (por ejemplo, Nginx, Traefik, AWS ALB) y sondas readiness de Kubernetes para el enrutamiento y control de tráfico.
- **Garantía de Tráfico:** El enrutamiento de tráfico hacia la instancia se pausa durante la degradación de dependencias sin matar ni reiniciar el proceso. `/ready` pertenece exclusivamente a las capas de ruteo, nunca a los supervisores de procesos o contenedores.

### Script de Chequeo de Salud
Ejecutá el script de prueba automatizado:
```bash
scripts/healthcheck.sh readiness   # Prueba /ready (código de salida 0 en 200, 1 en 503)
scripts/healthcheck.sh liveness    # Prueba /health (código de salida 0 en 200, 1 en error de conexión)
scripts/healthcheck.sh all         # Prueba ambos endpoints
```

---

## 5. Migraciones en el Despliegue

Las migraciones de base de datos son administradas mediante Alembic y se ejecutan automáticamente en cada despliegue antes de que inicie la aplicación.

- **Resolución de URL:** `DATABASE_URL` se lee directamente desde el entorno en `alembic/env.py`. El archivo versionado `alembic.ini` no contiene credenciales.
- **Verificación en seco (Dry-run):**
  ```bash
  DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/dbname" uv run alembic upgrade head --sql
  ```
- **Ejecución Automatizada en Despliegue:**
  En Docker Compose, el servicio `migrate` corre `alembic upgrade head`. El servicio `app` declara:
  ```yaml
  depends_on:
    migrate:
      condition: service_completed_successfully
  ```
  Si un script de migración falla o encuentra un conflicto de locks, el contenedor finaliza con error y Docker Compose detiene el despliegue antes de arrancar `app`.

---

## 6. Actualizaciones y Rollbacks

### Secuencia de Actualización Sin Tiempo de Inactividad

1. **Obtener la imagen actualizada o el tag de versión de Git:**
   ```bash
   git fetch origin && git checkout vX.Y.Z
   ```
2. **Ejecutar Migraciones:**
   Las migraciones deben ser retrocompatibles (patrón expand-contract). Los cambios de esquema no deben romper la versión en ejecución antes de que inicie la nueva.
3. **Reiniciar / Recrear el Servicio de Aplicación:**
   ```bash
   # Docker Compose
   docker compose -f docker-compose.prod.yml up -d --no-deps --build app

   # Systemd
   sudo systemctl restart agents-system-app.service
   ```
4. **Verificar la Salud del Servicio:**
   ```bash
   scripts/healthcheck.sh readiness
   ```

### Procedimiento de Rollback

Si una actualización introduce regresiones:

1. **Revertir Código / Contenedor:**
   ```bash
   # Docker Compose: volver a la imagen o commit anterior
   docker compose -f docker-compose.prod.yml up -d --no-deps app

   # Systemd: checkout del tag anterior y reinicio
   git checkout <tag-anterior>
   sudo systemctl restart agents-system-app.service
   ```
2. **Revertir Esquema de Base de Datos (de ser necesario):**
   ```bash
   uv run alembic downgrade -1
   # O revertir a una revisión específica:
   uv run alembic downgrade <id_revision>
   ```
   *Nota: Solo aplicar downgrade si la migración fue diseñada como reversible y no eliminó datos irreversibles.*

---

## 7. Registro de Logs y Retención

- **Formato:** Líneas JSON estructuradas mediante `structlog`. Cada registro incluye marcas temporales ISO, nivel de log, nombre del evento y atributos contextuales (`request_id`, `thread_id`, `correlation_id`).
- **Destino:** Salida estándar (`stdout`) / error estándar (`stderr`), capturados por el driver de Docker o `journald` en systemd.
- **Sin Secretos en Logs:** Credenciales, tokens de autenticación y datos personales de usuarios (PII) son enmascarados mediante políticas estrictas por defecto (`Redactor` y `RequestIdMiddleware`).
- **Política de Retención:**
  - **Docker Compose:** Configurado con el driver `json-file`, rotando a los `100MB` por archivo con retención de `5` archivos (máximo 500MB por servicio):
    ```yaml
    logging:
      driver: "json-file"
      options:
        max-size: "100m"
        max-file: "5"
    ```
  - **Systemd / Journald:** Administrado en `/etc/systemd/journald.conf`:
    ```ini
    [Journal]
    SystemMaxUse=2G
    SystemKeepFree=1G
    MaxRetentionSec=30day
    ```
