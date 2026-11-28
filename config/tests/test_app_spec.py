"""Tests for the App Platform app spec .do/production.yaml (issue #61).

There is no staging environment. The production spec has web, worker,
beat, a pre-deploy migrate job, managed Postgres, and managed Valkey. Settings must start from the
values the spec gives, including the connection strings DigitalOcean
binds, shown here with made-up hosts and passwords.
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml

from config.settings import _RESERVED_ADMIN_SEGMENTS
from config.tests.test_celery import QUEUES
from config.tests.test_settings import PRODUCTION_LIKE, TEST_SECRET_KEY, _run

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# A made-up password that must never appear in any output.
DO_MARKER = "not-a-real-do-pw-MARKER"
PG_HOST = "open-production-pg-do-user-1-0.f.db.ondigitalocean.example.org"
VALKEY_HOST = "open-production-valkey-do-user-1-0.f.db.ondigitalocean.example.org"

# The shapes DigitalOcean binds: the cluster's own connection string, the
# pool's (the pool name in place of the database), and Valkey's (TLS).
DO_DIRECT_URL = f"postgresql://open:{DO_MARKER}@{PG_HOST}:25060/open?sslmode=require"
DO_POOL_URL = f"postgresql://open:{DO_MARKER}@{PG_HOST}:25061/open-pool?sslmode=require"
DO_VALKEY_URL = f"rediss://default:{DO_MARKER}@{VALKEY_HOST}:25061"

PRINT_DEPLOYED = """
import json
import django
from django.conf import settings
django.setup()
out = {'REDIS_URL_OK': settings.REDIS_URL.startswith('rediss://')}
for alias, db in settings.DATABASES.items():
    out[alias] = {
        'NAME': db['NAME'],
        'PORT': str(db['PORT']),
        'SSLMODE': db.get('OPTIONS', {}).get('sslmode'),
        'CONN_MAX_AGE': db['CONN_MAX_AGE'],
    }
print(json.dumps(out))
"""


def _deployed(values: dict[str, str]) -> dict[str, Any]:
    result = _run(["-c", PRINT_DEPLOYED], values)
    assert DO_MARKER not in result.stdout + result.stderr
    if result.returncode != 0:
        raise AssertionError(f"settings failed to load:\n{result.stderr}")
    out: dict[str, Any] = json.loads(result.stdout)
    return out


DO_VALUES = {
    **PRODUCTION_LIKE,
    "DATABASE_URL": DO_POOL_URL,
    "DIRECT_DATABASE_URL": DO_DIRECT_URL,
    "REDIS_URL": DO_VALKEY_URL,
}


def test_settings_accept_digitalocean_connection_strings() -> None:
    out = _deployed(DO_VALUES)

    assert out["REDIS_URL_OK"] is True
    assert out["default"] == {
        "NAME": "open-pool",
        "PORT": "25061",
        "SSLMODE": "require",
        "CONN_MAX_AGE": 0,
    }
    assert out["direct"] == {
        "NAME": "open",
        "PORT": "25060",
        "SSLMODE": "require",
        "CONN_MAX_AGE": 0,
    }


def test_settings_accept_the_cluster_default_database_name() -> None:
    direct = DO_DIRECT_URL.replace("/open?", "/defaultdb?")
    out = _deployed({**DO_VALUES, "DIRECT_DATABASE_URL": direct})

    assert out["direct"]["NAME"] == "defaultdb"


# The spec ---------------------------------------------------------------

SPEC_PATH = BASE_DIR / ".do" / "production.yaml"

SLUG = "apps-s-1vcpu-0.5gb"
IMAGE = {
    "registry_type": "GHCR",
    "registry": "openpickleball",
    "repository": "backend",
    "tag": "main",
}
WORKER_COMMAND = (
    "celery -A config worker -Q ratings,email,webhooks,default -l info --concurrency 2"
)
BEAT_COMMAND = (
    "celery -A config beat -l info "
    "--scheduler django_celery_beat.schedulers:DatabaseScheduler"
)
MIGRATE_COMMAND = "python manage.py migrate"

SECRETS = {
    "DJANGO_SECRET_KEY",
    "FIELD_ENCRYPTION_KEYS",
    "RESEND_API_KEY",
    "ADMIN_URL",
}
BINDINGS = {
    "DATABASE_URL": "${pg.open-pool.DATABASE_URL}",
    "DIRECT_DATABASE_URL": "${pg.DATABASE_URL}",
    "REDIS_URL": "${valkey.DATABASE_URL}",
}
LITERALS = {
    "DJANGO_DEBUG": "false",
    "EMAIL_BACKEND": "anymail.backends.resend.EmailBackend",
    "DEFAULT_FROM_EMAIL": "OPEN <noreply@mail.openpb.org>",
    # The two hosts (#183, ADR 0063), never ${APP_DOMAIN}.
    "DJANGO_ALLOWED_HOSTS": "api.openpb.org,app.openpb.org",
}
FRONTEND_URL = "https://app.openpb.org"
API_HOST = "api.openpb.org"
APP_HOST = "app.openpb.org"
# The paths Django answers on the PWA's host (ADR 0061).
APP_HOST_PREFIXES = ("/api/", "/_allauth/", "/oauth/")

# Made-up values for the bindable variables, as App Platform fills them.
BOUND = {
    "${pg.open-pool.DATABASE_URL}": DO_POOL_URL,
    "${pg.DATABASE_URL}": DO_DIRECT_URL,
    "${valkey.DATABASE_URL}": DO_VALKEY_URL,
}


def _spec() -> dict[str, Any]:
    spec: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text())
    return spec


def _components(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *spec.get("services", []),
        *spec.get("workers", []),
        *spec.get("jobs", []),
    ]


def _envs(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {env["key"]: env for env in spec["envs"]}


def _one(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [item for item in items if item["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


def test_spec_has_exactly_the_components() -> None:
    spec = _spec()

    assert spec["name"] == "open-backend-production"
    assert [c["name"] for c in spec["services"]] == ["web"]
    assert [c["name"] for c in spec["workers"]] == ["worker", "beat"]
    assert [c["name"] for c in spec["jobs"]] == ["migrate"]
    assert [d["name"] for d in spec["databases"]] == ["pg", "valkey"]
    assert set(spec) <= {
        "name",
        "envs",
        "services",
        "workers",
        "jobs",
        "databases",
        "domains",
        "ingress",
    }


def test_web_serves_the_image_command_with_both_health_checks() -> None:
    web = _one(_spec()["services"], "web")

    # The image's command is gunicorn; App Platform sets PORT.
    assert "run_command" not in web
    assert web["http_port"] == 8000
    assert web["health_check"]["http_path"] == "/api/v1/health/ready"
    assert web["liveness_health_check"]["http_path"] == "/api/v1/health/live"
    for check in ("health_check", "liveness_health_check"):
        assert "port" not in web[check]
        assert "tcp" not in str(web[check]).lower()


def test_worker_consumes_every_queue() -> None:
    worker = _one(_spec()["workers"], "worker")

    assert worker["run_command"] == WORKER_COMMAND
    options = re.findall(r"-Q\s+([\w,]+)", worker["run_command"])
    assert len(options) == 1
    assert set(options[0].split(",")) == QUEUES


def test_beat_runs_once_with_the_database_scheduler() -> None:
    beat = _one(_spec()["workers"], "beat")

    assert beat["run_command"] == BEAT_COMMAND
    assert beat["instance_count"] == 1
    assert "autoscaling" not in beat


def test_migrate_runs_before_each_deploy() -> None:
    migrate = _one(_spec()["jobs"], "migrate")

    assert migrate["kind"] == "PRE_DEPLOY"
    # No --database: OPEN's migrate uses the direct URL itself (ADR 0038).
    assert migrate["run_command"] == MIGRATE_COMMAND


def test_databases_are_managed_production_clusters() -> None:
    spec = _spec()
    pg = _one(spec["databases"], "pg")
    valkey = _one(spec["databases"], "valkey")

    assert pg == {
        "name": "pg",
        "engine": "PG",
        "production": True,
        "cluster_name": "open-production-pg",
        "db_name": "open",
        "db_user": "open",
    }
    assert valkey == {
        "name": "valkey",
        "engine": "VALKEY",
        "production": True,
        "cluster_name": "open-production-valkey",
    }


def test_every_component_uses_the_smallest_slug_and_one_image() -> None:
    for component in _components(_spec()):
        assert component["instance_size_slug"] == SLUG, component["name"]
        assert component["image"] == IMAGE, component["name"]
        # Shared values are set once, at app level.
        assert "envs" not in component, component["name"]


def test_connection_strings_are_bound_at_app_level() -> None:
    envs = _envs(_spec())

    for key, binding in BINDINGS.items():
        assert envs[key]["value"] == binding, key
        assert envs[key]["scope"] == "RUN_TIME", key


def test_secrets_have_no_value() -> None:
    envs = _envs(_spec())

    for key in SECRETS:
        assert envs[key]["type"] == "SECRET", key
        assert "value" not in envs[key], key
    for key, env in envs.items():
        if key not in SECRETS:
            assert env.get("type", "GENERAL") == "GENERAL", key


def test_no_literal_holds_credentials_or_celery_urls() -> None:
    envs = _envs(_spec())

    for key, env in envs.items():
        value = str(env.get("value", ""))
        assert not re.search(r"://[^/\s]*@", value), key
        assert not key.startswith("CELERY_"), key
    # The client IP header stays at its default (ADR 0030).
    assert "CLIENT_IP_HEADER" not in envs


def test_env_values_are_exactly_the_planned_ones() -> None:
    envs = _envs(_spec())
    values = {key: env.get("value") for key, env in envs.items()}

    assert values == {
        **BINDINGS,
        **LITERALS,
        "FRONTEND_URL": FRONTEND_URL,
        **dict.fromkeys(SECRETS),
    }


def test_no_region_is_set() -> None:
    assert "region" not in _spec()


def _bound_env() -> dict[str, str]:
    envs = _envs(_spec())
    values: dict[str, str] = {}
    for key, env in envs.items():
        value = env.get("value")
        if value is None:
            continue
        values[key] = BOUND.get(value, value)
    values["DJANGO_SECRET_KEY"] = TEST_SECRET_KEY
    values["FIELD_ENCRYPTION_KEYS"] = PRODUCTION_LIKE["FIELD_ENCRYPTION_KEYS"]
    values["RESEND_API_KEY"] = PRODUCTION_LIKE["RESEND_API_KEY"]
    values["ADMIN_URL"] = PRODUCTION_LIKE["ADMIN_URL"]
    return values


def test_settings_start_from_the_spec_values() -> None:
    result = _run(["manage.py", "check", "--deploy"], _bound_env())

    output = result.stdout + result.stderr
    assert result.returncode == 0, result.stderr
    assert "System check identified no issues (0 silenced)." in output
    assert DO_MARKER not in output
    for key in ("DATABASE_URL", "REDIS_URL"):
        assert BOUND[BINDINGS[key]] not in output


# Hosts and routes (#183, ADR 0063) ---------------------------------------


def _rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = _spec()["ingress"]["rules"]
    return rules


def test_the_app_answers_on_the_api_and_pwa_hosts() -> None:
    domains = _spec()["domains"]

    assert [(d["domain"], d["type"]) for d in domains] == [
        (API_HOST, "PRIMARY"),
        (APP_HOST, "ALIAS"),
    ]
    for domain in domains:
        assert domain["minimum_tls_version"] == "1.2", domain["domain"]
        # A wildcard would put every subdomain's traffic on Django.
        assert not domain.get("wildcard", False), domain["domain"]
        # DNS for openpb.org is not on DigitalOcean.
        assert "zone" not in domain, domain["domain"]


def test_every_rule_names_its_host_and_keeps_the_path() -> None:
    # App Platform does not document the order it tries rules in, so no
    # rule may rely on it: each one names its exact host.
    for rule in _rules():
        assert set(rule["match"]) == {"authority", "path"}, rule
        assert set(rule["match"]["authority"]) == {"exact"}, rule
        assert set(rule["match"]["path"]) == {"prefix"}, rule
        assert rule["component"] == {"name": "web", "preserve_path_prefix": True}
        assert "redirect" not in rule
        assert "cors" not in rule


def test_django_answers_only_the_api_paths_on_the_pwa_host() -> None:
    routed = {
        (r["match"]["authority"]["exact"], r["match"]["path"]["prefix"])
        for r in _rules()
    }

    assert routed == {
        *((APP_HOST, prefix) for prefix in APP_HOST_PREFIXES),
        (API_HOST, "/"),
    }


def test_the_admin_path_is_never_routed_on_the_pwa_host() -> None:
    # Settings refuse an ADMIN_URL that starts with a path routed to
    # Django on the PWA's host, so the admin answers on api.openpb.org
    # only (ADR 0063).
    pwa_segments = {
        r["match"]["path"]["prefix"].strip("/")
        for r in _rules()
        if r["match"]["authority"]["exact"] == APP_HOST
    }

    assert pwa_segments <= _RESERVED_ADMIN_SEGMENTS


def test_allowed_hosts_are_the_two_hosts_and_no_origin_is_trusted() -> None:
    envs = _envs(_spec())
    hosts = envs["DJANGO_ALLOWED_HOSTS"]["value"].split(",")
    domains = [d["domain"] for d in _spec()["domains"]]

    assert hosts == domains
    assert envs["FRONTEND_URL"]["value"] == f"https://{APP_HOST}"
    # Same-origin requests need no trusted origin, and a listed host
    # would pass the origin check on the other host (ADR 0063).
    assert "CSRF_TRUSTED_ORIGINS" not in envs
