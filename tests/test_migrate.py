"""Pruebas del migrador contra un Jira y un GitHub simulados en memoria.

No usan internet ni cuentas reales: FakeSession responde como responderían
las dos APIs, y guarda todo lo que el script escribe para poder revisarlo.

Correr con:  python -m pytest
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import migrate  # noqa: E402
from migrate import Config, adf_to_markdown, neutralize, run  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = data if data is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


def adf(text):
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def jira_issue(n, title, category="new", kind="Task", priority="Medium", labels=(),
               description="Descripción", links=(), attachments=()):
    return {"key": f"DEMO-{n}", "fields": {
        "summary": title,
        "description": adf(description),
        "issuetype": {"name": kind},
        "priority": {"name": priority},
        "status": {"name": category, "statusCategory": {"key": category}},
        "labels": list(labels),
        "assignee": {"displayName": "Ana Pérez"},
        "reporter": {"displayName": "Luis Gómez"},
        "created": "2026-10-03T14:22:11.123-0300",
        "issuelinks": [
            {"type": {"outward": "blocks", "inward": "is blocked by"},
             "outwardIssue": {"key": other}} for other in links],
        "attachment": [{"filename": name, "content": f"https://jira/att/{name}"}
                       for name in attachments],
    }}


class FakeSession:
    """Simula Jira Cloud y GitHub. Pagina de a 2 para ejercitar la paginación."""

    def __init__(self, jira_issues, jira_comments=None):
        self.jira_issues = jira_issues
        self.jira_comments = jira_comments or {}
        self.gh_issues = []      # cada uno: number, title, body, state, labels, node_id
        self.gh_comments = {}    # número de issue -> lista de textos
        self.gh_labels = {"bug": "d73a4a"}
        self.project_items = {}  # node_id -> columna
        self.writes = []         # (método, url) de cada escritura
        self.fail_after_creates = None

    def request(self, method, url, headers=None, params=None, json=None,
                auth=None, timeout=None, **_):
        if method != "GET" and "search/jql" not in url:
            self.writes.append((method, url))
        params = params or {}

        # ---------------- Jira ----------------
        if url.endswith("/rest/api/3/search/jql"):
            assert auth, "Jira necesita autenticación"
            assert "fields" in json and json["jql"].startswith('project = "DEMO"')
            start = int(json.get("nextPageToken") or 0)
            page = self.jira_issues[start:start + 2]
            data = {"issues": page}
            if start + 2 < len(self.jira_issues):
                data["nextPageToken"] = str(start + 2)
            return FakeResponse(200, data)
        match = re.search(r"/rest/api/3/issue/(DEMO-\d+)/comment$", url)
        if match:
            comments = self.jira_comments.get(match.group(1), [])
            start = int(params.get("startAt", 0))
            return FakeResponse(200, {"comments": comments[start:start + 2],
                                      "total": len(comments)})

        # ---------------- GitHub ----------------
        assert headers and headers["Authorization"] == "Bearer gh-token"
        if url.endswith("/graphql"):
            return self._graphql(json)
        assert "/repos/yo/demo" in url, url
        path = url.split("/repos/yo/demo", 1)[1]

        if path == "/labels" and method == "GET":
            return self._page([{"name": n} for n in self.gh_labels], params)
        if path == "/labels" and method == "POST":
            assert json["name"] not in self.gh_labels, "etiqueta duplicada"
            self.gh_labels[json["name"]] = json["color"]
            return FakeResponse(201, json)
        if path == "/issues" and method == "GET":
            assert params.get("state") == "all"
            return self._page(self.gh_issues, params)
        if path == "/issues" and method == "POST":
            if self.fail_after_creates is not None:
                if self.fail_after_creates == 0:
                    return FakeResponse(500, {"message": "se cayó"})
                self.fail_after_creates -= 1
            number = len(self.gh_issues) + 1
            issue = {"number": number, "title": json["title"], "body": json["body"],
                     "state": "open", "labels": json["labels"],
                     "assignees": json["assignees"], "node_id": f"NODE{number}"}
            self.gh_issues.append(issue)
            return FakeResponse(201, issue)
        match = re.fullmatch(r"/issues/(\d+)", path)
        if match and method == "PATCH":
            issue = self.gh_issues[int(match.group(1)) - 1]
            issue.update({k: v for k, v in json.items() if k != "state_reason"})
            return FakeResponse(200, issue)
        match = re.fullmatch(r"/issues/(\d+)/comments", path)
        if match and method == "GET":
            bodies = self.gh_comments.get(int(match.group(1)), [])
            return self._page([{"body": b} for b in bodies], params)
        if match and method == "POST":
            self.gh_comments.setdefault(int(match.group(1)), []).append(json["body"])
            return FakeResponse(201, json)
        raise AssertionError(f"llamada no esperada: {method} {url}")

    @staticmethod
    def _page(items, params):
        size, page = int(params.get("per_page", 30)), int(params.get("page", 1))
        return FakeResponse(200, items[(page - 1) * size: page * size])

    def _graphql(self, payload):
        query, variables = payload["query"], payload["variables"]
        if "repositoryOwner" in query:
            assert variables == {"login": "yo", "number": 7}
            return FakeResponse(200, {"data": {"repositoryOwner": {"projectV2": {
                "id": "PROJ", "field": {"id": "FIELD", "options": [
                    {"id": "opt-todo", "name": "Todo"},
                    {"id": "opt-prog", "name": "In Progress"},
                    {"id": "opt-done", "name": "Done"}]}}}}})
        if "addProjectV2ItemById" in query:
            return FakeResponse(200, {"data": {"addProjectV2ItemById": {
                "item": {"id": "ITEM-" + variables["content"]}}}})
        if "updateProjectV2ItemFieldValue" in query:
            self.project_items[variables["item"]] = variables["option"]
            return FakeResponse(200, {"data": {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": variables["item"]}}}})
        raise AssertionError("consulta GraphQL no esperada")


def make_cfg(**extra):
    return Config(jira_base="https://sitio.atlassian.net", jira_email="yo@x.com",
                  jira_token="jira-token", jira_project="DEMO", gh_token="gh-token",
                  gh_repo="yo/demo", **extra)


def sample_world():
    issues = [
        jira_issue(1, "Falla el pago", category="done", kind="Bug", priority="High",
                   labels=["pagos"], links=["DEMO-3"], attachments=["captura.png"],
                   description="Lo vio @maria y se parece a #3"),
        jira_issue(2, "Renovar certificado", category="indeterminate"),
        jira_issue(3, "Conciliar pagos", category="new", kind="Story"),
        jira_issue(4, "Guía de instalación", category="done"),
        jira_issue(5, "Alertas de lentitud", category="new", links=["OTRO-9"]),
    ]
    comments = {
        "DEMO-1": [{"id": "101", "author": {"displayName": "Ana Pérez"},
                    "created": "2026-10-04T09:00:00.000-0300", "body": adf("Reproducido.")},
                   {"id": "102", "author": {"displayName": "Luis Gómez"},
                    "created": "2026-10-05T09:00:00.000-0300", "body": adf("Corregido.")},
                   {"id": "103", "author": {"displayName": "Ana Pérez"},
                    "created": "2026-10-06T09:00:00.000-0300", "body": adf("Verificado.")}],
        "DEMO-3": [{"id": "301", "author": {"displayName": "Luis Gómez"},
                    "created": "2026-10-07T09:00:00.000-0300", "body": adf("Hay 12 casos.")}],
    }
    return FakeSession(issues, comments)


def go(session, cfg=None, **kwargs):
    lines = []
    code = run(cfg or make_cfg(), session=session, pause=0, out=lines.append, **kwargs)
    return code, "\n".join(lines)


def test_la_simulacion_no_escribe_nada():
    world = sample_world()
    code, output = go(world)
    assert code == 0
    assert world.writes == []
    assert world.gh_issues == []
    assert "crear   [DEMO-1] Falla el pago" in output
    assert "crear 5 issues (0 ya existen), 4 comentarios" in output
    assert "cerrar 2 issues" in output


def test_migracion_completa():
    world = sample_world()
    code, output = go(world, apply=True)
    assert code == 0 and "Resultado: OK" in output

    assert [i["title"] for i in world.gh_issues] == [
        "[DEMO-1] Falla el pago", "[DEMO-2] Renovar certificado",
        "[DEMO-3] Conciliar pagos", "[DEMO-4] Guía de instalación",
        "[DEMO-5] Alertas de lentitud"]
    first = world.gh_issues[0]
    assert first["labels"] == ["tipo: bug", "prioridad: alta", "pagos"]
    assert "Luis Gómez" in first["body"] and "2026-10-03" in first["body"]
    assert "[captura.png](https://jira/att/captura.png)" in first["body"]
    assert "<!-- jira-key: DEMO-1 -->" in first["body"]

    # El enlace a DEMO-3 queda reescrito con el número de GitHub.
    assert "- blocks: #3 (DEMO-3)" in first["body"]
    # Un ticket de otro proyecto no se inventa: queda explicado.
    assert "OTRO-9 (de otro proyecto, no se migra)" in world.gh_issues[4]["body"]

    # Los terminados en Jira quedan cerrados; el resto, abiertos.
    assert [i["state"] for i in world.gh_issues] == ["closed", "open", "open", "closed", "open"]

    assert len(world.gh_comments[1]) == 3 and len(world.gh_comments[3]) == 1
    assert world.gh_comments[1][0].startswith("**Ana Pérez** comentó en Jira el 2026-10-04")
    assert {"tipo: bug", "tipo: tarea", "tipo: historia", "prioridad: alta",
            "prioridad: media", "pagos"} <= set(world.gh_labels)


def test_las_menciones_y_numerales_no_se_activan_en_github():
    world = sample_world()
    go(world, apply=True)
    body = world.gh_issues[0]["body"]
    assert "@maria" not in body and "@​maria" in body
    assert "#​3" in body       # el "#3" que venía escrito en Jira
    assert "#3 (DEMO-3)" in body    # el enlace real, generado por el script


def test_segunda_corrida_no_duplica_ni_modifica():
    world = sample_world()
    go(world, apply=True)
    issues_before = json.dumps(world.gh_issues, sort_keys=True)
    comments_before = json.dumps(world.gh_comments, sort_keys=True)
    world.writes.clear()

    code, output = go(world, apply=True)
    assert code == 0
    assert world.writes == []
    assert json.dumps(world.gh_issues, sort_keys=True) == issues_before
    assert json.dumps(world.gh_comments, sort_keys=True) == comments_before
    assert "0 issues creados, 5 ya existían, 0 comentarios" in output


def test_retoma_despues_de_una_caida():
    world = sample_world()
    world.fail_after_creates = 2
    try:
        go(world, apply=True)
        raise AssertionError("tenía que fallar")
    except migrate.ApiError:
        pass
    assert len(world.gh_issues) == 2 and world.gh_comments == {}

    world.fail_after_creates = None
    code, output = go(world, apply=True)
    assert code == 0 and "Resultado: OK" in output
    assert len(world.gh_issues) == 5
    assert len({i["title"] for i in world.gh_issues}) == 5
    assert len(world.gh_comments[1]) == 3
    assert "- blocks: #3 (DEMO-3)" in world.gh_issues[0]["body"]
    assert world.gh_issues[0]["state"] == "closed"


def test_la_verificacion_detecta_faltantes():
    world = sample_world()
    go(world, apply=True)
    world.gh_issues[1]["body"] = "alguien borró la marca"
    world.gh_comments[1].pop()
    code, output = go(world, only_verify=True)
    assert code == 1
    assert "Faltan migrar: DEMO-2" in output
    assert "Comentarios migrados:         3" in output
    assert "HAY DIFERENCIAS" in output


def test_ubica_cada_issue_en_la_columna_del_tablero():
    world = sample_world()
    code, _ = go(world, cfg=make_cfg(project_number=7, project_owner="yo"), apply=True)
    assert code == 0
    assert world.project_items == {
        "ITEM-NODE1": "opt-done", "ITEM-NODE2": "opt-prog", "ITEM-NODE3": "opt-todo",
        "ITEM-NODE4": "opt-done", "ITEM-NODE5": "opt-todo"}


def test_asigna_solo_a_los_usuarios_mapeados():
    world = sample_world()
    go(world, cfg=make_cfg(user_map={"Ana Pérez": "ana-gh"}), apply=True)
    assert world.gh_issues[0]["assignees"] == ["ana-gh"]
    world = sample_world()
    go(world, apply=True)
    assert world.gh_issues[0]["assignees"] == []
    assert "Asignado en Jira a:** Ana Pérez" in world.gh_issues[0]["body"]


def test_espera_y_reintenta_cuando_la_api_pide_frenar():
    world = sample_world()
    http = migrate.Http(world, pause=0)
    waits = []
    http.sleep = waits.append
    original, calls = world.request, {"n": 0}

    def flaky(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(429, {"message": "rate limit"}, {"Retry-After": "7"})
        return original(method, url, **kwargs)

    world.request = flaky
    labels = migrate.GitHubClient(http, make_cfg()).label_names()
    assert labels == {"bug"} and waits == [7]


def test_conversion_de_texto_de_jira_a_markdown():
    doc = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Pasos"}]},
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Hacer "},
            {"type": "text", "text": "esto ", "marks": [{"type": "strong"}]},
            {"type": "text", "text": "aquí", "marks": [
                {"type": "link", "attrs": {"href": "https://ejemplo.com/#12"}}]},
            {"type": "mention", "attrs": {"text": "@Ana Pérez"}}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "uno"}]},
                {"type": "orderedList", "content": [{"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "sub"}]}]}]}]},
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "dos"}]}]}]},
        {"type": "codeBlock", "attrs": {"language": "python"},
         "content": [{"type": "text", "text": "print('@x #1')"}]},
    ]}
    assert adf_to_markdown(doc) == (
        "## Pasos\n\n"
        "Hacer **esto** [aquí](https://ejemplo.com/#12)**Ana Pérez**\n\n"
        "- uno\n   1. sub\n- dos\n\n"
        "```python\nprint('@x #1')\n```"
    )
    assert adf_to_markdown(None) == ""


def test_neutralize_no_rompe_mails_ni_direcciones():
    assert neutralize("escribir a ana@ejemplo.com") == "escribir a ana@ejemplo.com"
    assert neutralize("ver https://x.com/a#12") == "ver https://x.com/a#12"
    assert neutralize("gracias @juan, es el #45") == "gracias @​juan, es el #​45"
