# ADR-012 — Ejecución de código confinada, y la regla de entrada no confiable

**Estado:** Propuesto · **Fecha:** 2026-10-08 · **Enmienda:** ADR-002 C.11 (solo una nota de alcance), ADR-003 R4 (solo una nota de alcance) · **Issue:** #284

## Resumen

Un agente que atiende a usuarios finales por un canal (`untrusted_input: true`) no puede ejecutar con el kit el código que escribe: `use_term` es T3, que R4 prohíbe a ese agente, y una herramienta de comando ejecuta solo un argv fijo con parámetros verificados. Por eso, quienes adoptan el kit y necesitan análisis de datos por usuario final construyen sus propios sandboxes, fuera de los permisos, la auditoría y los límites del kit. Este ADR propone una nueva familia de herramientas, la **ejecución de código confinada**: una herramienta, `run_code`, que ejecuta Python o un script de shell dentro de una caja que no contiene ningún secreto, no alcanza ninguna red y ve exactamente un directorio, el espacio de trabajo del sujeto del turno. Su permiso, `compute:workspace`, pertenece a una nueva familia de primer nivel `Compute` en **T2**, porque lo que el código puede alcanzar lo fija la caja y no el modelo. R4 y la prohibición de T3 siguen siendo absolutas. La caja es un puerto; la fase 1 entrega una caja bubblewrap reforzada (espacios de nombres, una lista permitida de seccomp, límites de cgroup v2, sin espacios de nombres de usuario anidados), y la fase 2 agrega una caja gVisor detrás del mismo puerto y de la misma suite de conformidad. El agente la solicita, quien despliega la otorga y la configura, la salida vuelve como un bloque de datos no confiables, y cada ejecución se audita.

## Contexto

**Qué ejecuta código hoy.** Dos familias de herramientas, ambas sobre un mismo punto de paso, `_run_argv` (`src/agents_system/connectors/operator.py:537-669`):

- `use_term` y `read_file`, de nivel T3 bajo `exec:command` y `read:files` (`operator.py:1011-1053`). `use_term` verifica solo `argv[0]` contra una lista permitida (`operator.py:695-703`) y monta en lectura y escritura un único `TerminalPolicy.root` elegido por el despliegue para cada llamada (`operator.py:152-163`), compartido por todos los usuarios del runtime.
- Herramientas de comando declarativas: un argv fijo cuyos parámetros se verifican contra un patrón (`src/agents_system/connectors/command_tools.py:145-217`), cada llamada en un directorio temporal nuevo que se borra después (`command_tools.py:198-215`), limitadas a T2 (`src/agents_system/harness/loader.py:659-671`).

**La caja que comparten.** `_bwrap_argv` ejecuta `bwrap --unshare-all --die-with-parent`, monta `/usr`, `/bin`, `/sbin`, `/lib` y `/lib64` en solo lectura (`_SYSTEM_RO_BINDS`, `operator.py:259`) y una lista permitida de siete entradas de `/etc` (`_SYSTEM_ETC_BINDS`, `operator.py:281-289`), monta su propio `/proc`, `/dev` y un tmpfs en `/tmp`, monta la raíz en lectura y escritura y reduce el entorno a cuatro variables fijas (`operator.py:384-417`, `:221-235`). `SandboxPolicy` no tiene red por defecto y rechaza montar el directorio de secretos, `/`, `/home`, `$HOME`, `/etc` y `/run` (`operator.py:87-137`). Los límites son `RLIMIT_AS` y `RLIMIT_CPU` mediante `prlimit` (`operator.py:440-458`), un tiempo máximo de reloj y un tope de salida por flujo (`operator.py:173-181`); un vencimiento o una cancelación mata todo el grupo de procesos (`operator.py:644-661`). Si falta `bwrap` o `prlimit`, se rechaza cada llamada y nunca se ejecuta sin sandbox (`operator.py:566-583`). Los requisitos del host están en `docs/operations_es/sandbox-bwrap.md:11-46`.

**Lo que esa caja no tiene.** Ningún filtro seccomp, ningún cgroup, por lo tanto ningún límite de cantidad de procesos ni contabilidad de memoria más allá del espacio de direcciones, ningún límite de tamaño de archivo, ningún tope de tamaño para `/tmp`. Medido en un host Linux 6.18 con bubblewrap 0.11.2: con las opciones actuales, un proceso dentro del sandbox puede crear un espacio de nombres de usuario anidado y ser uid 0 dentro de él (`unshare -U -r id -u` imprime `0`); al agregar `--unshare-user --disable-userns`, la misma llamada falla. `--disable-userns` requiere un `--unshare-user` explícito, porque `--unshare-all` solo *intenta* el espacio de nombres de usuario ([manual de bwrap](https://raw.githubusercontent.com/containers/bubblewrap/main/bwrap.xml)).

**La regla.** ADR-002 C.11 hizo mutuamente excluyentes `untrusted_input` y `exec:*`; ADR-003 R4 la generalizó a toda clase T3 (`docs/architecture_es/adr-003-permission-hierarchy.md:36`). Se aplica tres veces: al cargar (`loader.py:3064-3092`), al otorgar en `build_runtime` (`src/agents_system/harness/factory.py:625-635`) y al inyectar (`src/agents_system/harness/injector.py:120-128`). Su motivo sigue en pie: una instrucción sembrada en un mensaje no debe convertirse en código que se ejecuta en el host.

**Sobre qué construir.** El sujeto del turno y un handle acotado a (id de runtime, clave del sujeto) que recibe una herramienta `subject_scoped` y que el modelo nunca ve (`src/agents_system/harness/registry.py:69-74`, `src/agents_system/harness/interceptor.py:230-250`, `src/agents_system/scope.py:20-35`); el registro de datos por sujeto con exportación, borrado y retención (`src/agents_system/subject_data.py:42-59`, `:80`); los bloques de datos no confiables con un delimitador aleatorio por turno y topes de tamaño (`src/agents_system/agent/untrusted.py:23-61`, `:84-100`), que hoy llegan al modelo solo junto al prompt de sistema (`src/agents_system/agent/graph.py:478-483`), mientras que la salida de una herramienta todavía entra como un mensaje de herramienta plano (`graph.py:676-684`); los límites de ejecución que eleva el despliegue, acotados por el lease del outbox (`src/agents_system/execution_limits.py:1-14`, `:51-53`); y los campos de auditoría `actor` y `subject` (`src/agents_system/audit/events.py:27-28`). Un precedente: ADR-007 puso en T2 el SQL que escribe el modelo, porque su alcance lo acota la base de datos, no el modelo (`docs/architecture_es/adr-007-read-only-sql-tool.md:55-60`).

**No existe un mecanismo de aprobación.** `autonomy: confirm` se analiza y se verifica su monotonía (`loader.py:3668`, `:3850`), y nada fuera del cargador lo lee al momento de la llamada.

**Numeración.** ADR-006 está reservado para la delegación (#170), ADR-008 (#159) y ADR-009 (#189) están propuestos en pull requests abiertos, y ADR-010 y ADR-011 están aceptados. Este es ADR-012.

## Decisión

### 1. Una herramienta de código confinada, distinta de las herramientas de operador

- **Una herramienta, `run_code`**, con las entradas `language` (`python` o `shell`) y `code` (un texto, con un tope de 64 KiB). El código llega por la entrada estándar de un intérprete fijo (`python3 -I -` o `sh -s`); ningún argv lo elige el modelo. Ambos lenguajes corren en la misma caja: Python puede iniciar la shell dentro de la caja de todos modos, así que el lenguaje es una comodidad, y el límite es la caja.
- **Permiso `compute:workspace`**, en una nueva familia de primer nivel `Compute` en **T2**. No es subclase de `Run` ni de `Exec`, así que ningún permiso existente lo cubre (ADR-003 R3), y por ser T2 se revalida en cada llamada (`interceptor.py:50`).
- **Por qué T2 y no T3.** T3 significa ejecución en el host con un alcance que elige quien despliega. Aquí el alcance lo fija el kit: un espacio de trabajo, sin red, sin secretos. Como en ADR-007, declararlo T3 tomaría prestado R4 por otro motivo; el riesgo residual de ejecutar sobre el kernel del host se declara en el modelo de amenazas en lugar de esconderse en un nivel.
- **Acotada al sujeto.** `run_code` fija `subject_scoped=True`, así que un turno sin sujeto se rechaza y se audita antes de que corra el conector (`interceptor.py:230-250`).
- **Las herramientas de operador no cambian.** `use_term`, `read_file` y las herramientas de comando conservan su punto de paso, sus niveles y sus requisitos de host.

### 2. La regla de entrada no confiable

- **R4 y C.11 siguen siendo absolutas para T3.** Un agente `untrusted_input` sigue sin tener ningún permiso T3 y sin recibir ninguna herramienta T3. C.11 y R4 reciben una nota de alcance: el cómputo confinado no es ejecución en el host en el sentido de esas reglas, porque su alcance no depende de las elecciones de quien despliega.
- **El perfil confinado es el único perfil.** No existe ningún campo que agregue red, montajes, variables de entorno o capacidades a `run_code`, para ningún agente. Un agente que necesita más es un agente de operador con `use_term`, y R4 lo mantiene lejos de la entrada no confiable.
- **Qué gana un modelo controlado por completo por un atacante**: la posibilidad de computar sobre los archivos que este sujeto ya tiene, dentro de los límites, y nada más. El modelo de amenazas de abajo declara qué puede y qué no puede hacer.

### 3. La caja

- **Un puerto, `CodeBox`**, con una sola llamada: ejecutar este código, en este directorio de trabajo, bajo estos límites, y devolver el estado de salida, la salida con tope y las cifras de recursos. El kit entrega las implementaciones; cada una debe pasar la suite de conformidad (parte 7).
- **Fase 1, `BubblewrapBox`**, un constructor de argv aparte que reutiliza las piezas del punto de paso (`_SYSTEM_RO_BINDS`, `_SYSTEM_ETC_BINDS`, `_child_env`, la terminación y la recolección de procesos) y agrega:
  - `--unshare-all --unshare-user --disable-userns`, para que el código no pueda crear espacios de nombres; bwrap corre sin privilegios, y el código no tiene ninguna capacidad (medido: `CapEff` vale `0` dentro);
  - una **lista permitida de seccomp** pasada con `--seccomp`, que deniega por defecto con `EPERM`, con la forma del perfil por defecto de Docker, que "deshabilita alrededor de 44 llamadas al sistema de más de 300" ([documentación de Docker](https://docs.docker.com/engine/security/seccomp/)); deniega, entre otras, `mount`, `unshare`, `setns`, `ptrace`, `bpf`, `keyctl`, `perf_event_open`, `userfaultfd` e `io_uring_*`. El kit la entrega compilada a cBPF para x86_64 y aarch64; cualquier otra arquitectura rechaza;
  - una **hoja de cgroup v2 por ejecución**, creada antes de que empiece el código, con `pids.max`, `memory.max` (sin swap) y `cpu.max`, y desmontada con `cgroup.kill` ([documentación del kernel](https://docs.kernel.org/admin-guide/cgroup-v2.html)). Requiere un subárbol de cgroup v2 delegado al servicio de la plataforma, como `Delegate=yes` de systemd ([systemd](https://systemd.io/CGROUP_DELEGATION/)). `RLIMIT_NPROC` no puede reemplazar a `pids.max`: cuenta todos los hilos del id de usuario real ([getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html)), incluidos los de la propia plataforma;
  - `prlimit` para `RLIMIT_CPU` (como respaldo), `RLIMIT_FSIZE` y `RLIMIT_NOFILE`, y `--size` en el tmpfs de `/tmp`;
  - **sin red**: sin `--share-net` y sin `resolv.conf`, sin ningún campo para cambiarlo;
  - **un entorno vacío**: `PATH`, `HOME=/workspace`, `LANG`, `LC_ALL`, nada más;
  - **un sistema de solo lectura**: las rutas de sistema fijas y la lista permitida de `/etc`, más los directorios de herramientas de solo lectura que nombra quien despliega (un entorno de Python con sus paquetes), validados como hoy `extra_ro_binds` (`operator.py:117-137`); nunca algo que nombre el agente;
  - el espacio de trabajo montado en lectura y escritura en la ruta fija `/workspace`, para no mostrar la estructura del host.
- **Límites por defecto**, que fija quien despliega por runtime y que el agente solo puede ajustar a la baja, como en #278:

| Límite | Por defecto | Lo aplica |
|---|---|---|
| Tiempo de reloj | 10 s, nunca por encima del `tool_call_timeout_s` del runtime, y por lo tanto dentro del lease del outbox (`execution_limits.py:51-53`) | Vencimiento, luego `cgroup.kill` |
| CPU | 1 CPU; tiempo de CPU como respaldo | `cpu.max`; `RLIMIT_CPU` |
| Memoria | 512 MiB, sin swap | `memory.max` |
| Procesos | 64 | `pids.max` |
| Tamaño de archivo; archivos abiertos | 64 MiB; 256 | `RLIMIT_FSIZE`; `RLIMIT_NOFILE` |
| `/tmp` | 64 MiB | `--size` del tmpfs |
| Salida | 8 KiB por flujo | Lectura con tope, como `_read_capped` (`operator.py:499-534`) |
| Concurrencia | 4 cajas por proceso, luego la llamada se rechaza con `busy` | Un semáforo en la caja |

- **Falla cerrada.** Sin `bwrap`, sin `prlimit`, sin cgroup delegado, con una arquitectura no soportada o con una caja que no pasa su autoverificación de arranque: la herramienta rechaza cada llamada con `sandbox_unavailable`, registrado al arrancar como hoy (`operator.py:461-493`).
- **Solo Linux.** En otros sistemas la biblioteca se importa y `run_code` rechaza.
- **Comparación de las opciones:**

| Opción | Límite | Requisitos del host | Costo operativo | Latencia de inicio |
|---|---|---|---|---|
| bubblewrap + seccomp + cgroups | Kernel del host detrás de espacios de nombres y un filtro de llamadas al sistema | Espacios de nombres de usuario sin privilegios (perfil de AppArmor en Ubuntu 23.10+), `bwrap`, `prlimit`, delegación de cgroup v2 | Bajo: sin demonio, nada como root | Medido: `true` 4 ms, `python3 -c 'print(1)'` 15 ms (mediana, mismo host que arriba) |
| gVisor (`runsc`) | Un kernel en espacio de usuario escrito en Go intercepta las llamadas al sistema; el kernel del host ve las llamadas del Sentry ([gVisor](https://gvisor.dev/docs/)) | El binario `runsc`; la plataforma systrap no necesita KVM ([plataformas](https://gvisor.dev/docs/architecture_guide/platforms/)); el modo sin root es "principalmente adecuado solo para `runsc do`" y no tiene netstack ([sin root](https://gvisor.dev/docs/user_guide/rootless/)) | Medio: un segundo runtime para instalar y actualizar; "bajo rendimiento en cargas con muchas llamadas al sistema" y algunas llamadas no implementadas | No medido aquí; la documentación atribuye la mayor parte del tiempo de inicio de un contenedor a Docker, no a `runsc` |
| Microvm Firecracker | Virtualización por hardware mediante KVM, cinco dispositivos emulados, más el jailer ([Firecracker](https://firecracker-microvm.github.io/)) | `/dev/kvm` (hardware dedicado o virtualización anidada), un kernel invitado y una imagen de sistema de archivos raíz, el jailer iniciado como root ([jailer](https://github.com/firecracker-microvm/firecracker/blob/main/docs/jailer.md)) | Alto: imágenes, preparación como root, una VM por ejecución | Cifra del proveedor: arranque en menos de 125 ms, menos de 5 MiB de sobrecarga; no medido aquí |

- **La solidez, sin rodeos.** La caja bubblewrap comparte el kernel del host: un fallo del kernel alcanzable mediante una llamada al sistema permitida es una fuga. Los espacios de nombres de usuario sin privilegios se usan "ampliamente como un paso en varias cadenas de escalada de privilegios", y Ubuntu cita el hallazgo de Google de que el 44% de los exploits vistos por kCTF los necesitaban ([Ubuntu](https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces)). Denegar los espacios de nombres anidados y las capacidades elimina ese paso para el código de adentro; esto es una inferencia, no una medición. gVisor interpone un kernel con memoria segura entre el código y el host; una microvm interpone virtualización por hardware.
- **Recomendación: fase 1 bubblewrap, fase 2 gVisor.** La fase 1 corre en todo host que el kit ya soporta, con unos pocos milisegundos por llamada. La fase 2 agrega `GVisorBox`, que elige quien despliega, después de una prueba que mida la latencia y el funcionamiento sin root y sin red. Una microvm queda fuera del kit: el puerto permite que quien adopta el kit y tiene KVM conecte una.

### 4. El espacio de trabajo

- **Uno por (id de runtime, clave del sujeto)**, en `<workspace_root>/<sha256(id de runtime)>/<sha256(clave del sujeto)>`. Quien despliega fija `workspace_root`, validado como `TerminalPolicy.root` (`operator.py:183-193`) y fuera del directorio de secretos. La plataforma deriva la ruta del handle acotado; el modelo nunca la nombra, y un hash no puede llevar sintaxis de ruta.
- **Una ejecución por espacio de trabajo a la vez**, mediante un bloqueo, para que un turno programado y un turno de canal del mismo sujeto no se intercalen.
- **Entrada de datos**: archivos que otras herramientas escriben en el espacio de trabajo mediante un método `WorkspaceFiles` del handle acotado (una exportación de un reporte, un adjunto recibido). Nunca credenciales de base de datos ni tokens: la caja no tiene red para usarlos ni entorno para recibirlos.
- **Salida de datos**: la salida con tope (parte 5), y los archivos que quedan en el espacio de trabajo, que otras herramientas leen mediante el mismo handle.
- **El acceso desde el host nunca sigue un enlace.** Toda lectura desde el host de un espacio de trabajo (herramientas, exportación, verificación de cuota) abre solo archivos regulares y rechaza enlaces simbólicos, FIFOs y dispositivos, porque el código de adentro puede sembrar `leak -> /etc/agents-system/agents-system.env` para que el host lo siga.
- **Cuota.** `RLIMIT_FSIZE` pone un tope a cada archivo; una verificación de tamaño después de cada llamada marca un espacio de trabajo que superó su cuota (256 MiB por defecto), y la llamada siguiente se rechaza hasta que se reduzca.
- **Retención, exportación, borrado.** Un `SubjectWorkspaceStore` implementa `SubjectDataStore` y `RetainedStore` (`subject_data.py:42-59`) y se inscribe en el registro de datos por sujeto, así que `export_subject` devuelve los archivos y `delete_subject` borra el directorio; la retención purga por antigüedad mediante el único purgador (ADR-011 parte 6). `run_code` se niega a construirse si el almacén no está inscrito.

### 5. Salida y auditoría

- **La salida es un bloque de datos no confiables.** La salida estándar y la de error, cada una con tope, vuelven como `UntrustedData(source="run_code", ...)`, renderizado con un delimitador nuevo dentro del mensaje de herramienta, y cuentan para `max_context_tokens`. Es el tercer consumidor de la parte 4 de ADR-011, la salida de herramientas: `ToolSpec` gana un campo tipado `untrusted_output: bool`, y `run_code` lo fija sin forma de desactivarlo. Con un modelo controlado por un atacante, el bloque no cambia nada; protege a un modelo honesto de un archivo hostil que le pidieron analizar.
- **Cada ejecución se audita**, incluidos los rechazos, como un evento `code_executed` con el actor y el sujeto en sus columnas, y en el contenido el id de runtime, el nombre y la versión de la caja, el lenguaje, el SHA-256 y el tamaño del código, los límites aplicados, el resultado (`ok`, `exit_nonzero`, `timeout`, `memory`, `pids`, `file_size`, `busy`, `refused:<motivo>`), el código de salida, los segundos de reloj y de CPU, la memoria máxima, los bytes de salida y si se truncaron, y el tamaño del espacio de trabajo. El texto del código va en el contenido solo cuando la política de auditoría del despliegue conserva las entradas de las herramientas, pasando por el redactor existente.

### 6. Quién la habilita

- **Tres deben coincidir**, como en el registro (ADR-004 4b): el agente declara `run_code` y `compute:workspace` en su manifiesto y puede ajustar los límites a la baja con campos tipados de `policy.md`; quien despliega otorga `compute:workspace` en `DEPLOY_GRANTS` o en `grants=`; y quien despliega pasa una `ComputePolicy` (raíz de los espacios de trabajo, directorios de herramientas, límites, caja) a `create_app(compute=...)` o a `build_runtime(compute=...)`. Sin la política, la herramienta rechaza cada llamada, como un `use_term` sin configurar (`operator.py:910`).
- **Sin punto de aprobación humana** (pregunta abierta 4). Para un agente con entrada no confiable, quien aprueba sería el usuario final, que puede ser el atacante, o un operador por cada mensaje, lo que anula el caso de uso. El control es la caja.

### 7. Migración y conformidad

- **Quien adopta el kit y usa su propio sandbox** asigna su herramienta a `run_code`, copia los archivos de cada usuario en la ruta de espacio de trabajo del kit, quita toda credencial que tenía su caja, otorga y configura como en la parte 6, ejecuta la suite de conformidad en su host y retira su caja. Conservarla es posible como una implementación de `CodeBox` que pase la misma suite; el kit soporta solo las cajas que entrega.
- **La suite de conformidad** es un conjunto de intentos de fuga de la suite de mecanismos (ADR-010 parte 1): un modelo guionado siempre emite el ataque, y cada protección tiene un gemelo sin la protección que debe fallar (`tests/test_mechanism_suite.py:1-11`). Cubre la red (una dirección externa, DNS, un puerto de loopback del host), los secretos (el entorno, el directorio de secretos, el home del host), otros espacios de trabajo (adivinar rutas, `..`, un enlace simbólico sembrado que lee una herramienta del host, una FIFO), escrituras fuera del espacio de trabajo, espacios de nombres de usuario anidados, `mount`, `ptrace`, `bpf`, `keyctl`, `io_uring`, una bomba de procesos, una bomba de memoria, un bucle de CPU más allá del tiempo de reloj, un archivo demasiado grande, un exceso de cuota, un proceso que intenta sobrevivir a la llamada, la inyección en la terminal, el `/proc` del host, avalanchas de salida y un delimitador de bloque falsificado. Extiende las pruebas actuales del sandbox (`tests/test_sandbox_integration.py:64-121`) y se entrega para que quien adopta el kit pueda ejecutarla en un host de despliegue.

## Modelo de amenazas

El atacante controla cada token que emite el modelo: cada entrada de `run_code`, cada otra llamada a herramientas y la respuesta. No controla las herramientas, los permisos, los límites, el perfil de la caja, la ruta del espacio de trabajo ni el prompt de sistema, todos fijados antes del turno (ADR-011 parte 4).

- **Dentro de la caja el atacante puede:** ejecutar cualquier código dentro de los límites; leer, cambiar y borrar cada archivo del espacio de trabajo de este sujeto; leer el árbol de sistema público y `/proc/self/mountinfo`, que lista los orígenes de los montajes del host (aceptado en #239, `docs/operations_es/sandbox-bwrap.md:222`); usar CPU, memoria, procesos y tiempo hasta los límites en cada llamada, en tantas llamadas como permitan `max_tool_calls` y el presupuesto del sujeto; sembrar enlaces, FIFOs o archivos grandes para los lectores del host; e invocar cualquier llamada al sistema que permita la lista.
- **El atacante no puede:** abrir una conexión de red; leer un secreto, porque el entorno está vacío, el directorio de secretos nunca se monta y ninguna credencial entra al espacio de trabajo; ver el espacio de trabajo de otro sujeto, porque se monta un solo directorio y lo eligió la plataforma; sobrevivir a la llamada; crear un espacio de nombres, montar u obtener una capacidad; cambiar sus propios permisos, límites, herramientas o prompt; ni ampliar el perfil, porque ningún campo lo hace.
- **Fuera de la caja**, el alcance son las otras herramientas del agente. Ninguna herramienta que entrega recibe una dirección elegida por el modelo (ADR-011 parte 5), así que los datos del espacio de trabajo llegan al sujeto que es su dueño, y a lo que permitan los demás permisos de quien despliega.
- **Riesgo residual:**
  - **Fuga por el kernel.** Un fallo alcanzable mediante una llamada al sistema permitida le da al atacante el usuario propio de la plataforma, que puede leer el archivo de secretos del despliegue, o root. La fase 2 reduce esta superficie; un host o una VM dedicados para un despliegue que atiende entrada no confiable limitan el radio del daño.
  - **Agotamiento dentro de los límites.** Hasta 4 cajas por proceso en sus límites, en cada llamada que permitan el turno y el presupuesto. Los presupuestos por usuario ponen precio a los tokens del modelo, no a los segundos de CPU.
  - **Los datos del propio usuario.** Código hostil, o un modelo honesto engañado por un archivo hostil, puede destruir o alterar el espacio de trabajo; la fase 1 no guarda instantáneas.
  - **Canales laterales.** Las cachés de CPU compartidas y los tiempos no se cierran con espacios de nombres.
  - **La configuración de quien despliega.** Un directorio de herramientas que contiene un secreto es legible dentro; la validación detecta solo las rutas conocidas.

## Migración

Ningún comportamiento existente cambia. Las herramientas de operador, las herramientas de comando, R4, C.11 y sus puntos de aplicación no se tocan; `ToolSpec` gana un campo con valor por defecto; `compute:workspace` es nuevo, así que ningún permiso existente lo cubre. Los hosts ganan requisitos solo cuando habilitan `run_code`: la delegación de cgroup v2 y un bubblewrap con `--disable-userns`. Quienes adoptan el kit migran como en la parte 7, y la documentación da los pasos.

## Consecuencias

- Un agente detrás de un canal puede analizar con código los datos de su usuario, dentro de los permisos, la auditoría y los límites del kit, en lugar del sandbox propio de quien lo adopta.
- El kit asume un límite de seguridad frente a código arbitrario. Una fuga por el kernel pasa a ser una clase de incidente del kit; la suite de conformidad y la fase 2 son su respuesta.
- Los despliegues con entrada no confiable y `run_code` necesitan un host con delegación de cgroup y, por recomendación, un host dedicado.
- La salida de herramientas pasa a ser datos no confiables por primera vez, empezando por `run_code`.
- ADR-002 C.11 y ADR-003 R4 reciben una referencia a este ADR al aceptarse.

## Preguntas abiertas para el responsable

1. **La regla de entrada no confiable.** Mantener C.11 absoluta, sin código para un agente `untrusted_input`, o permitir el perfil confinado como una familia T2 `Compute`. **Recomendación:** permitirlo. R4 sigue absoluta para T3, y la garantía la da la caja, no el nivel.
2. **Qué caja puede usar un agente con entrada no confiable.** La bubblewrap de la fase 1, o solo la caja gVisor de la fase 2. **Recomendación:** la fase 1, con el riesgo residual del kernel en la documentación y un host dedicado recomendado; el permiso que otorga quien despliega es la aceptación.
3. **Qué se ejecuta.** Python, una shell o ambos. **Recomendación:** ambos en una sola herramienta, porque Python puede iniciar la shell dentro de la caja de todos modos.
4. **Aprobación humana para agentes con entrada no confiable.** **Recomendación:** ninguna; quien aprueba sería el usuario final o un operador por cada mensaje.
5. **Un host sin delegación de cgroup.** Rechazar, o ejecutar solo con rlimits. **Recomendación:** rechazar; `pids.max` es lo que detiene una bomba de procesos.
6. **Cuota del espacio de trabajo.** Una verificación de tamaño después de cada llamada, o cuotas de proyecto del sistema de archivos. **Recomendación:** la verificación más `RLIMIT_FSIZE` en la fase 1; las cuotas de proyecto como refuerzo opcional.

## Seguimientos

Issues a abrir cuando se acepte este ADR (ninguno se abre ahora), en orden de dependencia:

1. **Suite de intentos de fuga.** Los intentos de fuga de la suite de mecanismos de la parte 7 con sus gemelos sin protección, ejecutados primero contra el punto de paso actual para registrar la línea base (se espera que fallen los espacios de nombres de usuario anidados, la cantidad de procesos y el tamaño de archivo).
2. **`BubblewrapBox` y el puerto `CodeBox`**, con la lista permitida de seccomp, la hoja de cgroup, los rlimits y la autoverificación de arranque, hasta que pase el punto 1.
3. **La familia `Compute`, `compute:workspace`, `ComputePolicy` y `run_code`**, acotada al sujeto, con sus escenarios en vivo de capacidad.
4. **`SubjectWorkspaceStore` y `WorkspaceFiles`**, con acceso desde el host sin enlaces, cuota, retención, exportación y borrado.
5. **Salida de herramientas no confiable**: `ToolSpec.untrusted_output` y su renderizado. Los puntos 3 a 5 pueden avanzar en paralelo cuando llegue el punto 2.
6. **El evento de auditoría `code_executed`**, y los segundos de CPU en los registros de consumo.
7. **Documentación EN + ES**: una página de ejecución de código, los requisitos del host, la guía de migración para quienes adoptan el kit, la fila de funcionalidades y las referencias en C.11 y R4.
8. **Más adelante: `GVisorBox`**, después de una prueba de `runsc` sin root y sin red, y de su latencia.
