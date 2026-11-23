# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
import pytest

from gatewright.config import ConfigError, load_pipeline
from gatewright.graph import levels

from .conftest import BASIC_PIPELINE


def test_valid_pipeline(project):
    root = project(BASIC_PIPELINE, """\
        jobs:
          - {id: a, gate: commit, run: "true"}
          - {id: b, gate: commit, run: "true", needs: [a], resources: {cpu: 0.5, mem: 1GB}}
          - {id: c, gate: test, run: "true", needs: [a]}
    """)
    p = load_pipeline(root)
    assert [g.id for g in p.gates] == ["commit", "test"]
    assert p.job("b").mem == 1024**3
    waves = levels(p.jobs_in("commit"))
    assert [[j.id for j in w] for w in waves] == [["a"], ["b"]]


@pytest.mark.parametrize("jobs, expected", [
    ('jobs:\n  - {id: a, gate: commit, run: "true", colour: red}', "unknown key"),
    ('jobs:\n  - {id: a, gate: commit, run: "true", category: qa}', "must be one of"),
    ('jobs:\n  - {id: a, gate: deploy, run: "true"}', "unknown gate 'deploy'"),
    ('jobs:\n  - {id: a, gate: commit}', "missing required key: 'run'"),
    ('jobs:\n  - {id: a, gate: commit, run: "true", needs: [zz]}', "needs unknown job 'zz'"),
    ('jobs:\n  - {id: a, gate: commit, run: "true", needs: [b]}\n  - {id: b, gate: commit, run: "true", needs: [a]}',
     "dependency cycle"),
    ('jobs:\n  - {id: a, gate: commit, run: "true", needs: [b]}\n  - {id: b, gate: test, run: "true"}',
     "from a later gate"),
    ('jobs:\n  - {id: a, gate: commit, run: "true"}\n  - {id: a, gate: commit, run: "true"}', "duplicate job id"),
    ('jobs: [ {id: a', "invalid YAML"),
])
def test_config_errors_are_human(project, jobs, expected):
    root = project(BASIC_PIPELINE, jobs)
    with pytest.raises(ConfigError) as ei:
        load_pipeline(root)
    assert any(expected in p for p in ei.value.problems), ei.value.problems


def test_missing_pipeline(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_pipeline(tmp_path)
    assert "gatewright init" in ei.value.problems[0]


def test_init_templates_are_valid(tmp_path):
    from gatewright.templates import init
    for tpl in ("python", "node", "docker"):
        d = tmp_path / tpl
        d.mkdir()
        init(d, tpl)
        p = load_pipeline(d)
        assert p.jobs and p.gates


def test_builtin_validator_matches_jsonschema():
    """Gatewright validates config without the jsonschema package; make sure it agrees."""
    jsonschema = pytest.importorskip("jsonschema")
    import random
    from gatewright.config import JOB_SCHEMA, PIPELINE_SCHEMA
    from gatewright.schema_check import iter_errors
    rng = random.Random(7)
    vals = [None, True, 0, -1, 0.5, 3, 11, "", "x", "bad id!", "dev", "qa", "512MB", "1.5 GB", "lots", [], ["a"],
            [1], {}, {"type": "junit"}, {"type": "junit", "x": 1}, {"cpu": 0}, {"cpu": 2, "mem": "1GB"}]
    for schema, keys in ((JOB_SCHEMA, list(JOB_SCHEMA["properties"]) + ["colour"]),
                         (PIPELINE_SCHEMA, list(PIPELINE_SCHEMA["properties"]) + ["colour"])):
        for _ in range(1500):
            doc = {k: rng.choice(vals) for k in rng.sample(keys, rng.randint(0, 6))}
            expected = not list(jsonschema.Draft202012Validator(schema).iter_errors(doc))
            assert (not list(iter_errors(doc, schema))) == expected, doc



def test_yaml_on_off_yes_no_are_text(project):
    """`on:` must stay the key "on" (PyYAML's YAML 1.1 default would make it True)."""
    from gatewright.config import load_yaml_text
    assert load_yaml_text("on: [a]\nx: yes\ny: off\nz: true\nw: False") == {"on": ["a"], "x": "yes", "y": "off",
                                                                          "z": True, "w": False}
    root = project(BASIC_PIPELINE + "notify:\n  on: [run_failed]\n  channels:\n    - type: ntfy\n      url: http://x\n      on: [deployed]\n",
                   'jobs:\n  - {id: a, gate: commit, run: "echo hi", cache: yes}\n')
    with pytest.raises(ConfigError, match="cache"):   # `yes` is not a boolean any more: clear error
        load_pipeline(root)
    (root / ".gatewright" / "jobs" / "jobs.yml").write_text('jobs:\n  - {id: a, gate: commit, run: "echo hi"}\n')
    p = load_pipeline(root)
    assert p.notify["on"] == ["run_failed"] and p.notify["channels"][0]["on"] == ["deployed"]
