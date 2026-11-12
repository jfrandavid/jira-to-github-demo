#!/usr/bin/env python3
"""Carga 20 tickets de ejemplo en un proyecto de Jira vacío.

Sirve para tener un origen realista que migrar: tickets de distinto tipo,
prioridad y estado, con comentarios, enlaces entre tickets y un adjunto.

Uso:
    python seed_jira.py
"""
from __future__ import annotations

import sys

from migrate import ApiError, Http, config_from_env, load_env_file

SEED_LABEL = "demo-migracion"


def text(value: str, *marks: str) -> dict:
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = [{"type": m} for m in marks]
    return node


def paragraph(*parts) -> dict:
    return {"type": "paragraph",
            "content": [text(p) if isinstance(p, str) else p for p in parts]}


def bullets(*items: str) -> dict:
    return {"type": "bulletList", "content": [
        {"type": "listItem", "content": [paragraph(item)]} for item in items]}


def code(source: str, language: str = "") -> dict:
    return {"type": "codeBlock", "attrs": {"language": language},
            "content": [{"type": "text", "text": source}]}


def doc(*blocks) -> dict:
    return {"type": "doc", "version": 1,
            "content": [paragraph(b) if isinstance(b, str) else b for b in blocks]}


# (tipo, prioridad, estado final, etiquetas, título, descripción, comentarios)
# estado final: "new" = por hacer, "indeterminate" = en curso, "done" = terminado
TICKETS = [
    ("bug", "High", "done", ["pagos"], "El pago con tarjeta falla si el monto tiene centavos",
     doc(paragraph("Al pagar ", text("$1.250,50", "strong"), " el sistema devuelve error 500."),
         "Pasos para reproducirlo:",
         bullets("Reservar un turno de $1.250,50", "Elegir tarjeta de crédito", "Confirmar el pago"),
         code('{"error": "invalid amount", "amount": "1250,50"}', "json")),
     ["Lo reproduje. El problema es la coma decimal: la pasarela espera punto.",
      "Corregido y desplegado. Lo vio @maria en producción antes que nosotros."]),
    ("bug", "Highest", "indeterminate", ["login"], "No se puede iniciar sesión desde Safari en iPhone",
     doc("El botón Ingresar no responde. Solo pasa en Safari, en Chrome funciona.",
         "Puede estar relacionado con lo que se cambió en #3."),
     ["Confirmado en iOS 18. Estoy revisando las cookies de sesión."]),
    ("task", "Medium", "done", ["infraestructura"], "Actualizar el certificado SSL del dominio",
     doc("El certificado vence a fin de mes. Hay que renovarlo y probar la renovación automática."), []),
    ("story", "Medium", "indeterminate", ["reservas"], "Permitir cancelar un turno hasta 2 horas antes",
     doc("Como cliente quiero cancelar mi turno desde la app para no tener que llamar.",
         bullets("Solo hasta 2 horas antes del turno", "Se devuelve la seña completa",
                 "El local recibe un aviso")),
     ["Falta definir qué pasa con las señas pagadas en efectivo.",
      "Definido con el cliente: en efectivo queda como saldo a favor."]),
    ("bug", "Low", "new", ["interfaz"], "El calendario muestra el domingo como primer día de la semana",
     doc("La semana tiene que empezar el lunes."), []),
    ("task", "High", "new", ["infraestructura"], "Configurar copias de seguridad diarias de la base de datos",
     doc("Hoy solo hay una copia semanal. Tiene que ser diaria y guardarse 30 días."),
     ["Propongo usar las copias automáticas del proveedor en vez de un script propio."]),
    ("story", "High", "new", ["pagos"], "Agregar pago con billetera virtual",
     doc("Varios locales lo piden. Empezar por la billetera más usada."), []),
    ("bug", "Medium", "done", ["reservas"], "Se pueden reservar dos turnos en el mismo horario",
     doc("Si dos personas confirman al mismo tiempo, quedan las dos reservas guardadas.",
         paragraph("Falta un ", text("bloqueo", "em"), " en la tabla de turnos.")),
     ["Agregué una restricción de unicidad en la base. Ya no se puede duplicar."]),
    ("task", "Low", "done", ["documentacion"], "Escribir la guía de instalación para desarrolladores nuevos",
     doc("Tiene que alcanzar para levantar el proyecto en menos de una hora."), []),
    ("task", "Medium", "indeterminate", ["infraestructura"], "Pasar los tests a un pipeline automático",
     doc("Hoy los tests se corren a mano antes de cada despliegue."),
     ["Ya corren los tests unitarios. Faltan los de integración."]),
    ("bug", "High", "new", ["notificaciones"], "Los recordatorios por mail llegan con una hora de diferencia",
     doc("Parece un problema de zona horaria: el servidor está en UTC."), []),
    ("story", "Low", "new", ["reservas"], "Lista de espera cuando no hay turnos disponibles",
     doc("Si se libera un turno, avisar al primero de la lista."), []),
    ("task", "Medium", "done", ["login"], "Agregar verificación en dos pasos para los administradores",
     doc("Obligatoria para cuentas con permiso de administración."),
     ["Hecho con códigos temporales. Queda documentado en la guía."]),
    ("bug", "Lowest", "new", ["interfaz"], "El logo se ve pixelado en pantallas de alta resolución",
     doc("Hay que reemplazar el PNG por un SVG."), []),
    ("story", "Medium", "new", ["reportes"], "Reporte mensual de turnos por profesional",
     doc("Exportable a planilla. Columnas: profesional, turnos dados, cancelados y ausentes."), []),
    ("task", "High", "indeterminate", ["pagos"], "Conciliar los pagos de la pasarela con las reservas",
     doc("Hay pagos aprobados que no figuran como reserva confirmada."),
     ["Encontré 12 casos del mes pasado. Todos son de pagos que tardaron más de 30 segundos."]),
    ("bug", "Medium", "new", ["notificaciones"], "El SMS de confirmación corta los nombres largos",
     doc("El texto supera los 160 caracteres y se parte en dos mensajes."), []),
    ("task", "Low", "new", ["documentacion"], "Documentar la API pública de reservas",
     doc("Con ejemplos de cada llamada."), []),
    ("story", "High", "done", ["reservas"], "Reprogramar un turno sin cancelarlo",
     doc("El cliente elige otro horario y conserva la seña."),
     ["Listo. Se probó con tres locales durante una semana."]),
    ("task", "Medium", "new", ["infraestructura"], "Definir alertas cuando el sitio tarda más de 3 segundos",
     doc("Avisar por el canal del equipo."), []),
]

# Pares (índice del que bloquea, índice del bloqueado), contando desde cero.
LINKS = [(0, 15), (9, 5)]
ATTACHMENT_ON = 1  # índice del ticket que recibe un archivo adjunto

TYPE_NAMES = {
    "bug": ("bug", "error"),
    "task": ("task", "tarea"),
    "story": ("story", "historia"),
}


class Seeder:
    def __init__(self, http: Http, cfg):
        self.http = http
        self.cfg = cfg
        self.base = f"{cfg.jira_base}/rest/api/3"
        self.auth = (cfg.jira_email, cfg.jira_token)
        self.headers = {"Accept": "application/json"}

    def call(self, method: str, path: str, **kwargs):
        return self.http.request(method, self.base + path, auth=self.auth,
                                 headers=self.headers, **kwargs)

    def already_seeded(self) -> bool:
        data = self.call("POST", "/search/jql", json={
            "jql": f'project = "{self.cfg.jira_project}" AND labels = "{SEED_LABEL}"',
            "maxResults": 1, "fields": ["summary"],
        }).json()
        return bool(data.get("issues"))

    def issue_types(self) -> dict[str, str]:
        """Elige, para cada tipo que usa la demo, un tipo que exista en el proyecto."""
        project = self.call("GET", f"/project/{self.cfg.jira_project}").json()
        available = [t for t in project.get("issueTypes", []) if not t.get("subtask")]
        if not available:
            raise SystemExit("El proyecto no tiene tipos de ticket disponibles.")
        by_name = {t["name"].lower(): t["id"] for t in available}
        fallback = by_name.get("task") or by_name.get("tarea") or available[0]["id"]
        chosen = {}
        for kind, names in TYPE_NAMES.items():
            chosen[kind] = next((by_name[n] for n in names if n in by_name), fallback)
        return chosen

    def create(self, type_id, priority, labels, title, description) -> str:
        fields = {
            "project": {"key": self.cfg.jira_project},
            "issuetype": {"id": type_id},
            "summary": title,
            "description": description,
            "labels": labels + [SEED_LABEL],
            "priority": {"name": priority},
        }
        try:
            return self.call("POST", "/issue", json={"fields": fields}).json()["key"]
        except ApiError:
            # Algunos proyectos no tienen el campo prioridad: se reintenta sin él.
            fields.pop("priority")
            return self.call("POST", "/issue", json={"fields": fields}).json()["key"]

    def move_to(self, key: str, category: str) -> None:
        """Mueve el ticket al primer estado disponible de la categoría pedida."""
        if category == "new":
            return
        transitions = self.call("GET", f"/issue/{key}/transitions").json().get("transitions", [])
        for t in transitions:
            if ((t.get("to") or {}).get("statusCategory") or {}).get("key") == category:
                self.call("POST", f"/issue/{key}/transitions", json={"transition": {"id": t["id"]}})
                return
        print(f"  aviso: no encontré un estado '{category}' para {key}; queda como está.")

    def link(self, blocker: str, blocked: str) -> None:
        types = self.call("GET", "/issueLinkType").json().get("issueLinkTypes", [])
        if not types:
            print("  aviso: el sitio no tiene tipos de enlace; no se enlazan tickets.")
            return
        name = next((t["name"] for t in types if t["name"].lower() == "blocks"), types[0]["name"])
        self.call("POST", "/issueLink", json={
            "type": {"name": name},
            "inwardIssue": {"key": blocker},
            "outwardIssue": {"key": blocked},
        })

    def attach(self, key: str) -> None:
        self.http.request(
            "POST", f"{self.base}/issue/{key}/attachments", auth=self.auth,
            headers={"Accept": "application/json", "X-Atlassian-Token": "no-check"},
            files={"file": ("registro-de-error.txt",
                            b"2026-11-02 10:14:03 ERROR login: cookie de sesion rechazada\n",
                            "text/plain")},
        )


def seed(cfg, session=None, out=print) -> list[str]:
    seeder = Seeder(Http(session, pause=0), cfg)
    if seeder.already_seeded():
        raise SystemExit("El proyecto ya tiene los tickets de ejemplo. No cargo nada.")
    types = seeder.issue_types()
    keys = []
    for kind, priority, status, labels, title, description, comments in TICKETS:
        key = seeder.create(types[kind], priority, labels, title, description)
        for comment in comments:
            seeder.call("POST", f"/issue/{key}/comment", json={"body": doc(comment)})
        seeder.move_to(key, status)
        keys.append(key)
        out(f"  creado {key}: {title}")
    for blocker, blocked in LINKS:
        try:
            seeder.link(keys[blocker], keys[blocked])
        except ApiError as error:
            out(f"  aviso: no pude enlazar {keys[blocker]} con {keys[blocked]}: {error}")
    try:
        seeder.attach(keys[ATTACHMENT_ON])
    except ApiError as error:
        out(f"  aviso: no pude subir el adjunto: {error}")
    return keys


def main() -> None:
    load_env_file()
    cfg = config_from_env()
    try:
        keys = seed(cfg)
    except ApiError as error:
        print(f"\nError de API:\n{error}", file=sys.stderr)
        sys.exit(2)
    print(f"\nListo: {len(keys)} tickets cargados en {cfg.jira_project}.")


if __name__ == "__main__":
    main()
