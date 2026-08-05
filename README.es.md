# shutter-farm

[![CI](https://github.com/keivanmalhani/shutter-farm/actions/workflows/ci.yml/badge.svg)](https://github.com/keivanmalhani/shutter-farm/actions/workflows/ci.yml)
![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

[English](README.md) | Espanol

Apunta un contenedor a un volumen de medios. Hace el culling de todo el archivo en horario programado, y nunca repite el mismo trabajo dos veces.

`shutter-farm` es la historia de despliegue de la [familia shutter](https://github.com/keivanmalhani). Descubre las carpetas que necesitan proceso, manda cada una a [shutter-cull](https://github.com/keivanmalhani/shutter-cull) o a [shutter-select](https://github.com/keivanmalhani/shutter-select), y lleva un libro de registro para que un cron nocturno sea idempotente, reanudable y barato.

## Local-first no significa no desplegable

Local-first es una promesa sobre a donde van tus datos. No es una afirmacion de que el software no se pueda operar en serio.

Un estudio con una maquina de archivo, un NAS o un rack no quiere correr un CLI a mano sobre cuatro terabytes de sesiones. Quiere algo que corra a las 3am, le diga que hizo, y no se caiga porque una tarjeta salio corrupta. Eso es infraestructura, y la infraestructura no es lo opuesto a la privacidad: este contenedor monta *tu* volumen, en *tu* maquina o *tu* cluster, y el unico puerto que abre es el de metricas que tu decidas raspar.

Las fotos siguen sin salir de ahi.

## Que hace

```text
   /media  ......................  tu archivo, se puede montar solo lectura
      |
      v
   descubrir  ...................  un trabajo por carpeta con medios
      |                            se omiten salidas, ocultos y "do not include"
      v
   preguntar al registro  .......  hecho y sin cambios? se omite, ese es el punto
      |
      v
   despachar  ...................  shutter-cull para foto, shutter-select para video
      |                            subproceso, timeout, se mata el grupo si se cuelga
      v
   registrar  ...................  despues de CADA carpeta, no al final del lote
      |
      +--> logs JSON estructurados en stdout
      +--> metricas Prometheus en :9090
      +--> /healthz y /readyz
```

## Lo interesante es el registro

Un cron que reprocesa todo cada hora no es un pipeline, es un calentador. Asi que el farm tiene que contestar una pregunta barata y correcta: **esta carpeta ya se hizo, en el estado en el que esta ahora mismo?**

Las marcas de tiempo son la respuesta obvia y la equivocada, porque el mtime de una carpeta cambia cuando algo adentro se toca, incluidas las herramientas que el farm acaba de correr. La llave es el contenido: una huella sobre el nombre, tamano y mtime de cada archivo de medios, y *solo* de los archivos de medios.

Esa decision compra todo lo demas:

- Agrega una foto y la carpeta vuelve a ser trabajo.
- Corre las herramientas, que escriben sidecars y un arbol `_selects` dentro de esa misma carpeta, y la huella no se mueve. La siguiente pasada no hace nada.
- Copia la carpeta a otro lado y correctamente es otro trabajo.
- Pierde el registro completo y el farm reprocesa. Desperdicio, nunca incorrecto, que es la direccion correcta para algo que corre sin supervision.

Verificado de extremo a extremo: una pasada real sobre un archivo real corrio ambos motores, escribio sidecars XMP y una linea de tiempo de selects, y la segunda pasada termino en 0.1 segundos sin hacer nada. Agregar una foto trajo de vuelta exactamente una carpeta.

## La falla es por carpeta, nunca por corrida

Una tarjeta ilegible no debe tumbar un lote nocturno de doscientas sesiones.

Una carpeta que falla se registra con su error, se reintenta con retroceso exponencial, y despues de tres intentos queda en **cuarentena**: deja de quemar ciclos y empieza a ser visible, registrada en WARNING en cada pasada posterior con el error que la puso ahi. Arregla la carpeta y el cambio de huella la libera sola. O liberala a mano:

```bash
shutter-farm retry --root /media /media/2026-04-canyon
```

## Hecho para que lo maten

El trabajo por lotes va en capacidad barata, asi que todo el diseno asume que el nodo puede desaparecer a media pasada:

- El estado se escribe despues de cada carpeta, asi que un trabajo interrumpido pierde una carpeta, no el lote.
- SIGTERM termina la carpeta actual y sale limpio en vez de morir a media escritura.
- Cada invocacion tiene timeout, y un cuelgue mata al grupo de procesos completo para que los hijos de ffmpeg no sobrevivan a su padre.
- Los codigos de salida distinguen causas: `0` limpio, `1` mala configuracion, `2` la pasada corrio pero algunas carpetas fallaron. Un programador de tareas puede distinguir "no pude arrancar" de "corri y parte del trabajo esta roto", que son alertas distintas a las 3am.

## Como correrlo

Local:

```bash
ARCHIVE=~/Pictures docker compose up
```

Kubernetes, cada noche:

```bash
kubectl apply -f deploy/k8s/pvc.yaml -f deploy/k8s/cronjob.yaml
```

Cloud Run Jobs:

```bash
./deploy/cloud-run-job.sh my-project us-central1
```

O sin contenedor, en la maquina donde ya vive el archivo:

```bash
pip install git+https://github.com/keivanmalhani/shutter-farm.git
```

```bash
shutter-farm run --root /Volumes/Archive
```

## La escritura viene apagada

El farm pasa la bandera `--write` a las herramientas en lugar de decidir por ti, y viene apagada en todos lados: en el CLI, en los manifiestos y en el compose, que ademas monta el archivo en solo lectura. Un lote programado que empieza a calificar el archivo de un cliente porque una opcion cambio en silencio es exactamente la falla que esto evita.

Mira primero una pasada en seco. Luego enciendelo a proposito.

## Configuracion

Cada bandera tiene su variable de entorno, porque los manifiestos configuran con entorno y las personas configuran con banderas.

| Bandera | Entorno | Por defecto | Que hace |
| --- | --- | --- | --- |
| `--root` | `FARM_ROOT` | - | El volumen de medios a recorrer. |
| `--state` | `FARM_STATE` | `<root>/.shutter-farm-state.json` | Ruta del registro. Ponlo en su propio volumen si el archivo es de solo lectura. |
| `--write` | `FARM_WRITE` | apagado | Deja que las herramientas escriban sus salidas. |
| `--timeout` | `FARM_TIMEOUT` | 3600 | Segundos antes de matar la herramienta de una carpeta. |
| `--max-jobs` | `FARM_MAX_JOBS` | 0 | Limita carpetas por pasada. El resto queda en fila. |
| `--max-attempts` | `FARM_MAX_ATTEMPTS` | 3 | Fallas antes de mandar una carpeta a cuarentena. |
| `--metrics-port` | `FARM_METRICS_PORT` | 0 | Sirve `/metrics`, `/healthz`, `/readyz`. |
| `--interval` | `FARM_INTERVAL` | 900 | Solo en modo `serve`: segundos entre pasadas. |

## Observabilidad sin proveedor

Los logs son un objeto JSON por linea en stdout, con el `severity` que Cloud Logging espera, asi que `jsonPayload.event` y `jsonPayload.folder` son campos consultables sin parser y sin agente:

```json
{"severity":"INFO","time":"2026-08-05T17:12:11Z","event":"job_finished","service":"shutter-farm","folder":"/media/2026-04-canyon","tool":"shutter-cull","duration_seconds":1.9,"media_files":4}
```

Las metricas salen en formato de texto Prometheus desde un servidor HTTP de la biblioteca estandar, asi que un ServiceMonitor de GKE y un `curl` local funcionan igual:

```text
shutter_farm_jobs_total{result="success",tool="shutter-cull"} 41
shutter_farm_folders{status="quarantined"} 1
shutter_farm_last_run_timestamp_seconds 1785945130
```

Esa ultima es la alerta que si vale la pena: si deja de avanzar, el horario esta roto, y ninguna cantidad de pods verdes te lo va a decir.

## Postura de seguridad

- Sin root (uid 10001), sistema de archivos raiz en solo lectura, todas las capacidades removidas, `no-new-privileges`.
- Sin red saliente. El unico puerto que escucha es el de metricas.
- El farm es solo biblioteca estandar: cero dependencias propias, asi que nunca es la razon por la que una imagen no compila o un escaneo de CVE se enciende.
- Los comandos se construyen como listas de argumentos, nunca como cadenas de shell. Una carpeta con comillas en el nombre es algo normal en el disco de un fotografo, no una inyeccion.
- No se siguen directorios enlazados, asi que un enlace dentro de la raiz de trabajo no puede meter un archivo ajeno a un lote programado.

## Desarrollo

```bash
pip install -e ".[dev]"
pytest
```

52 pruebas, sin motores: el farm despacha a las herramientas en lugar de importarlas, asi que la suite corre contra un despachador falso y cubre lo que el farm realmente es, o sea descubrimiento, idempotencia, aislamiento de fallas y observabilidad. CI ademas construye la imagen en cada push y verifica que no corra como root, porque un repo cuyo argumento entero es "esto se despliega" no deberia dejar eso sin verificar.

## Familia

[shutter-cull](https://github.com/keivanmalhani/shutter-cull) y [shutter-select](https://github.com/keivanmalhani/shutter-select) son los motores. [shutter-cull-mcp](https://github.com/keivanmalhani/shutter-cull-mcp) es la interfaz para agentes. Este es el de lotes: mismas herramientas, mismas garantias, en horario.

## Licencia

MIT, ver [LICENSE](LICENSE).
