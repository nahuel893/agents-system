# Canales: agregar un canal de mensajería detrás del puerto

Issue #22. Un **canal** es por donde una conversación llega a la plataforma y
por donde sale la respuesta: WhatsApp hoy, Telegram o un proveedor de SMS
mañana. Un canal hace tres cosas y nada más:

1. **Autenticar** una entrega entrante (Meta firma con un header HMAC; otro
   proveedor puede enviar un token secreto compartido).
2. **Normalizar** el payload en los mensajes que acepta el inbox durable, y
   después en el único turno que ejecuta el agente.
3. **Enviar** una respuesta de vuelta.

Todo lo que hay entre esos extremos es agnóstico del canal y pertenece a la
plataforma: la lectura acotada del body, la aceptación durable y el dedup, el
worker diferido, la búsqueda del participante, ejecutar el turno, registrarlo,
los reintentos y las fallas terminales. El puerto es
`src/agents_system/channels/port.py`; el canal WhatsApp incorporado
(`src/agents_system/channels/whatsapp.py`) es su primer adaptador. Agregar un
canal no edita ningún módulo de la plataforma.

---

## El puerto

```python
class ChannelAdapter(Protocol):
    def verify_inbound(self, body: bytes, headers: Headers) -> None: ...
    def read_inbound(self, body: bytes) -> InboundDelivery: ...
    def extract_turn(
        self, payload: dict[str, Any], message_id: str
    ) -> InboundTurn | None: ...
    async def send_text(self, to: str, body: str) -> None: ...
```

| Método | Corre | Contrato |
|---|---|---|
| `verify_inbound` | En el request, sobre el body crudo, después del techo de tamaño y **antes de parsear o persistir nada** | Rechaza lanzando `fastapi.HTTPException` (normalmente 403). Compara secretos en tiempo constante (`hmac.compare_digest` / `secrets.compare_digest`). |
| `read_inbound` | En el request, solo después de que `verify_inbound` pasó | Devuelve `InboundDelivery(payload, messages)`. `payload` (un dict serializable a JSON) se guarda junto a cada mensaje; `messages` es un `InboundRef(message_id, conversation_key)` por mensaje. Sin mensajes significa "reconocer, no persistir nada" (un callback de estado, un body malformado). No debe lanzar ante entradas con forma de ataque. |
| `extract_turn` | En el worker diferido, una vez por mensaje guardado | Dado el `payload` guardado y el id propio del mensaje, devuelve `InboundTurn(message_id, sender, text)`, o `None` para completarlo sin turno (un mensaje que no es texto, por ejemplo). |
| `send_text` | En el worker diferido | Envía `body` a `to` (el remitente, normalizado por el `ParticipantDirectory` de la implementación). Lanza ante una falla: el worker reintenta. La entrega es **al menos una vez** —un envío ambiguo puede repetirse—. |

Reglas en las que se apoya la plataforma:

- `message_id` es el id propio del canal para el mensaje. El inbox deduplica
  sobre él, acotado al canal (guardado como `<canal>:<id>`), así un proveedor
  que reentrega el mismo id se acepta una sola vez. Debe ser estable entre
  reentregas.
- `conversation_key` es lo que debe procesarse en orden de llegada —
  normalmente el remitente—. Nunca debe estar vacío; un mensaje sin remitente
  utilizable necesita una clave propia (WhatsApp usa `unresolved:<id>`) para
  no bloquear a nadie.
- Ninguno de los métodos de entrada puede tocar la base de datos: la
  aceptación es de la plataforma.

Un adaptador cuyo proveedor verifica la URL del webhook con un GET (Meta lo
hace) también implementa `SubscriptionHandshake.verify_subscription(params) ->
str`; para cualquier otro canal, `GET /webhook/{nombre}` responde 405.

## Registrar un canal

```python
from agents_system.channels.port import Channel
from agents_system.main import create_app

app = create_app(
    registry_factory=my_registry_factory,
    agents={"support": "support-agent"},
    grants={"support": ["read:catalog"]},
    channels={"telegram": Channel(adapter=TelegramChannel(...), runtime_id="support")},
)
```

- El canal se sirve en `POST /webhook/telegram` (y `GET` si tiene handshake).
  `/webhook` sin nombre sigue siendo el canal WhatsApp incorporado;
  `/webhook/whatsapp` lo alcanza por nombre.
- Un nombre tiene de 1 a 32 letras minúsculas, dígitos, `_` o `-`, y empieza
  con una letra. `whatsapp` está reservado. Se guarda en
  `webhook_inbox.channel`, así que hay que tratarlo como permanente una vez
  que se aceptaron mensajes con él.
- `runtime_id` nombra el runtime registrado que responde al canal. El arranque
  falla salvo que esté registrado, resuelva `untrusted_input: true` (ADR-002
  C.13: un canal de webhook entrega entrada externa no confiable) y su
  presupuesto de ejecución deje 60 segundos de margen dentro del lease del
  outbox —los mismos controles que recibe `WHATSAPP_RUNTIME_ID`—.
- Un único worker diferido sirve a todos los canales. El `thread_id` del turno
  (y el hilo del recorder) es el remitente normalizado acotado al canal
  (`telegram:+54911...`), así dos canales nunca comparten una conversación.
  Se une al checkpointer compartido siempre que el proceso construya uno
  (`WHATSAPP_CHECKPOINTER_ENABLED`, que a pesar del nombre es el interruptor
  de todo el proceso).

## La configuración pertenece al adaptador

La configuración de un canal —su token, su secreto, su cliente HTTP— son los
argumentos del constructor del adaptador (o su propia clase
`pydantic-settings` con su propio prefijo de entorno). No se agrega nada a
los `Settings` planos de la plataforma, y `config.py` nunca se edita para
agregar un canal.

El canal WhatsApp incorporado sigue la misma regla en el código:
`WhatsAppChannelSettings.from_settings(settings)` proyecta la porción de
WhatsApp (`META_WEBHOOK_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_GRAPH_API_URL`, `WHATSAPP_RUNTIME_ID`,
`WHATSAPP_CHECKPOINTER_ENABLED`) y el adaptador lee solo esa vista. Las
variables de entorno conservan sus nombres: separarlas en un modelo de
configuración anidado renombraría cada variable desplegada sin ningún
beneficio de comportamiento, así que la "separación de settings" de la que
dependía el issue original se resuelve como esta vista y no como una ruptura
de configuración. Quedan dos restos, anotados pero sin cambiar:
`META_PHONE_NUMBER_ID` y `META_ACCESS_TOKEN` están declarados pero ningún
código los lee, y `webhook_max_body_bytes` / `webhook_worker_*` son,
correctamente, agnósticos del canal.

## Límites

- **Solo texto.** `InboundTurn` lleva texto; los medios quedan fuera de
  alcance, igual que para WhatsApp.
- **Un único directorio de participantes.** El `ParticipantDirectory` de la
  implementación normaliza y resuelve el remitente de todos los canales. Un
  canal cuyas direcciones no son números de teléfono necesita un directorio
  que las entienda.
- **No es para APIs de request/respuesta.** El adaptador compatible con
  OpenAI (`/v1/chat/completions`, Open WebUI) responde en la misma respuesta
  HTTP; no tiene webhook entrante, ni traspaso durable, ni envío saliente, así
  que no es un canal en este sentido y queda donde está.

## Prueba

`tests/test_channel_port.py` define un canal falso solo en el archivo de
test, lo registra con `create_app(channels=...)` y conduce un turno completo
por la ruta real, el `accept_inbound_message` real, el worker diferido real y
un `AgentRuntime` real construido por el harness (solo el SQL del outbox y el
proveedor del modelo son en memoria). Los `tests/test_webhook.py` y
`tests/test_webhook_worker.py` sin cambios fijan el comportamiento de
WhatsApp.

> Espejo en inglés: `docs/platform/channel.md` (mantener ambos al día juntos).
