#!/usr/bin/env python3
"""Migra los tickets de un proyecto de Jira Cloud a GitHub Issues.

Uso:
    python migrate.py            Simulación: lee todo y muestra el plan, no escribe nada.
    python migrate.py --apply    Migra de verdad y verifica al final.
    python migrate.py --verify   Solo compara Jira contra GitHub.

El script se puede correr varias veces: nunca duplica issues ni comentarios,
porque cada cosa que crea lleva una marca oculta con el identificador de Jira.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# Reglas de traducción Jira -> GitHub (lo único que hay que tocar para adaptar)
# --------------------------------------------------------------------------

# Tipo de ticket de Jira -> etiqueta de GitHub. Acepta nombres en inglés y español.
TYPE_LABELS = {
    "bug": "tipo: bug",
    "error": "tipo: bug",
    "task": "tipo: tarea",
    "tarea": "tipo: tarea",
    "story": "tipo: historia",
    "historia": "tipo: historia",
    "epic": "tipo: epic",
    "subtask": "tipo: subtarea",
    "sub-task": "tipo: subtarea",
    "subtarea": "tipo: subtarea",
}

# Prioridad de Jira -> etiqueta de GitHub.
PRIORITY_LABELS = {
    "highest": "prioridad: crítica",
    "más alta": "prioridad: crítica",
    "high": "prioridad: alta",
    "alta": "prioridad: alta",
    "medium": "prioridad: media",
    "media": "prioridad: media",
    "low": "prioridad: baja",
    "baja": "prioridad: baja",
    "lowest": "prioridad: mínima",
    "más baja": "prioridad: mínima",
}

# Categoría de estado de Jira -> columna del tablero de GitHub Projects.
# Jira agrupa todos los estados en tres categorías: new, indeterminate y done.
STATUS_TO_PROJECT = {
    "new": "Todo",
    "indeterminate": "In Progress",
    "done": "Done",
}

KEY_MARK = re.compile(r"<!-- jira-key: ([A-Z][A-Z0-9_]*-\d+) -->")
COMMENT_MARK = re.compile(r"<!-- jira-comment: (\w+) -->")
MAX_BODY = 65000  # GitHub rechaza textos de más de 65.536 caracteres


# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------

@dataclass
class Config:
    jira_base: str
    jira_email: str
    jira_token: str
    jira_project: str
    gh_token: str
    gh_repo: str
    project_number: int | None = None
    project_owner: str | None = None
    user_map: dict = field(default_factory=dict)
    gh_api: str = "https://api.github.com"


def load_env_file(path: str = ".env") -> None:
    """Carga las variables del archivo .env. Si una ya existía, gana la del archivo."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def config_from_env() -> Config:
    required = [
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY",
        "GITHUB_TOKEN", "GITHUB_REPO",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            "Faltan variables de configuración: " + ", ".join(missing)
            + "\nCopiá .env.example como .env y completalo."
        )
    repo = os.environ["GITHUB_REPO"]
    if repo.count("/") != 1:
        raise SystemExit("GITHUB_REPO tiene que tener la forma usuario/repositorio.")
    number = os.environ.get("GITHUB_PROJECT_NUMBER", "").strip()
    user_map = {}
    if Path("users.json").exists():
        user_map = json.loads(Path("users.json").read_text(encoding="utf-8"))
    return Config(
        jira_base=os.environ["JIRA_BASE_URL"].rstrip("/"),
        jira_email=os.environ["JIRA_EMAIL"],
        jira_token=os.environ["JIRA_API_TOKEN"],
        jira_project=os.environ["JIRA_PROJECT_KEY"],
        gh_token=os.environ["GITHUB_TOKEN"],
        gh_repo=repo,
        project_number=int(number) if number else None,
        project_owner=os.environ.get("GITHUB_PROJECT_OWNER") or repo.split("/")[0],
        user_map=user_map,
    )


# --------------------------------------------------------------------------
# Capa HTTP: reintenta sola cuando la API pide esperar
# --------------------------------------------------------------------------

class ApiError(RuntimeError):
    pass


class Http:
    def __init__(self, session=None, pause: float = 1.0):
        self.session = session or requests.Session()
        self.pause = pause  # espera entre escrituras, para no chocar el límite de GitHub
        self.sleep = time.sleep

    def request(self, method: str, url: str, **kwargs):
        resp = None
        for _ in range(5):
            resp = self.session.request(method, url, timeout=30, **kwargs)
            if resp.status_code in (403, 429) and self._rate_limited(resp):
                headers = {k.lower(): v for k, v in resp.headers.items()}
                wait = int(headers.get("retry-after") or 60)
                print(f"  La API pidió esperar {wait} s. Espero y reintento.")
                self.sleep(wait)
                continue
            break
        if resp.status_code >= 400:
            hint = ""
            if resp.status_code == 401:
                hint = " (el token es inválido o venció)"
            elif resp.status_code == 404:
                hint = " (no existe, o el token no tiene permiso para verlo)"
            raise ApiError(f"{method} {url} -> {resp.status_code}{hint}\n{resp.text[:500]}")
        return resp

    @staticmethod
    def _rate_limited(resp) -> bool:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return (
            "retry-after" in headers
            or headers.get("x-ratelimit-remaining") == "0"
            or "rate limit" in resp.text.lower()
        )

    def wrote(self) -> None:
        if self.pause > 0:
            self.sleep(self.pause)


# --------------------------------------------------------------------------
# Jira
# --------------------------------------------------------------------------

JIRA_FIELDS = [
    "summary", "description", "issuetype", "priority", "status", "labels",
    "assignee", "reporter", "created", "issuelinks", "attachment", "parent",
]


class JiraClient:
    def __init__(self, http: Http, cfg: Config):
        self.http = http
        self.cfg = cfg
        self.auth = (cfg.jira_email, cfg.jira_token)
        self.headers = {"Accept": "application/json"}

    def search_issues(self) -> list[dict]:
        """Trae todos los tickets del proyecto, página por página."""
        url = f"{self.cfg.jira_base}/rest/api/3/search/jql"
        payload = {
            "jql": f'project = "{self.cfg.jira_project}" ORDER BY created ASC',
            "maxResults": 100,
            "fields": JIRA_FIELDS,
        }
        issues, seen_tokens = [], set()
        while True:
            data = self.http.request(
                "POST", url, json=payload, auth=self.auth, headers=self.headers
            ).json()
            issues.extend(data.get("issues", []))
            token = data.get("nextPageToken")
            if not token or data.get("isLast") or token in seen_tokens:
                break
            seen_tokens.add(token)
            payload["nextPageToken"] = token
        return issues

    def get_comments(self, key: str) -> list[dict]:
        url = f"{self.cfg.jira_base}/rest/api/3/issue/{key}/comment"
        comments, start = [], 0
        while True:
            data = self.http.request(
                "GET", url, params={"startAt": start, "maxResults": 100},
                auth=self.auth, headers=self.headers,
            ).json()
            page = data.get("comments", [])
            comments.extend(page)
            start += len(page)
            if not page or start >= data.get("total", 0):
                break
        return comments


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

class GitHubClient:
    def __init__(self, http: Http, cfg: Config):
        self.http = http
        self.cfg = cfg
        self.repo_url = f"{cfg.gh_api}/repos/{cfg.gh_repo}"
        self.headers = {
            "Authorization": f"Bearer {cfg.gh_token}",
            "Accept": "application/vnd.github+json",
        }

    def _get_all(self, url: str, params: dict | None = None) -> list[dict]:
        items, page = [], 1
        while True:
            batch = self.http.request(
                "GET", url, headers=self.headers,
                params={**(params or {}), "per_page": 100, "page": page},
            ).json()
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def migrated_issues(self) -> dict[str, dict]:
        """Devuelve los issues que ya fueron migrados, indexados por clave de Jira."""
        found = {}
        for issue in self._get_all(f"{self.repo_url}/issues", {"state": "all"}):
            if "pull_request" in issue:  # la API mezcla pull requests con issues
                continue
            match = KEY_MARK.search(issue.get("body") or "")
            if match:
                found[match.group(1)] = issue
        return found

    def label_names(self) -> set[str]:
        return {label["name"] for label in self._get_all(f"{self.repo_url}/labels")}

    def create_label(self, name: str, color: str) -> None:
        self.http.request(
            "POST", f"{self.repo_url}/labels", headers=self.headers,
            json={"name": name, "color": color},
        )
        self.http.wrote()

    def create_issue(self, title, body, labels, assignees) -> dict:
        issue = self.http.request(
            "POST", f"{self.repo_url}/issues", headers=self.headers,
            json={"title": title, "body": body, "labels": labels, "assignees": assignees},
        ).json()
        self.http.wrote()
        return issue

    def update_issue(self, number: int, **changes) -> dict:
        issue = self.http.request(
            "PATCH", f"{self.repo_url}/issues/{number}", headers=self.headers, json=changes,
        ).json()
        self.http.wrote()
        return issue

    def list_comments(self, number: int) -> list[dict]:
        return self._get_all(f"{self.repo_url}/issues/{number}/comments")

    def create_comment(self, number: int, body: str) -> None:
        self.http.request(
            "POST", f"{self.repo_url}/issues/{number}/comments",
            headers=self.headers, json={"body": body},
        )
        self.http.wrote()

    # ---- Tablero (GitHub Projects), que solo se maneja por GraphQL ----

    def graphql(self, query: str, variables: dict) -> dict:
        data = self.http.request(
            "POST", f"{self.cfg.gh_api}/graphql", headers=self.headers,
            json={"query": query, "variables": variables},
        ).json()
        if data.get("errors"):
            raise ApiError("GraphQL: " + json.dumps(data["errors"], ensure_ascii=False)[:500])
        return data["data"]

    def load_project(self) -> dict:
        """Busca el tablero y los identificadores de las opciones del campo Status."""
        query = """
        query($login: String!, $number: Int!) {
          repositoryOwner(login: $login) {
            ... on User { projectV2(number: $number) { ...Tablero } }
            ... on Organization { projectV2(number: $number) { ...Tablero } }
          }
        }
        fragment Tablero on ProjectV2 {
          id
          field(name: "Status") {
            ... on ProjectV2SingleSelectField { id options { id name } }
          }
        }
        """
        data = self.graphql(
            query, {"login": self.cfg.project_owner, "number": self.cfg.project_number}
        )
        project = (data.get("repositoryOwner") or {}).get("projectV2")
        if not project:
            raise ApiError(
                f"No encontré el tablero número {self.cfg.project_number} de "
                f"{self.cfg.project_owner}. Revisá GITHUB_PROJECT_NUMBER y que el "
                "token tenga el permiso 'project'."
            )
        status = project.get("field") or {}
        return {
            "id": project["id"],
            "status_field": status.get("id"),
            "options": {o["name"].lower(): o["id"] for o in status.get("options", [])},
        }

    def add_to_project(self, project_id: str, issue_node_id: str) -> str:
        query = """
        mutation($project: ID!, $content: ID!) {
          addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
            item { id }
          }
        }
        """
        data = self.graphql(query, {"project": project_id, "content": issue_node_id})
        return data["addProjectV2ItemById"]["item"]["id"]

    def set_project_status(self, project: dict, item_id: str, option_id: str) -> None:
        query = """
        mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $project, itemId: $item, fieldId: $field,
            value: {singleSelectOptionId: $option}
          }) { projectV2Item { id } }
        }
        """
        self.graphql(query, {
            "project": project["id"], "item": item_id,
            "field": project["status_field"], "option": option_id,
        })


# --------------------------------------------------------------------------
# Conversión del texto de Jira a Markdown
# --------------------------------------------------------------------------

def neutralize(text: str) -> str:
    """Evita dos efectos no deseados de GitHub sobre texto que vino de Jira.

    En GitHub, "@nombre" le manda una notificación al usuario con ese nombre y
    "#12" se convierte en un enlace al issue 12. Un ticket viejo que decía
    "@juan" avisaría a un desconocido, y "#12" apuntaría al issue equivocado.
    Se inserta un carácter invisible para que GitHub no los interprete.
    """
    text = re.sub(r"(?<![\w`/])@(?=\w)", "@​", text)
    return re.sub(r"(?<![\w&/])#(?=\d)", "#​", text)


def _wrap(text: str, mark: str) -> str:
    """Aplica negrita, cursiva o tachado dejando los espacios afuera de la marca."""
    core = text.strip()
    if not core:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return f"{lead}{mark}{core}{mark}{trail}"


def _inline(nodes) -> str:
    return "".join(_inline_node(n) for n in nodes or [])


def _inline_node(node: dict) -> str:
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "text":
        text = node.get("text", "")
        marks = {m.get("type"): m for m in node.get("marks") or []}
        if "code" in marks:
            return f"`{text}`"
        text = neutralize(text)
        if "strong" in marks:
            text = _wrap(text, "**")
        if "em" in marks:
            text = _wrap(text, "*")
        if "strike" in marks:
            text = _wrap(text, "~~")
        if "link" in marks:
            href = (marks["link"].get("attrs") or {}).get("href", "")
            text = f"[{text}]({href})"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        name = (attrs.get("text") or "alguien").lstrip("@")
        return f"**{name}**"  # sin arroba: no notifica a nadie en GitHub
    if kind == "emoji":
        return attrs.get("text") or attrs.get("shortName", "")
    if kind == "inlineCard":
        return attrs.get("url", "")
    return _inline(node.get("content"))


def _list(node: dict, depth: int, ordered: bool) -> str:
    lines = []
    indent = "   " * depth
    for index, item in enumerate(node.get("content") or [], 1):
        prefix = f"{index}. " if ordered else "- "
        first = True
        for child in item.get("content") or []:
            if child.get("type") in ("bulletList", "orderedList"):
                lines.append(_list(child, depth + 1, child["type"] == "orderedList"))
            else:
                text = _block(child).replace("\n", f"\n{indent}   ")
                lines.append(f"{indent}{prefix if first else '   '}{text}")
                first = False
    return "\n".join(lines)


def _block(node: dict) -> str:
    kind = node.get("type")
    content = node.get("content") or []
    attrs = node.get("attrs") or {}
    if kind == "paragraph":
        return _inline(content)
    if kind == "heading":
        return "#" * int(attrs.get("level", 3)) + " " + _inline(content)
    if kind == "bulletList":
        return _list(node, 0, ordered=False)
    if kind == "orderedList":
        return _list(node, 0, ordered=True)
    if kind == "codeBlock":
        code = "".join(n.get("text", "") for n in content)
        return f"```{attrs.get('language') or ''}\n{code}\n```"
    if kind in ("blockquote", "panel"):
        inner = "\n\n".join(_block(n) for n in content)
        return "\n".join("> " + line for line in inner.split("\n"))
    if kind == "rule":
        return "---"
    if kind in ("mediaSingle", "mediaGroup", "media"):
        return "_(imagen o archivo: ver la sección Adjuntos)_"
    if kind == "table":
        rows = []
        for row in content:
            cells = [" ".join(_block(c) for c in cell.get("content") or [])
                     for cell in row.get("content") or []]
            rows.append("| " + " | ".join(cells) + " |")
        if rows:
            columns = rows[0].count("|") - 1
            rows.insert(1, "|" + " --- |" * columns)
        return "\n".join(rows)
    if content and all("text" in n or n.get("type") in ("hardBreak", "mention") for n in content):
        return _inline(content)
    return "\n\n".join(_block(n) for n in content)


def adf_to_markdown(node) -> str:
    """Convierte el formato de texto de Jira (ADF, un árbol JSON) a Markdown."""
    if not node:
        return ""
    if isinstance(node, str):
        return neutralize(node)
    return "\n\n".join(_block(n) for n in node.get("content") or []).strip()


# --------------------------------------------------------------------------
# Modelo intermedio: un ticket ya leído y traducido
# --------------------------------------------------------------------------

@dataclass
class Comment:
    id: str
    author: str
    created: str
    body: str


@dataclass
class Ticket:
    key: str
    title: str
    description: str
    type_name: str
    priority_name: str
    status_name: str
    status_category: str
    labels: list
    assignee: str
    reporter: str
    created: str
    relations: list  # pares (texto de la relación, clave del otro ticket)
    attachments: list  # pares (nombre, url)
    comments: list

    @property
    def number_in_jira(self) -> int:
        return int(self.key.rsplit("-", 1)[1])


def parse_ticket(raw: dict, raw_comments: list[dict]) -> Ticket:
    f = raw.get("fields") or {}
    status = f.get("status") or {}
    relations = []
    if f.get("parent"):
        relations.append(("ticket padre", f["parent"]["key"]))
    for link in f.get("issuelinks") or []:
        link_type = link.get("type") or {}
        if link.get("outwardIssue"):
            relations.append((link_type.get("outward", "relacionado con"), link["outwardIssue"]["key"]))
        elif link.get("inwardIssue"):
            relations.append((link_type.get("inward", "relacionado con"), link["inwardIssue"]["key"]))
    return Ticket(
        key=raw["key"],
        title=f.get("summary") or "(sin título)",
        description=adf_to_markdown(f.get("description")),
        type_name=(f.get("issuetype") or {}).get("name", ""),
        priority_name=(f.get("priority") or {}).get("name", ""),
        status_name=status.get("name", ""),
        status_category=(status.get("statusCategory") or {}).get("key", "new"),
        labels=list(f.get("labels") or []),
        assignee=(f.get("assignee") or {}).get("displayName", ""),
        reporter=(f.get("reporter") or {}).get("displayName", ""),
        created=(f.get("created") or "")[:10],
        relations=relations,
        attachments=[(a.get("filename", "archivo"), a.get("content", ""))
                     for a in f.get("attachment") or []],
        comments=[
            Comment(
                id=str(c["id"]),
                author=(c.get("author") or {}).get("displayName", "desconocido"),
                created=(c.get("created") or "")[:10],
                body=adf_to_markdown(c.get("body")),
            )
            for c in raw_comments
        ],
    )


def labels_for(ticket: Ticket) -> list[str]:
    labels = []
    if ticket.type_name:
        name = ticket.type_name.lower()
        labels.append(TYPE_LABELS.get(name, f"tipo: {name}"))
    if ticket.priority_name:
        name = ticket.priority_name.lower()
        labels.append(PRIORITY_LABELS.get(name, f"prioridad: {name}"))
    labels.extend(ticket.labels)
    return list(dict.fromkeys(labels))  # sin repetidos, en el mismo orden


def label_color(name: str) -> str:
    if name.startswith("tipo: bug"):
        return "D73A4A"
    if name.startswith("tipo:"):
        return "1D76DB"
    if name in ("prioridad: crítica", "prioridad: alta"):
        return "B60205"
    if name.startswith("prioridad:"):
        return "FBCA04"
    return "C5DEF5"


def build_title(ticket: Ticket) -> str:
    return f"[{ticket.key}] {ticket.title}"[:250]


def build_body(ticket: Ticket, cfg: Config, numbers: dict[str, int]) -> str:
    """Arma el texto del issue. `numbers` traduce clave de Jira a número de GitHub."""
    header = [
        f"> **Migrado desde Jira:** [{ticket.key}]({cfg.jira_base}/browse/{ticket.key})",
        f"> **Creado por:** {ticket.reporter or 'desconocido'} · "
        f"**Fecha original:** {ticket.created or 'desconocida'}",
        f"> **Estado en Jira:** {ticket.status_name or 'desconocido'}",
    ]
    if ticket.assignee:
        header.append(f"> **Asignado en Jira a:** {ticket.assignee}")
    parts = ["\n".join(header), ticket.description or "_Sin descripción._"]

    if ticket.relations:
        lines = ["### Relaciones con otros tickets"]
        for relation, other in ticket.relations:
            if other in numbers:
                lines.append(f"- {relation}: #{numbers[other]} ({other})")
            elif other.rsplit("-", 1)[0] == cfg.jira_project:
                lines.append(f"- {relation}: {other} (todavía no migrado)")
            else:
                lines.append(f"- {relation}: {other} (de otro proyecto, no se migra)")
        parts.append("\n".join(lines))

    if ticket.attachments:
        lines = ["### Adjuntos (siguen guardados en Jira)"]
        lines += [f"- [{name}]({url})" for name, url in ticket.attachments]
        parts.append("\n".join(lines))

    mark = f"<!-- jira-key: {ticket.key} -->"
    body = "\n\n".join(parts)
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n_(Texto recortado: el original sigue en Jira.)_"
    return f"{body}\n\n{mark}"


def build_comment(comment: Comment) -> str:
    return (
        f"**{comment.author}** comentó en Jira el {comment.created}:\n\n"
        f"{comment.body or '_(comentario vacío)_'}\n\n"
        f"<!-- jira-comment: {comment.id} -->"
    )


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _same(a: str | None, b: str | None) -> bool:
    def norm(s):
        return (s or "").replace("\r\n", "\n").strip()
    return norm(a) == norm(b)


# --------------------------------------------------------------------------
# Migración
# --------------------------------------------------------------------------

@dataclass
class Report:
    created: int = 0
    already: int = 0
    comments: int = 0
    bodies: int = 0
    closed: int = 0
    labels: int = 0
    board: int = 0


def read_jira(jira: JiraClient) -> list[Ticket]:
    tickets = [parse_ticket(raw, jira.get_comments(raw["key"])) for raw in jira.search_issues()]
    tickets.sort(key=lambda t: t.number_in_jira)
    return tickets


def migrate(tickets, gh: GitHubClient, cfg: Config, apply: bool, out=print) -> Report:
    report = Report()
    issues = gh.migrated_issues()
    created_now = set()
    mode = "MIGRACIÓN" if apply else "SIMULACIÓN (no se escribe nada)"
    out(f"\n{mode}: {len(tickets)} tickets de Jira, {len(issues)} ya estaban en GitHub.\n")

    # Etiquetas: se crean antes para poder darles color.
    needed = list(dict.fromkeys(label for t in tickets for label in labels_for(t)))
    existing_labels = gh.label_names()
    missing = [name for name in needed if name not in existing_labels]
    for name in missing:
        out(f"  etiqueta nueva: {name}")
        if apply:
            gh.create_label(name, label_color(name))
    report.labels = len(missing)

    # Pasada 1: crear los issues que faltan. Recién después se conocen los
    # números de GitHub, que hacen falta para reescribir los enlaces.
    for t in tickets:
        if t.key in issues:
            report.already += 1
            continue
        login = cfg.user_map.get(t.assignee)
        extra = f", {_plural(len(t.comments), 'comentario')}" if t.comments else ""
        closing = ", se cierra" if t.status_category == "done" else ""
        out(f"  crear   {build_title(t)}  [{', '.join(labels_for(t))}]{extra}{closing}")
        report.created += 1
        if apply:
            numbers = {k: i["number"] for k, i in issues.items()}
            issues[t.key] = gh.create_issue(
                build_title(t), build_body(t, cfg, numbers), labels_for(t),
                [login] if login else [],
            )
            created_now.add(t.key)

    # Pasada 2: enlaces con los números definitivos, comentarios, cierre y tablero.
    project = gh.load_project() if cfg.project_number else None
    numbers = {k: i["number"] for k, i in issues.items()}
    for t in tickets:
        issue = issues.get(t.key)
        if issue is None:  # solo pasa en simulación
            report.comments += len(t.comments)
            report.closed += t.status_category == "done"
            continue
        n = issue["number"]

        body = build_body(t, cfg, numbers)
        if not _same(body, issue.get("body")):
            report.bodies += 1
            if apply:
                gh.update_issue(n, body=body)

        done_ids = set()
        if t.key not in created_now:
            for existing in gh.list_comments(n):
                done_ids.update(COMMENT_MARK.findall(existing.get("body") or ""))
        pending = [c for c in t.comments if c.id not in done_ids]
        if pending and t.key not in created_now:
            out(f"  faltan  {_plural(len(pending), 'comentario')} en #{n} ({t.key})")
        report.comments += len(pending)
        if apply:
            for c in pending:
                gh.create_comment(n, build_comment(c))

        if t.status_category == "done" and issue.get("state") != "closed":
            if t.key not in created_now:
                out(f"  cerrar  #{n} ({t.key})")
            report.closed += 1
            if apply:
                gh.update_issue(n, state="closed", state_reason="completed")

        if project and apply:
            item_id = gh.add_to_project(project["id"], issue["node_id"])
            column = STATUS_TO_PROJECT.get(t.status_category, "")
            option = project["options"].get(column.lower())
            if option and project["status_field"]:
                gh.set_project_status(project, item_id, option)
            report.board += 1

    if apply:
        out(
            f"\nResumen: {report.created} issues creados, {report.already} ya existían, "
            f"{report.comments} comentarios, {report.bodies} textos actualizados, "
            f"{report.closed} cerrados, {report.labels} etiquetas nuevas"
            + (f", {report.board} ubicados en el tablero." if project else ".")
        )
    else:
        out(
            f"\nQuedaría por hacer: crear {report.created} issues ({report.already} ya existen), "
            f"{report.comments} comentarios y {report.labels} etiquetas, "
            f"actualizar {report.bodies} textos y cerrar {report.closed} issues."
        )
    return report


def verify(tickets, gh: GitHubClient, out=print) -> bool:
    """Compara lo que hay en Jira con lo que quedó en GitHub."""
    issues = gh.migrated_issues()
    missing = [t.key for t in tickets if t.key not in issues]
    jira_comments = sum(len(t.comments) for t in tickets)
    gh_comments, wrong_state = 0, []
    for t in tickets:
        issue = issues.get(t.key)
        if not issue:
            continue
        ids = set()
        for c in gh.list_comments(issue["number"]):
            ids.update(COMMENT_MARK.findall(c.get("body") or ""))
        gh_comments += len(ids & {c.id for c in t.comments})
        should_be_closed = t.status_category == "done"
        if should_be_closed != (issue.get("state") == "closed"):
            wrong_state.append(t.key)
    jira_done = sum(t.status_category == "done" for t in tickets)
    gh_done = sum(1 for t in tickets if (issues.get(t.key) or {}).get("state") == "closed")

    ok = not missing and not wrong_state and jira_comments == gh_comments
    out("\nVerificación")
    out(f"  Tickets en Jira:              {len(tickets)}")
    out(f"  Issues migrados en GitHub:    {len(tickets) - len(missing)}")
    out(f"  Comentarios en Jira:          {jira_comments}")
    out(f"  Comentarios migrados:         {gh_comments}")
    out(f"  Terminados en Jira / GitHub:  {jira_done} / {gh_done}")
    if missing:
        out(f"  Faltan migrar: {', '.join(missing)}")
    if wrong_state:
        out(f"  Abierto/cerrado no coincide: {', '.join(wrong_state)}")
    out(f"  Resultado: {'OK' if ok else 'HAY DIFERENCIAS'}")
    return ok


def run(cfg: Config, *, apply: bool = False, only_verify: bool = False,
        session=None, pause: float = 1.0, out=print) -> int:
    http = Http(session, pause)
    gh = GitHubClient(http, cfg)
    out(f"Leyendo el proyecto {cfg.jira_project} de {cfg.jira_base} ...")
    tickets = read_jira(JiraClient(http, cfg))
    if only_verify:
        return 0 if verify(tickets, gh, out) else 1
    migrate(tickets, gh, cfg, apply, out)
    if not apply:
        out("\nEsto fue una simulación. Para migrar de verdad: python migrate.py --apply")
        return 0
    return 0 if verify(tickets, gh, out) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Migra tickets de Jira a GitHub Issues.")
    parser.add_argument("--apply", action="store_true", help="escribe en GitHub (sin esto, solo simula)")
    parser.add_argument("--verify", action="store_true", help="solo compara Jira contra GitHub")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="segundos de espera entre escrituras (por defecto 1)")
    args = parser.parse_args()
    load_env_file()
    try:
        code = run(config_from_env(), apply=args.apply, only_verify=args.verify, pause=args.pause)
    except ApiError as error:
        print(f"\nError de API:\n{error}", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
