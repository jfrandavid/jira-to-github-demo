# Migración de Jira a GitHub Issues y Projects

Demo de una migración completa: pasa los tickets de un proyecto de Jira a GitHub Issues, los ubica en un tablero de GitHub Projects y reemplaza las reglas automáticas de Jira con GitHub Actions.

Los issues de este repositorio son el resultado: fueron creados por el script a partir de un proyecto de Jira de ejemplo con 10 tickets.

## Qué resuelve

Una migración de este tipo tiene dos partes, y la demo cubre las dos.

1. **Los datos.** `migrate.py` lee los tickets con la API de Jira y los crea con la API de GitHub.
2. **Las reglas automáticas.** Lo que en Jira eran reglas de automatización, acá son dos workflows de GitHub Actions y las automatizaciones propias del tablero.

## Cómo se traduce cada dato

| En Jira | En GitHub | Detalle |
| --- | --- | --- |
| Clave del ticket (`DEMO-12`) | Prefijo del título: `[DEMO-12] ...` | Permite buscar un ticket viejo por su clave |
| Tipo (Bug, Task, Story) | Etiqueta `tipo: ...` | |
| Prioridad | Etiqueta `prioridad: ...` | |
| Etiquetas | Las mismas etiquetas | |
| Descripción | Texto del issue en Markdown | Jira guarda el texto en un formato propio (ADF); el script lo convierte |
| Comentarios | Comentarios, en el mismo orden | Cada uno indica autor y fecha originales |
| Estado | Abierto o cerrado, más la columna del tablero | Los estados "terminados" de Jira cierran el issue |
| Enlaces entre tickets | Sección "Relaciones" con el número nuevo (`#7`) | Se reescriben después de crear todos los issues |
| Persona asignada | Asignación en GitHub, si está en `users.json` | Si no está, queda escrita en el texto |
| Adjuntos | Enlace al archivo en Jira | Ver "Qué no se migra" |

## Qué no se migra y cómo queda resuelto

- **Autor y fecha originales.** La API de GitHub crea todo a nombre del dueño del token y con la fecha del momento. El script escribe el autor y la fecha originales dentro del texto de cada issue y de cada comentario.
- **Adjuntos.** La API de GitHub no permite subir archivos a un issue. Cada issue lista sus adjuntos con el enlace al archivo en Jira, así que Jira tiene que seguir accesible o los archivos se mueven aparte.
- **Historial de cambios.** Quién movió el ticket de un estado a otro y cuándo no pasa a GitHub. Queda en Jira.
- **Estados intermedios propios.** Jira permite estados a medida ("En revisión", "Esperando al cliente"). El script los agrupa en las tres categorías que usa Jira por dentro (por hacer, en curso, terminado) y cada una va a una columna del tablero. El nombre exacto del estado original queda escrito en el issue.

## Tres problemas de una migración real que el script contempla

- **No duplica.** Cada issue y cada comentario que crea lleva una marca oculta con su identificador de Jira. Antes de crear algo, el script busca esa marca. Se puede correr diez veces y el resultado es el mismo.
- **Retoma si se corta.** Si la migración se interrumpe por la mitad, al volver a correrla completa solo lo que falta.
- **No notifica ni enlaza por error.** En GitHub, `@nombre` le avisa al usuario con ese nombre y `#12` se convierte en un enlace al issue 12. Un ticket viejo que decía "@juan" le avisaría a un desconocido. El script desactiva las arrobas y los numerales que venían escritos en Jira.

Además espera un segundo entre cada escritura y reintenta solo cuando GitHub le pide frenar, para no chocar con los límites de uso de la API.

## Reglas automáticas

| Regla que suele haber en Jira | Cómo queda en GitHub |
| --- | --- |
| Un bug nuevo sin prioridad recibe prioridad alta | `.github/workflows/triage-bugs.yml` |
| Avisar cuando un ticket lleva 30 días sin actividad | `.github/workflows/stale-issues.yml` |
| Pasar a "Terminado" cuando se cierra el ticket | Automatización propia del tablero (viene activada) |
| Agregar al tablero todo ticket nuevo | Automatización propia del tablero ("Auto-add to project") |

Las dos últimas no necesitan Actions: el tablero las trae incluidas. Actions se usa para lo que el tablero no cubre.

## Cómo usarlo

Hace falta Python 3.10 o más nuevo.

```bash
pip install -r requirements.txt
cp .env.example .env        # y completar los datos de Jira y GitHub
```

Si el proyecto de Jira está vacío, este comando carga los 10 tickets de ejemplo:

```bash
python seed_jira.py
```

La migración tiene tres modos:

```bash
python migrate.py            # simulación: muestra qué haría, no escribe nada
python migrate.py --apply    # migra y verifica
python migrate.py --verify   # solo compara Jira contra GitHub
```

Al terminar, el script compara los dos lados y termina con error si algo no coincide:

```
Verificación
  Tickets en Jira:              10
  Issues migrados en GitHub:    10
  Comentarios en Jira:          11
  Comentarios migrados:         11
  Terminados en Jira / GitHub:  6 / 6
  Resultado: OK
```

Para asignar los issues a personas, copiar `users.example.json` como `users.json` y poner el nombre de cada persona en Jira junto a su usuario de GitHub.

## Pruebas

```bash
python -m pytest
```

Las pruebas corren contra un Jira y un GitHub simulados en memoria, sin internet ni cuentas. Cubren la simulación, la migración completa, la segunda corrida sin duplicados, la reanudación después de una caída, la verificación, el tablero y la conversión de texto.

## Archivos

- `migrate.py`: el migrador. Las reglas de traducción están al principio del archivo.
- `seed_jira.py`: carga los tickets de ejemplo en Jira.
- `.github/workflows/`: los dos workflows de Actions.
- `tests/`: las pruebas automáticas.
