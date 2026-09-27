# Sandbox con bubblewrap — requisitos del host (ADR-002 C.14)

Toda ejecución T3 en el host (`use_term`, y cada entrada declarada de
`command_tools`) corre dentro de un sandbox `bwrap`, con límites de
recursos aplicados vía `prlimit`. Esto no es opcional y no tiene una vía de
respaldo sin sandbox: un host que no cumple los requisitos de abajo obtiene
una herramienta que se niega en cada llamada, nunca una herramienta que
corre sin sandbox en silencio (`connectors/operator.py::_run_argv`,
ADR-002 C.14).

## Requisitos del host

- **`bwrap` (bubblewrap)** instalado y disponible en `$PATH`.
- **`prlimit`** (parte de `util-linux`, presente en prácticamente toda
  distribución Linux convencional) instalado y disponible en `$PATH`.
  Aplica `RLIMIT_AS`/`RLIMIT_CPU` al proceso sandboxeado — ver "Por qué
  `prlimit` y no `preexec_fn`" más abajo.
- **Espacios de nombres de usuario sin privilegios permitidos** para el
  usuario con el que corre la plataforma. Ubuntu 23.10+ (incluyendo 24.04
  LTS) los restringe por defecto vía AppArmor
  (`kernel.apparmor_restrict_unprivileged_userns=1`), y la restricción se
  dispara específicamente por la postura de "sin red" por defecto de
  `bwrap` (la configuración de loopback de `--unshare-net` necesita una
  capacidad que la restricción deniega) — así que un host que por lo demás
  parece correcto fallará en cada llamada sandboxeada hasta resolver esto.
  Dos formas de corregirlo, en orden de preferencia:
  1. Cargar el perfil de AppArmor que Debian/Ubuntu distribuyen
     exactamente para este caso:
     `sudo apt install apparmor-profiles apparmor-utils`, y luego instalar
     y cargar `/usr/share/apparmor/extra-profiles/bwrap-userns-restrict`.
     Esto preserva la restricción para cualquier otro programa del host.
  2. Desactivar la restricción directamente:
     `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`
     (persistirlo vía un archivo bajo `/etc/sysctl.d/`). Más amplio que la
     opción 1; aceptable en un host dedicado a correr esta plataforma,
     menos aceptable en uno compartido.

  Esto es exactamente lo que aplica `.github/workflows/ci.yml` para los
  runners `ubuntu-latest` de GitHub, que tienen la misma restricción (ver
  los jobs `ci` y `sandbox-integration`).
- Las distribuciones sin esta restricción de AppArmor (la mayoría de Linux
  no-Ubuntu, y las versiones de Ubuntu anteriores a 23.10) no necesitan
  configuración adicional — `bwrap` funciona de fábrica para un usuario sin
  privilegios con `CONFIG_USER_NS` habilitado, que es el valor por defecto
  del kernel en casi todas partes.

## Por qué `prlimit` y no `preexec_fn`

Una versión anterior aplicaba `RLIMIT_AS`/`RLIMIT_CPU` vía `preexec_fn`
sobre la llamada a `asyncio.create_subprocess_exec` que lanzaba `bwrap`. La
propia documentación de Python llama insegura a `preexec_fn` en presencia
de hilos: corre en el hijo entre `fork()` y `exec()`, y si otro hilo de
este proceso (asyncio, y en producción, un proceso alojado en un servidor)
mantenía un lock en el momento del fork, el hijo puede quedar bloqueado
para siempre sosteniendo un lock que nada liberará jamás. `prlimit COMMAND`
establece los límites sobre sí mismo antes de hacer `exec` del comando
real, así que aplicarlo es solo otro `execvp`, nunca un fork desde dentro
del proceso de la plataforma en ejecución — por eso `prlimit` es un
requisito obligatorio junto a `bwrap`, no un extra opcional.

## Verificación manual

```bash
which bwrap prlimit
prlimit --as=536870912 --cpu=10 -- \
  bwrap --unshare-all --die-with-parent \
    --ro-bind /usr /usr --proc /proc --dev /dev -- /usr/bin/true \
  && echo "sandbox OK"
```

Si esto falla con un error de espacio de nombres o de permisos (no un error
de binario faltante), aplicar una de las dos correcciones de AppArmor de
arriba.

## Timeouts y cancelación

Dos temporizadores pueden detener un comando sandboxeado.
`TerminalPolicy.timeout_s` es el temporizador de terminación propio del
sandbox. El `tool_call_timeout_s` del interceptor (`agent/graph.py`) acota
toda la llamada a la herramienta desde afuera y normalmente es el más
ajustado de los dos. Una tarea cancelada o un apagado detienen la llamada
de la misma forma que el timeout externo.

Sea cual sea el primero, `_run_argv` mata todo el grupo de procesos del
comando con `SIGKILL` y lo cosecha (reap) antes de que la llamada devuelva
su resultado o de que la cancelación se propague. Una cancelación siempre
se vuelve a lanzar, nunca se convierte en un resultado. Ese grupo
contiene todo lo que ejecuta el sandbox: el lanzador (`prlimit`, que
luego pasa a ser el `bwrap` externo), el init del espacio de nombres de
PID de bwrap y el comando. Así, una sola terminación los alcanza a todos
en cualquier momento, incluso mientras bwrap todavía está levantando el
sandbox. Un proceso que el comando pase a una sesión propia sale del
grupo pero no del espacio de nombres de PID, y el kernel mata todo lo que
haya en el espacio de nombres cuando su init desaparece. Antes de #105 la
terminación solo corría en el manejador de timeout propio de
`_run_argv`, así que cuando `tool_call_timeout_s` se disparaba primero el
comando seguía corriendo en el host después de que al agente ya se le
había informado el timeout.

La cosecha drena la salida que todavía quede en los pipes y tiene un tope
de 5 segundos. Si se agota, se registra `operator.reap_incomplete` con
nivel error y la llamada termina igual.

### Por qué el sandbox corre sin `--new-session`

`--new-session` hacía que el init del espacio de nombres de bwrap llamara
a `setsid()`, lo que sacaba al init y al comando del grupo que mata
`_run_argv`. A partir de ahí solo morían mediante `--die-with-parent`, y
bwrap lo activa en ese init recién después del `setsid()`. Una
terminación que caía entre ambos dejaba huérfano todo el sandbox,
comando incluido, y la cosecha agotaba su tope de 5 segundos esperando
los pipes que los huérfanos seguían teniendo abiertos. Un test que mata en
ese momento filtraba el sandbox en unos 4 de cada 10 intentos antes de
quitar la opción.

Lo que `--new-session` evitaba sigue cerrado. `_run_argv` inicia el
lanzador con `start_new_session=True`, una sesión nueva sin terminal de
control que hereda todo lo que corre en el sandbox. Por eso un comando
sandboxeado no puede abrir la terminal de quien lo llama para inyectarle
entrada con `TIOCSTI` (CVE-2017-5226).
`test_a_sandboxed_command_cannot_open_the_callers_terminal` lo prueba
desde un llamador que sí tiene terminal.

La contrapartida es que ahora el sandbox comparte grupo de procesos con
su propio lanzador, así que puede enviarle señales, por ejemplo con
`kill(0, …)`. Eso solo puede terminar o frenar su propia llamada: los
timeouts siguen matando al grupo, y el proceso de la plataforma no está
en él.

## Qué significa `sandbox_unavailable`

`_run_argv` verifica la disponibilidad de `bwrap` y de `prlimit` en
**cada** llamada y falla en modo cerrado — nunca recae en ejecutar un
comando sin sandbox, ni sandboxeado pero sin límites de recursos
impuestos. Un resultado de herramienta con
`error_kind: "sandbox_unavailable"` significa que uno de esos dos binarios
falta, o que el sandbox no pudo iniciarse (lo más común es la restricción
de espacio de nombres de usuario de arriba). La línea de log de structlog
de nivel warning que lo acompaña (`operator.sandbox_unavailable`, o para
`command_tools`, el mismo evento bajo ese conector) indica qué binario y
qué programa fue rechazado.

## Señal en el arranque

`build_terminal_connector` y `command_tools.build_command_tool_connector`
verifican la disponibilidad una vez, en el momento de construcción — que
coincide con el arranque de la aplicación, ya que los runtimes se
construyen una sola vez al iniciar (`lifespan` de `main.py`), no por
solicitud. Si falta alguno de los dos binarios, registran una línea de
nivel **error**: `operator.sandbox_unavailable_at_boot`, indicando el
contexto de rol/herramienta y qué binario falta. Esto **no** detiene el
arranque del despliegue (un rol no relacionado no debería caerse porque el
operador/command tools de otro rol esté mal configurado) — observar los
logs de arranque de la aplicación para esta línea en vez de asumir que un
arranque limpio significa que el sandbox funciona; la aplicación real
sigue ocurriendo en cada llamada, sin importar si esta línea fue leída o
no.

## Delimitación del espacio de trabajo

`TerminalPolicy.root` — el directorio montado en **lectura-escritura**
dentro de cada sandbox — debe ser un espacio de trabajo dedicado, creado
para ese propósito. La construcción rechaza un `root` que resuelva a la
raíz del sistema de archivos, `/home`, `$HOME`, `/root`, `/run`,
`/var/run`, o al directorio de trabajo actual del propio proceso
(`connectors/operator.py::TerminalPolicy.__post_init__`, seguimiento de
revisión de ADR-002 C.14). `command_tools` nunca necesita un `root`
configurado por el despliegue: cada llamada obtiene su propio directorio
de trabajo temporal, nuevo y de un solo uso, creado y eliminado alrededor
de esa llamada.
