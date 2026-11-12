"""Prueba del cargador de tickets de ejemplo contra un Jira simulado."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import seed_jira  # noqa: E402
from migrate import Config, parse_ticket  # noqa: E402
from test_migrate import FakeResponse  # noqa: E402


class FakeJira:
    def __init__(self, with_priority=True):
        self.with_priority = with_priority
        self.issues, self.comments, self.status = {}, {}, {}
        self.links, self.attachments = [], []

    def request(self, method, url, json=None, auth=None, headers=None, files=None, **_):
        assert auth == ("yo@x.com", "jira-token")
        path = url.split("/rest/api/3", 1)[1]
        if path == "/search/jql":
            return FakeResponse(200, {"issues": []})
        if path == "/project/DEMO":
            return FakeResponse(200, {"issueTypes": [
                {"id": "1", "name": "Tarea"}, {"id": "2", "name": "Error"},
                {"id": "3", "name": "Subtarea", "subtask": True}]})
        if path == "/issue" and method == "POST":
            if "priority" in json["fields"] and not self.with_priority:
                return FakeResponse(400, {"errors": {"priority": "no se puede definir"}})
            key = f"DEMO-{len(self.issues) + 1}"
            self.issues[key] = json["fields"]
            return FakeResponse(201, {"key": key})
        match = re.fullmatch(r"/issue/(DEMO-\d+)/(comment|transitions|attachments)", path)
        if match:
            key, what = match.groups()
            if what == "comment":
                self.comments.setdefault(key, []).append(json["body"])
                return FakeResponse(201, {})
            if what == "transitions" and method == "GET":
                return FakeResponse(200, {"transitions": [
                    {"id": "11", "to": {"statusCategory": {"key": "indeterminate"}}},
                    {"id": "21", "to": {"statusCategory": {"key": "done"}}}]})
            if what == "transitions":
                self.status[key] = json["transition"]["id"]
                return FakeResponse(204, {})
            assert headers["X-Atlassian-Token"] == "no-check" and files
            self.attachments.append(key)
            return FakeResponse(200, [])
        if path == "/issueLinkType":
            return FakeResponse(200, {"issueLinkTypes": [{"name": "Relates"}, {"name": "Blocks"}]})
        if path == "/issueLink":
            self.links.append((json["type"]["name"], json["inwardIssue"]["key"],
                               json["outwardIssue"]["key"]))
            return FakeResponse(201, {})
        raise AssertionError(f"llamada no esperada: {method} {url}")


CFG = Config(jira_base="https://sitio.atlassian.net", jira_email="yo@x.com",
             jira_token="jira-token", jira_project="DEMO", gh_token="x", gh_repo="yo/demo")


def test_carga_los_veinte_tickets():
    jira = FakeJira()
    keys = seed_jira.seed(CFG, session=jira, out=lambda *_: None)
    assert len(keys) == 20 == len(jira.issues)
    assert jira.issues["DEMO-1"]["issuetype"] == {"id": "2"}       # bug -> "Error"
    assert jira.issues["DEMO-4"]["issuetype"] == {"id": "1"}       # story -> no existe, usa Tarea
    assert all("demo-migracion" in f["labels"] for f in jira.issues.values())
    assert sum(len(c) for c in jira.comments.values()) == sum(len(t[6]) for t in seed_jira.TICKETS)
    assert jira.status["DEMO-1"] == "21" and jira.status["DEMO-2"] == "11"
    assert "DEMO-5" not in jira.status
    assert jira.links == [("Blocks", "DEMO-1", "DEMO-16"), ("Blocks", "DEMO-10", "DEMO-6")]
    assert jira.attachments == ["DEMO-2"]


def test_reintenta_sin_prioridad_si_el_proyecto_no_la_tiene():
    jira = FakeJira(with_priority=False)
    seed_jira.seed(CFG, session=jira, out=lambda *_: None)
    assert len(jira.issues) == 20
    assert all("priority" not in f for f in jira.issues.values())


def test_los_textos_de_ejemplo_se_convierten_bien():
    kind, priority, status, labels, title, description, comments = seed_jira.TICKETS[0]
    ticket = parse_ticket({"key": "DEMO-1", "fields": {"summary": title, "description": description}}, [])
    assert "**$1.250,50**" in ticket.description
    assert "- Reservar un turno" in ticket.description
    assert "```json" in ticket.description
