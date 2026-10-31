"""The release generator on throwaway Git repositories; nothing is built or pushed."""
import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
import release

try:
    import yaml  # noqa: F401
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

DIGEST = "@sha256:" + "a" * 64
DESCRIPTOR = {
    "descriptor": 1, "product": "pilot",
    "shared": ["codebase/pilot.backend-services/Directory.*.props"],
    "services": {
        "api": {"build": {"dockerfile": "deployment/backend.Dockerfile", "args": {"SERVICE": "Api"}},
                "paths": ["codebase/pilot.backend-services/Pilot.Api/**"], "health": "/health",
                "settings": {"required": ["Database:Connection"]}, "volumes": {"keys": "/data/keys"},
                "sites": {"app": {"path": "/api"}}, "needs": ["postgres"]},
        "edge": {"build": {"dockerfile": "deployment/edge.Dockerfile", "contexts": {"deployment": "deployment"}},
                 "paths": ["codebase/pilot.frontend-services/**", "deployment/edge/**"], "port": 80,
                 "health": {"command": ["wget", "-q", "-O-", "http://localhost/healthz"]},
                 "capabilities": ["NET_BIND_SERVICE"], "sites": {"app": None, "landing": {"exposure": "public"}}},
    },
}


def git(repo, *arguments):
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True).stdout.strip()


class DescriptorTests(unittest.TestCase):
    def test_defaults_fill_every_service(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        api, edge = descriptor["services"]["api"], descriptor["services"]["edge"]
        self.assertEqual(("linux/amd64", 8080, "512m", "30s", "codebase"),
                         (descriptor["platform"], api["port"], api["memory"], api["stopGrace"], api["build"]["context"]))
        self.assertEqual(["CMD", "curl", "--fail", "--silent", "http://localhost:8080/health"], api["health"])
        self.assertEqual({"file": "/app/appsettings.Target.json", "required": ["Database:Connection"]}, api["settings"])
        self.assertEqual({"app": {"port": 80, "path": "/", "exposure": "public"},
                          "landing": {"port": 80, "path": "/", "exposure": "public"}}, edge["sites"])
        self.assertEqual(["CMD", "wget", "-q", "-O-", "http://localhost/healthz"], edge["health"])

    def test_an_explicit_settings_file_keeps_its_path(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["api"]["settings"]["file"] = "/app/appsettings.Local.json"
        api = release.validate(declared)["services"]["api"]
        self.assertEqual({"file": "/app/appsettings.Local.json", "required": ["Database:Connection"]}, api["settings"])

    def test_rule_breaks_name_the_rule(self):
        cases = (
            (lambda d: d.update(extra=1), "Unknown top-level key"),
            (lambda d: d.update(product="Pilot"), "product must be"),
            (lambda d: d.update(descriptor=3), "descriptor must be 1 or 2"),
            (lambda d: d.update(sites={"app": {}}), "Top-level sites need descriptor 2"),
            (lambda d: d["services"]["api"].pop("health"), "health must be"),
            (lambda d: d["services"]["api"].update(paths=["../outside/**"]), "relative to engineering"),
            (lambda d: d["services"]["api"].update(settings={"file": "appsettings.json"}), "absolute path"),
            (lambda d: d["services"]["api"].update(sites={"app": {}}), "already serves site app"),
            (lambda d: d["services"]["api"].update(needs=["mongo"]), "needs takes"),
            (lambda d: d["services"]["api"].update(memory="lots"), "memory must"),
            (lambda d: d["services"]["api"].update(image="ghcr.io/o/api:1"), "without a tag"),
            (lambda d: d["services"]["api"].update(unknown=True), "unknown key"),
            (lambda d: d.update(dotnetEnvironment="yes"), "dotnetEnvironment must be"),
            (lambda d: d.update(verify={"service": "worker", "command": ["true"]}), "verify service must"),
            (lambda d: d.update(verify={"service": "api", "command": "curl"}), "verify command must"),
            (lambda d: d.update(verify={"service": "api", "command": ["true"], "timeout": "2h"}), "verify timeout must be"),
            (lambda d: d.update(verify={"service": "api", "command": ["true"], "timeout": 60}), "verify timeout must look"),
            (lambda d: d.update(verify={"service": "api", "command": ["true"], "image": "x"}), "verify takes"),
            (lambda d: d["services"]["edge"].update(dependsOn="api"), "dependsOn must list"),
            (lambda d: d["services"]["edge"].update(dependsOn=["api", "api"]), "dependsOn must list"),
            (lambda d: d["services"]["edge"].update(dependsOn=["edge"]), "cannot depend on itself"),
            (lambda d: d["services"]["edge"].update(dependsOn=["worker"]), "dependsOn must name a service of this product$"),
            (lambda d: d["services"]["api"].update(dependsOn=["postgres"]), "platform services go under needs"),
            (lambda d: (d["services"]["edge"].update(dependsOn=["api"]), d["services"]["api"].update(dependsOn=["edge"])),
             "dependsOn forms a cycle: api, edge"),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                descriptor = copy.deepcopy(DESCRIPTOR)
                mutate(descriptor)
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(descriptor)

    def test_globs_match_inside_engineering_only(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        self.assertEqual(["api"], release.changed_services(descriptor, [
            "engineering/codebase/pilot.backend-services/Pilot.Api/Controllers/Home.cs"]))
        self.assertEqual(["edge"], release.changed_services(descriptor, ["engineering/deployment/edge/Caddyfile"]))
        self.assertEqual(["api", "edge"], release.changed_services(descriptor, [
            "engineering/deployment/backend.Dockerfile", "engineering/deployment/edge.Dockerfile"]))
        self.assertEqual([], release.changed_services(descriptor, ["README.md", "codebase/pilot.backend-services/x.cs"]))

    def test_a_shared_path_changes_every_service(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        self.assertEqual(["api", "edge"], release.changed_services(descriptor, [
            "engineering/codebase/pilot.backend-services/Directory.Packages.props"]))

    def test_the_environment_mapping_and_the_dev_check_are_opt_in(self):
        plain = release.validate(copy.deepcopy(DESCRIPTOR))
        self.assertEqual((False, None), (plain["dotnetEnvironment"], plain["verify"]))
        declared = copy.deepcopy(DESCRIPTOR)
        declared.update(dotnetEnvironment=True, verify={"service": "api", "command": ["dotnet", "Pilot.Verify.dll"]})
        descriptor = release.validate(declared)
        self.assertTrue(descriptor["dotnetEnvironment"])
        self.assertEqual({"service": "api", "command": ["dotnet", "Pilot.Verify.dll"], "timeoutSeconds": 300},
                         descriptor["verify"])
        declared["verify"]["timeout"] = "90s"
        self.assertEqual(90, release.validate(declared)["verify"]["timeoutSeconds"])

    def test_dependencies_are_opt_in_and_ordered(self):
        self.assertEqual([], release.validate(copy.deepcopy(DESCRIPTOR))["services"]["edge"]["dependsOn"])
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["worker"] = {"build": {"dockerfile": "deployment/backend.Dockerfile"},
                                          "paths": ["codebase/pilot.worker/**"], "health": "/health",
                                          "dependsOn": ["api", "edge"]}
        declared["services"]["edge"]["dependsOn"] = ["api"]
        descriptor = release.validate(declared)
        self.assertEqual(["api", "edge"], descriptor["services"]["worker"]["dependsOn"])
        self.assertEqual(["api", "edge", "worker"], release.dependency_order(descriptor["services"]))

    def test_typed_keys_join_the_required_list_and_take_checked_defaults(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["api"]["settings"]["keys"] = {
            "Billing:Mode": {"type": "choice", "values": ["test", "live"], "default": "test"},
            "Stripe:ApiKey": {"secret": True, "required": True},
            "Jobs:Cleanup": {"type": "cron", "default": "0 3 * * *"},
            "Promo:EndsOn": {"type": "date", "format": "dd.MM.yyyy"},
            "Limits:MaxUsers": {"type": "number", "default": 50},
            "Features:Beta": {"type": "boolean", "default": False},
            "Support:Url": {"type": "url", "required": True},
            "Support:Email": {"type": "email"},
            "Notes:Banner": None,
        }
        settings = release.validate(declared)["services"]["api"]["settings"]
        self.assertEqual(["Database:Connection", "Stripe:ApiKey", "Support:Url"], settings["required"])
        keys = settings["keys"]
        self.assertEqual({"type": "choice", "required": False, "secret": False, "default": "test", "values": ["test", "live"]},
                         keys["Billing:Mode"])
        self.assertEqual(("50", "false"), (keys["Limits:MaxUsers"]["default"], keys["Features:Beta"]["default"]))
        self.assertEqual("dd.MM.yyyy", keys["Promo:EndsOn"]["format"])
        self.assertEqual({"type": "text", "required": False, "secret": False}, keys["Notes:Banner"])
        # A descriptor without typed keys validates as it always did.
        self.assertNotIn("keys", release.validate(copy.deepcopy(DESCRIPTOR))["services"]["api"]["settings"])

    def test_typed_key_rule_breaks_name_the_rule(self):
        cases = (
            ({"Bad Key": {}}, "must be a configuration path"),
            ({"A:B": {"type": "colour"}}, "type must be one of"),
            ({"A:B": {"kind": "text"}}, "takes type, required, secret"),
            ({"A:B": {"type": "choice"}}, "a choice lists 1-50 values"),
            ({"A:B": {"type": "choice", "values": ["x", "x"]}}, "must be distinct"),
            ({"A:B": {"values": ["x"]}}, "only a choice lists values"),
            ({"A:B": {"format": "yyyy"}}, "only a date takes a format"),
            ({"A:B": {"type": "date", "format": "%Y-%m"}}, "a date format uses"),
            ({"A:B": {"secret": True, "default": "x"}}, "a secret takes no default"),
            ({"A:B": {"required": True, "default": "x"}}, "a required key takes no default"),
            ({"A:B": {"required": "yes"}}, "required and secret must be true or false"),
            ({"A:B": {"type": "number", "default": "ten"}}, "default must be a number"),
            ({"A:B": {"type": "boolean", "default": "yes"}}, "default must be true or false"),
            ({"A:B": {"type": "choice", "values": ["a"], "default": "b"}}, "default must be one of the values"),
            ({"A:B": {"type": "url", "default": "example.com"}}, "default must be an absolute http"),
            ({"A:B": {"type": "cron", "default": "61 * * * *"}}, "default must be a cron expression"),
            ({"A:B": {"type": "date", "default": "2026-02-30"}}, "default must be a date as yyyy-MM-dd"),
            ({"A:B": {"type": "email", "default": "owner"}}, "default must be an email address"),
            ([], "settings keys must map keys"),
        )
        for keys, message in cases:
            with self.subTest(message=message):
                declared = copy.deepcopy(DESCRIPTOR)
                declared["services"]["api"]["settings"]["keys"] = keys
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(declared)

    def test_generate_keys_fill_their_defaults_and_follow_their_own_required(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["api"]["settings"]["keys"] = {
            "Auth:ClientSecret": {"secret": True, "required": True, "generate": {"random": 32}},
            "Auth:Salt": {"type": "text", "secret": True, "generate": {"random": 16, "encoding": "hex"}},
            "Auth:Pepper": {"secret": True, "generate": {"random": 64, "encoding": "base64url"}},
            "Auth:SigningKey": {"secret": True, "required": True,
                                "generate": {"command": ["dotnet", "Pilot.Api.dll", "keys", "generate"]}},
            "Auth:Shortest": {"secret": True, "generate": {"command": ["keygen"], "timeout": "10s"}},
            "Auth:Longest": {"secret": True, "generate": {"command": ["keygen"], "timeout": "10m"}},
            "Auth:Seconds": {"secret": True, "generate": {"command": ["keygen", "--bits", "256"], "timeout": "600s"}},
        }
        settings = release.validate(declared)["services"]["api"]["settings"]
        # A generate key follows its own `required`, like any typed key.
        self.assertEqual(["Database:Connection", "Auth:ClientSecret", "Auth:SigningKey"], settings["required"])
        keys = settings["keys"]
        self.assertEqual({"type": "text", "required": True, "secret": True,
                          "generate": {"random": 32, "encoding": "base64url"}}, keys["Auth:ClientSecret"])
        self.assertEqual({"random": 16, "encoding": "hex"}, keys["Auth:Salt"]["generate"])
        self.assertEqual({"random": 64, "encoding": "base64url"}, keys["Auth:Pepper"]["generate"])
        self.assertEqual({"command": ["dotnet", "Pilot.Api.dll", "keys", "generate"], "timeoutSeconds": 120},
                         keys["Auth:SigningKey"]["generate"])
        self.assertEqual(10, keys["Auth:Shortest"]["generate"]["timeoutSeconds"])
        self.assertEqual(600, keys["Auth:Longest"]["generate"]["timeoutSeconds"])
        self.assertEqual(600, keys["Auth:Seconds"]["generate"]["timeoutSeconds"])

    def test_generate_rule_breaks_name_the_rule(self):
        make = {"random": 32}
        cases = (
            ({"A:B": {"generate": make}}, "generate needs a secret key"),
            ({"A:B": {"secret": False, "generate": make}}, "generate needs a secret key"),
            ({"A:B": {"secret": True, "type": "number", "generate": make}}, "generate needs a text key"),
            ({"A:B": {"secret": True, "type": "email", "generate": make}}, "generate needs a text key"),
            ({"A:B": {"secret": True, "default": "x", "generate": make}}, "generate cannot sit beside default"),
            ({"A:B": {"default": "x", "generate": make}}, "generate cannot sit beside default"),
            ({"A:B": {"secret": True, "from": "A:C", "generate": make}, "A:C": {}}, "generate cannot sit beside from"),
            ({"A:B": {"secret": True, "generate": None}}, "generate must be exactly one of random"),
            ({"A:B": {"secret": True, "generate": "random"}}, "generate must be exactly one of random"),
            ({"A:B": {"secret": True, "generate": {}}}, "generate must be exactly one of random"),
            ({"A:B": {"secret": True, "generate": {"length": 32}}}, "generate must be exactly one of random"),
            ({"A:B": {"secret": True, "generate": {"random": 32, "command": ["x"]}}}, "generate must be exactly one of"),
            ({"A:B": {"secret": True, "generate": {"random": 32, "timeout": "2m"}}}, "random takes only random and encoding"),
            ({"A:B": {"secret": True, "generate": {"random": 32, "length": 1}}}, "random takes only random and encoding"),
            ({"A:B": {"secret": True, "generate": {"command": ["x"], "encoding": "hex"}}},
             "command takes only command and timeout"),
            ({"A:B": {"secret": True, "generate": {"command": ["x"], "random": 32}}}, "generate must be exactly one of"),
            ({"A:B": {"secret": True, "generate": {"random": 15}}}, "random must be a whole number of bytes, 16-64"),
            ({"A:B": {"secret": True, "generate": {"random": 65}}}, "random must be a whole number of bytes, 16-64"),
            ({"A:B": {"secret": True, "generate": {"random": True}}}, "random must be a whole number of bytes, 16-64"),
            ({"A:B": {"secret": True, "generate": {"random": "32"}}}, "random must be a whole number of bytes, 16-64"),
            ({"A:B": {"secret": True, "generate": {"random": 32.0}}}, "random must be a whole number of bytes, 16-64"),
            ({"A:B": {"secret": True, "generate": {"random": 32, "encoding": "base64"}}}, "encoding must be base64url or hex"),
            ({"A:B": {"secret": True, "generate": {"random": 32, "encoding": None}}}, "encoding must be base64url or hex"),
            ({"A:B": {"secret": True, "generate": {"command": []}}}, "command must list the program and its arguments"),
            ({"A:B": {"secret": True, "generate": {"command": "keygen"}}}, "command must list the program and its arguments"),
            ({"A:B": {"secret": True, "generate": {"command": [""]}}}, "each a non-empty string"),
            ({"A:B": {"secret": True, "generate": {"command": ["keygen", 1]}}}, "each a non-empty string"),
            ({"A:B": {"secret": True, "generate": {"command": [None]}}}, "each a non-empty string"),
            ({"A:B": {"secret": True, "generate": {"command": ["x"], "timeout": "9s"}}}, "timeout must be 10s-10m"),
            ({"A:B": {"secret": True, "generate": {"command": ["x"], "timeout": "11m"}}}, "timeout must be 10s-10m"),
            ({"A:B": {"secret": True, "generate": {"command": ["x"], "timeout": "601s"}}}, "timeout must be 10s-10m"),
            ({"A:B": {"secret": True, "generate": {"command": ["x"], "timeout": "1h"}}}, "timeout must be 10s-10m"),
            ({"A:B": {"secret": True, "generate": {"command": ["x"], "timeout": "0s"}}}, "timeout must look like"),
            ({"A:B": {"secret": True, "generate": {"command": ["x"], "timeout": "2 m"}}}, "timeout must look like"),
            ({"A:B": {"secret": True, "generate": {"command": ["x"], "timeout": "120"}}}, "timeout must look like"),
            ({"A:B": {"secret": True, "generate": {"command": ["x"], "timeout": 120}}}, "timeout must look like"),
        )
        for keys, message in cases:
            with self.subTest(keys=keys):
                declared = copy.deepcopy(DESCRIPTOR)
                declared["services"]["api"]["settings"]["keys"] = keys
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(declared)

    def test_from_keys_stand_alone_and_never_join_the_required_list(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["api"]["settings"]["keys"] = {
            "Auth:ClientSecret": {"secret": True, "required": True, "generate": {"random": 32}},
            "Auth:Issuer": {"from": "Sites:app:Url"},
            "Auth:Landing": {"from": "Sites:landing:Url"},
            "Auth:Again": {"from": "Auth:ClientSecret"},
            "Plain:Optional": {"default": "x"},
        }
        declared["services"]["edge"]["settings"] = {"keys": {"Desk:Secret": {"from": "Auth:ClientSecret"},
                                                             "Desk:Plain": {"from": "Plain:Optional"}}}
        descriptor = release.validate(declared)
        api, edge = descriptor["services"]["api"]["settings"], descriptor["services"]["edge"]["settings"]
        # A linked key joins no required list, whatever its source does.
        self.assertEqual(["Database:Connection", "Auth:ClientSecret"], api["required"])
        self.assertEqual([], edge["required"])
        self.assertEqual({"from": "Sites:app:Url"}, api["keys"]["Auth:Issuer"])
        self.assertEqual({"from": "Sites:landing:Url"}, api["keys"]["Auth:Landing"])
        self.assertEqual({"from": "Auth:ClientSecret"}, edge["keys"]["Desk:Secret"])
        self.assertEqual({"from": "Plain:Optional"}, edge["keys"]["Desk:Plain"])

    def test_from_names_a_top_level_site_in_descriptor_2(self):
        declared = copy.deepcopy(DESCRIPTOR_2)
        declared["services"]["worker"]["settings"] = {"keys": {"Auth:Issuer": {"from": "Sites:app:Url"},
                                                               "Auth:Admin": {"from": "Sites:admin:Url"}}}
        keys = release.validate(declared)["services"]["worker"]["settings"]["keys"]
        self.assertEqual({"Auth:Issuer": {"from": "Sites:app:Url"}, "Auth:Admin": {"from": "Sites:admin:Url"}}, keys)
        declared["services"]["worker"]["settings"]["keys"]["Auth:Ghost"] = {"from": "Sites:web:Url"}
        with self.assertRaisesRegex(release.Invalid, "worker: setting Auth:Ghost: from Sites:web:Url names no key"):
            release.validate(declared)

    def test_from_rule_breaks_name_the_rule(self):
        cases = []
        # A linked key stands alone: any other field, even a default one, is refused.
        for field in ({"type": "text"}, {"required": False}, {"secret": True}, {"default": "x"}, {"values": ["a"]},
                      {"format": "yyyy"}, {"required": True, "secret": True}):
            cases.append(({"A:B": {"from": "A:C", **field}, "A:C": {}}, None, "a linked key stands alone"))
        for source in (5, None, "", ["A:C"], True, "A C", "A::C", "A:C ", "A:C\n"):
            cases.append(({"A:B": {"from": source}, "A:C": {}}, None, "from must name a configuration path"))
        cases += [
            # An unknown source: declared nowhere, only in settings.required, or a site/derived key that is not there.
            ({"A:B": {"from": "Nope:Key"}}, None, "A:B: from Nope:Key names no key a service declares"),
            ({"A:B": {"from": "Database:Connection"}}, None, "names no key a service declares under settings.keys"),
            ({"A:B": {"from": "Sites:ghost:Url"}}, None, "names no key a service declares"),
            ({"A:B": {"from": "Sites:app:Host"}}, None, "names no key a service declares"),
            ({"A:B": {"from": "sites:app:Url"}}, None, "names no key a service declares"),
            ({"A:B": {"from": "Sites:App:Url"}}, None, "names no key a service declares"),
            # One hop: a source that is itself linked, itself included, or in another service.
            ({"A:B": {"from": "A:C"}, "A:C": {"from": "A:D"}, "A:D": {}}, None, "A:B: from A:C is itself linked"),
            ({"A:B": {"from": "A:B"}}, None, "A:B: from A:B is itself linked"),
            ({"A:B": {"from": "A:C"}, "A:C": {"from": "A:B"}}, None, "is itself linked"),
            ({"A:C": {"from": "A:D"}, "A:D": {}}, {"E:K": {"from": "A:C"}}, "edge: setting E:K: from A:C is itself linked"),
            ({"A:C": {"from": "Sites:app:Url"}}, {"E:K": {"from": "A:C"}}, "edge: setting E:K: from A:C is itself linked"),
        ]
        for keys, edge_keys, message in cases:
            with self.subTest(keys=keys, edge=edge_keys):
                declared = copy.deepcopy(DESCRIPTOR)
                declared["services"]["api"]["settings"]["keys"] = keys
                if edge_keys:
                    declared["services"]["edge"]["settings"] = {"keys": edge_keys}
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(declared)

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed for this interpreter")
    def test_the_documented_generated_and_linked_keys_parse(self):
        text = """
descriptor: 2
product: pilot
sites:
  identity: { primary: true }
services:
  identity:
    image: ghcr.io/wow-two-sdk-beta/wow-two-sdk-beta.identity/identity:0.3.6
    health: /health
    sites:
      identity: { paths: [/] }
    settings:
      keys:
        Identity:Server:Keys:Signing:0:
          secret: true
          required: true
          generate: { command: [dotnet, Identity.Api.dll, keys, generate, --role, signing] }
        Identity:Server:Clients:0:ClientSecret:
          secret: true
          required: true
          generate: { random: 32 }
        Identity:Server:Issuer:
          from: Sites:identity:Url
  desk:
    image: ghcr.io/wow-two-platform/desk/desk:1.0.0
    health: /health
    settings:
      keys:
        Auth:ClientSecret:
          from: Identity:Server:Clients:0:ClientSecret
"""
        services = release.validate(release.parse(text))["services"]
        identity = services["identity"]["settings"]
        self.assertEqual(["Identity:Server:Keys:Signing:0", "Identity:Server:Clients:0:ClientSecret"], identity["required"])
        self.assertEqual({"command": ["dotnet", "Identity.Api.dll", "keys", "generate", "--role", "signing"],
                          "timeoutSeconds": 120}, identity["keys"]["Identity:Server:Keys:Signing:0"]["generate"])
        self.assertEqual({"from": "Sites:identity:Url"}, identity["keys"]["Identity:Server:Issuer"])
        self.assertEqual({"from": "Identity:Server:Clients:0:ClientSecret"},
                         services["desk"]["settings"]["keys"]["Auth:ClientSecret"])

    def test_cron_takes_five_or_six_fields_names_steps_and_macros(self):
        for value in ("0 3 * * *", "*/15 9-17 * * MON-FRI", "0 0 1 JAN,JUL *", "0 30 2 L * ?", "0 0 * * 5#2", "@daily"):
            self.assertTrue(release.is_cron(value), value)
        for value in ("", "* * * *", "60 * * * *", "* 24 * * *", "0 0 0 * *", "5-1 * * * *", "*/0 * * * *", "@often",
                      " 0 3 * * *"):
            self.assertFalse(release.is_cron(value), value)

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed for this interpreter")
    def test_a_yaml_descriptor_parses(self):
        text = "descriptor: 1\nproduct: pilot\nservices:\n  api:\n    build: {dockerfile: deployment/Dockerfile}\n" \
               "    paths: [codebase/**]\n    health: /health\n"
        self.assertEqual(["api"], list(release.validate(release.parse(text))["services"]))

    def test_an_identity_is_checked_and_left_out_of_the_validated_descriptor(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared.update(name="Pilot", description="Flies the sample product.", homepage="https://pilot.example/start")
        declared["services"]["api"].update(name="API", description="Answers the app.")
        declared["services"]["edge"]["name"] = "Site"
        self.assertEqual(release.validate(copy.deepcopy(DESCRIPTOR)), release.validate(declared))
        self.assertEqual({"name": "Pilot", "description": "Flies the sample product.",
                          "homepage": "https://pilot.example/start"}, release.identity_of(declared))
        self.assertIsNone(release.identity_of(copy.deepcopy(DESCRIPTOR)))
        del declared["homepage"]
        self.assertEqual({"name": "Pilot", "description": "Flies the sample product."}, release.identity_of(declared))

    def test_identity_rule_breaks_name_the_rule(self):
        cases = (
            (lambda d: d.update(name="Pilot"), "needs both name and description"),
            (lambda d: d.update(homepage="https://pilot.example"), "needs both name and description"),
            (lambda d: d.update(name="n" * 81, description="x"), "name must be one line of 1-80"),
            (lambda d: d.update(name="", description="x"), "name must be one line"),
            (lambda d: d.update(name=42, description="x"), "name must be one line"),
            (lambda d: d.update(name="Pilot", description=" padded"), "description must be one line"),
            (lambda d: d.update(name="Pilot", description="two\nlines"), "description must be one line"),
            (lambda d: d.update(name="Pilot", description="d" * 201), "description must be one line of 1-200"),
            (lambda d: d.update(name="Pilot", description="x", homepage="javascript:alert(1)"), "homepage must be"),
            (lambda d: d.update(name="Pilot", description="x", homepage="https://user:pw@pilot.example"), "homepage must be"),
            (lambda d: d.update(name="Pilot", description="x", homepage="https://" + "h" * 300), "homepage must be"),
            (lambda d: d["services"]["api"].update(name=""), "api: name must be one line"),
            (lambda d: d["services"]["api"].update(description="d" * 201), "api: description must be one line of 1-200"),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                descriptor = copy.deepcopy(DESCRIPTOR)
                mutate(descriptor)
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(descriptor)

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed for this interpreter")
    def test_a_yaml_identity_parses(self):
        text = "descriptor: 1\nproduct: pilot\nname: Pilot\ndescription: Flies the sample product.\n" \
               "homepage: https://pilot.example\nservices:\n  api:\n    name: API\n" \
               "    build: {dockerfile: deployment/Dockerfile}\n    paths: [codebase/**]\n    health: /health\n"
        parsed = release.parse(text)
        release.validate(parsed)
        self.assertEqual({"name": "Pilot", "description": "Flies the sample product.", "homepage": "https://pilot.example"},
                         release.identity_of(parsed))


DESCRIPTOR_2 = {
    "descriptor": 2, "product": "pilot",
    "sites": {"app": None, "admin": {"exposure": "private", "environments": ["dev"]}},
    "services": {
        "api": {"build": {"dockerfile": "deployment/backend.Dockerfile"},
                "paths": ["codebase/pilot.backend-services/**"], "health": "/health",
                "sites": {"app": {"paths": ["/api", "/hubs"]}, "admin": {"paths": ["/api"], "port": 8081}}},
        "edge": {"build": {"dockerfile": "deployment/edge.Dockerfile"},
                 "paths": ["codebase/pilot.frontend-services/**"], "port": 80, "health": "/healthz",
                 "sites": {"app": None, "admin": {}}},
        "worker": {"build": {"dockerfile": "deployment/backend.Dockerfile"},
                   "paths": ["codebase/pilot.worker/**"], "health": "/health"},
    },
}


class SitesTests(unittest.TestCase):
    """Descriptor 2: the product declares its sites; each service lists the paths it serves per site."""

    def test_sites_and_paths_take_their_defaults(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR_2))
        self.assertEqual(2, descriptor["descriptor"])
        self.assertEqual({"app": {"exposure": "public", "primary": True, "environments": ["dev", "prod"]},
                          "admin": {"exposure": "private", "primary": False, "environments": ["dev"]}},
                         descriptor["sites"])
        services = descriptor["services"]
        self.assertEqual({"app": {"port": 8080, "paths": ["/api", "/hubs"]}, "admin": {"port": 8081, "paths": ["/api"]}},
                         services["api"]["sites"])
        self.assertEqual({"app": {"port": 80, "paths": ["/"]}, "admin": {"port": 80, "paths": ["/"]}},
                         services["edge"]["sites"])
        self.assertEqual({}, services["worker"]["sites"])

    def test_one_site_may_be_marked_primary_and_environments_keep_their_order(self):
        declared = copy.deepcopy(DESCRIPTOR_2)
        declared["sites"] = {"app": {"environments": ["prod", "dev"]}, "admin": {"primary": True}}
        sites = release.validate(declared)["sites"]
        self.assertEqual((False, True), (sites["app"]["primary"], sites["admin"]["primary"]))
        self.assertEqual(["dev", "prod"], sites["app"]["environments"])

    def test_rule_breaks_name_the_rule(self):
        api = lambda d: d["services"]["api"]
        cases = (
            # Exactly one service serves `/` per site: api claims it first, so edge's `/` is the second claim.
            (lambda d: api(d)["sites"]["app"]["paths"].append("/"),
             r"edge: another service \(api\) already serves site app at /$"),
            (lambda d: d["services"]["edge"]["sites"].update(app={"paths": ["/", "/api"]}),
             r"edge: another service \(api\) already serves site app at /api$"),
            (lambda d: api(d)["sites"]["app"].update(paths=["/api", "/api"]), "site app lists a path twice"),
            (lambda d: api(d)["sites"].update(web={}), "site web is not declared under the top-level sites"),
            (lambda d: d["sites"].update(web={}), "No service serves site web"),
            (lambda d: api(d)["sites"]["app"].update(path="/api"), "lists paths, not path, in descriptor 2"),
            (lambda d: api(d)["sites"]["app"].update(exposure="private"), "exposure belongs to the top-level site"),
            (lambda d: api(d)["sites"]["app"].update(host="app.example.com"), "site app takes paths and port"),
            (lambda d: api(d)["sites"]["app"].update(paths=[]), "paths must list at least one path"),
            (lambda d: api(d)["sites"]["app"].update(paths="/api"), "paths must list at least one path"),
            (lambda d: api(d)["sites"]["app"].update(port=0), "site port must be 1-65535"),
            (lambda d: d["sites"]["admin"].update(exposure="world"), "site admin: exposure must be public or private"),
            (lambda d: d["sites"]["admin"].update(environments=["test"]), "environments must list dev, prod or both"),
            (lambda d: d["sites"]["admin"].update(environments=[]), "environments must list dev, prod or both"),
            (lambda d: d["sites"]["admin"].update(host="admin.example.com"), "takes exposure, primary and environments"),
            (lambda d: d["sites"]["admin"].update(primary="yes"), "primary must be true or false"),
            (lambda d: d["sites"].update(app={"primary": True}, admin={"primary": True}),
             "Only one site may be primary: app, admin"),
            (lambda d: d.update(sites=["app"]), "sites must map site names"),
            (lambda d: d["sites"].update(Admin={}), "Site names must be lowercase slugs"),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                descriptor = copy.deepcopy(DESCRIPTOR_2)
                mutate(descriptor)
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(descriptor)

    def test_a_bad_path_is_refused(self):
        for path in ("api", "/api/", "/a//b", "/../x", "/a/./b", "/a b", "/api?x=1", "", "/" + "a" * 200, 7):
            with self.subTest(path=path):
                descriptor = copy.deepcopy(DESCRIPTOR_2)
                descriptor["services"]["api"]["sites"]["app"]["paths"] = [path]
                with self.assertRaisesRegex(release.Invalid, "must look like / or /api"):
                    release.validate(descriptor)
        for path in ("/", "/api", "/api/v2", "/.well-known", "/a~b_c.d-e"):
            with self.subTest(path=path):
                descriptor = copy.deepcopy(DESCRIPTOR_2)
                descriptor["services"]["api"]["sites"]["app"]["paths"] = [path]
                descriptor["services"]["edge"]["sites"]["app"] = {"paths": ["/web"]} if path == "/" else None
                self.assertEqual([path], release.validate(descriptor)["services"]["api"]["sites"]["app"]["paths"])

    def test_a_site_without_a_root_path_warns(self):
        self.assertEqual([], release.site_warnings(release.validate(copy.deepcopy(DESCRIPTOR_2))))
        declared = copy.deepcopy(DESCRIPTOR_2)
        declared["services"]["edge"]["sites"]["admin"] = {"paths": ["/ops"]}
        descriptor = release.validate(declared)
        self.assertEqual(["site admin has no / path: its host answers 404 outside /api, /ops"],
                         release.site_warnings(descriptor))
        with contextlib.redirect_stderr(io.StringIO()) as printed:
            release.warned(descriptor)
        self.assertEqual("::warning title=deploy.yml::site admin has no / path: its host answers 404 outside /api, /ops\n",
                         printed.getvalue())
        self.assertEqual([], release.site_warnings(release.validate(copy.deepcopy(DESCRIPTOR))))

    def test_a_service_without_sites_needs_no_top_level_sites(self):
        declared = copy.deepcopy(DESCRIPTOR_2)
        del declared["sites"]
        for service in declared["services"].values():
            service.pop("sites", None)
        descriptor = release.validate(declared)
        self.assertEqual({}, descriptor["sites"])

    def test_a_bundle_carries_the_sites_and_each_services_paths(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR_2))
        planned = {"release": "v1.0.0", "kind": "release", "sourceCommit": "c" * 40,
                   "services": {name: {"version": "1.0.0", "changedIn": "v1.0.0"} for name in descriptor["services"]}}
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
        compose_bytes, manifest = release.render(descriptor, planned, images)
        self.assertEqual(2, manifest["schemaVersion"])
        self.assertEqual(descriptor["sites"], manifest["sites"])
        self.assertEqual({"api": {"app": {"port": 8080, "paths": ["/api", "/hubs"]},
                                  "admin": {"port": 8081, "paths": ["/api"]}},
                          "edge": {"app": {"port": 80, "paths": ["/"]}, "admin": {"port": 80, "paths": ["/"]}}},
                         manifest["routes"])
        self.assertEqual(["sites", "routes", "needs"], list(manifest)[list(manifest).index("sites"):][:3])
        # Sites never reach Compose: the ingress routes them, so no service publishes a port.
        self.assertNotIn("ports", json.dumps(json.loads(compose_bytes)))

    def test_a_descriptor_1_bundle_keeps_schema_1(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        planned = {"release": "v1.0.0", "kind": "release", "sourceCommit": "c" * 40,
                   "services": {name: {"version": "1.0.0", "changedIn": "v1.0.0"} for name in descriptor["services"]}}
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
        _, manifest = release.render(descriptor, planned, images)
        self.assertEqual(1, manifest["schemaVersion"])
        self.assertNotIn("routes", manifest)
        self.assertEqual({"api": {"app": {"port": 8080, "path": "/api", "exposure": "public"}},
                          "edge": {"app": {"port": 80, "path": "/", "exposure": "public"},
                                   "landing": {"port": 80, "path": "/", "exposure": "public"}}}, manifest["sites"])

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed for this interpreter")
    def test_the_documented_example_parses(self):
        text = """
descriptor: 2
product: ocharo-studio
sites:
  studio: { primary: true }
services:
  studio:
    build: { dockerfile: deployment/Dockerfile }
    paths: [codebase/**]
    health: /health
    sites:
      studio: { paths: [/] }
"""
        descriptor = release.validate(release.parse(text))
        self.assertEqual({"studio": {"exposure": "public", "primary": True, "environments": ["dev", "prod"]}},
                         descriptor["sites"])
        self.assertEqual({"studio": {"port": 8080, "paths": ["/"]}}, descriptor["services"]["studio"]["sites"])


MIGRATE = [{"service": "api", "command": ["dotnet", "Pilot.Api.dll", "migrate"]},
           {"service": "edge", "command": ["/app/edge", "migrate"], "timeout": "90s"}]


def fixed_bundle(descriptor, images=None):
    """release.json and compose.json bytes for a fixed main release of every service, as the workflow writes them."""
    planned = {"release": "v1.0.0", "kind": "release", "sourceCommit": "c" * 40,
               "services": {name: {"version": "1.0.0", "changedIn": "v1.0.0"} for name in descriptor["services"]}}
    images = images or {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
    compose_bytes, manifest = release.render(descriptor, planned, images, "main",
                                             identity={"version": "1.0.0", "channel": "release"})
    return (json.dumps(manifest, indent=2) + "\n").encode(), compose_bytes


class MigrateTests(unittest.TestCase):
    """The migrate step's items and the untrusted extensions: checked, and carried only when declared."""

    def declared(self, **keys):
        descriptor = copy.deepcopy(DESCRIPTOR)
        descriptor.update(copy.deepcopy(keys))
        return descriptor

    def test_a_valid_list_builds_in_the_descriptors_order_with_its_timeouts(self):
        descriptor = release.validate(self.declared(migrate=MIGRATE, extensions=["postgres_fdw", "pg-cron"]))
        expected = [{"service": "api", "command": ["dotnet", "Pilot.Api.dll", "migrate"], "timeoutSeconds": 600},
                    {"service": "edge", "command": ["/app/edge", "migrate"], "timeoutSeconds": 90}]
        self.assertEqual(expected, descriptor["migrate"])
        self.assertEqual(["postgres_fdw", "pg-cron"], descriptor["extensions"])
        manifest = json.loads(fixed_bundle(descriptor)[0])
        self.assertEqual(expected, manifest["migrate"])
        self.assertEqual(["postgres_fdw", "pg-cron"], manifest["extensions"])
        for bound in ("10s", "60m", "1h", "3600s"):
            with self.subTest(timeout=bound):
                items = [{"service": "api", "command": ["migrate"], "timeout": bound}]
                self.assertEqual(1, len(release.validate(self.declared(migrate=items))["migrate"]))

    def test_extensions_ride_alone_and_an_empty_list_is_declared(self):
        manifest = json.loads(fixed_bundle(release.validate(self.declared(extensions=[])))[0])
        self.assertEqual([], manifest["extensions"])
        self.assertNotIn("migrate", manifest)

    def test_rule_breaks_name_the_rule(self):
        api = ["dotnet", "Pilot.Api.dll", "migrate"]
        cases = (
            ([{"service": "api", "command": api, "timeout": "5s"}], "migrate api timeout must be 10s-60m"),
            ([{"service": "api", "command": api, "timeout": "61m"}], "migrate api timeout must be 10s-60m"),
            ([{"service": "api", "command": api, "timeout": "2h"}], "migrate api timeout must be 10s-60m"),
            ([{"service": "api", "command": api, "timeout": 600}], "migrate api timeout must look like"),
            ([{"service": "api", "command": api, "timeout": "10 m"}], "migrate api timeout must look like"),
            ([{"service": "worker", "command": api}], r"migrate item 1: service must name a service of this "
                                                      r"descriptor \(worker is not one\)"),
            ([{"service": "postgres", "command": api}], "service must name a service of this descriptor"),
            ([{"command": api}], "migrate item 1: service must name a service"),
            ([{"service": "api", "command": []}], "migrate api: command must list the program"),
            ([{"service": "api"}], "migrate api: command must list the program"),
            ([{"service": "api", "command": "dotnet Pilot.Api.dll migrate"}], "command must list the program"),
            ([{"service": "api", "command": ["dotnet", ""]}], "command must list the program"),
            ([{"service": "api", "command": ["x"] * 33}], "command must list the program"),
            ([{"service": "api", "command": api, "image": "x"}], "migrate item 1 takes service, command and timeout"),
            ([{"service": "api", "command": api}, {"service": "api", "command": api}], "migrate lists api twice"),
            (["api"], "migrate item 1 takes service, command and timeout"),
            ([], "migrate must list at least one item"),
            (None, "migrate must list at least one item"),
            ({"service": "api", "command": api}, "migrate must list at least one item"),
        )
        for items, message in cases:
            with self.subTest(message=message, items=items):
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(self.declared(migrate=items))

    def test_extension_rule_breaks_name_the_rule(self):
        for extensions, message in ((["PostGIS"], "extensions must list PostgreSQL extension names"),
                                    (["1cube"], "extensions must list"), (["x" * 64], "extensions must list"),
                                    (["pg trgm"], "extensions must list"), ([7], "extensions must list"),
                                    (None, "extensions must list"), ("postgis", "extensions must list"),
                                    (["postgis", "postgis"], "extensions lists an extension twice")):
            with self.subTest(extensions=extensions):
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(self.declared(extensions=extensions))

    def test_a_descriptor_without_the_keys_builds_the_v0_11_0_bundle_byte_for_byte(self):
        # The hashes of the bundles v0.11.0's generator wrote for these descriptors and inputs.
        for declared, release_hash, compose_hash in (
                (DESCRIPTOR, "c32b1d6e556fff4752a12a07bac3d1eca7cb4b1585cdc4b1340c42dd49939ee0",
                 "a8e69c8526bbfe204dca10c7ec059508f2c0696148eea358407138be3ce15969"),
                (DESCRIPTOR_2, "f8c6c236e7fc9d5deabcfee01783514deff3a1ec8b477ff6daf2ed49cd2620fa",
                 "a81beeae65b80a2d8565e16fe8e8a04034395edfc62acb663db00948d3332f3f")):
            with self.subTest(descriptor=declared["descriptor"]):
                release_bytes, compose_bytes = fixed_bundle(release.validate(copy.deepcopy(declared)))
                self.assertEqual((release_hash, compose_hash), (hashlib.sha256(release_bytes).hexdigest(),
                                                                hashlib.sha256(compose_bytes).hexdigest()))
                for key in ("migrate", "migrations", "migrationSets", "extensions"):
                    self.assertNotIn('"' + key + '"', release_bytes.decode())

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed for this interpreter")
    def test_the_documented_example_parses(self):
        text = """
descriptor: 2
product: foreverpin
services:
  management:
    build: { dockerfile: deployment/Dockerfile }
    paths: [codebase/**]
    health: /health
migrate:                              # one item per image that hosts a set
  - service: management               # hosts every set this repository owns
    command: [dotnet, ForeverPin.Api.dll, migrate]
    timeout: 10m                      # default 10m; 10s-60m
extensions: [postgres_fdw]            # untrusted extensions only; a set creates trusted ones
"""
        descriptor = release.validate(release.parse(text))
        self.assertEqual([{"service": "management", "command": ["dotnet", "ForeverPin.Api.dll", "migrate"],
                           "timeoutSeconds": 600}], descriptor["migrate"])
        self.assertEqual(["postgres_fdw"], descriptor["extensions"])


SETS = [{"name": "pilot", "schema": "public", "strategy": "bespoke", "level": 3,
         "contracts": [{"label": "003-drop-legacy", "since": "v0.4"}], "dependsOn": [{"set": "identity", "level": 2}],
         "extensions": ["pg_trgm"], "package": "Pilot.Api", "version": "1.0.0"},
        {"name": "outbox", "schema": "outbox", "strategy": "bespoke", "ownerKind": "module", "level": 1}]
RESULT = 'migrate-result {"verb":"sets","exit":0,"sets":[{"name":"pilot","state":"current","levelAfter":3}]}'


def sets_output(sets, result=RESULT, indent=None):
    """What an image prints for `migrate sets --json`: the JSON report, then the migrate-result line last."""
    return json.dumps({"sets": sets}, indent=indent) + "\n" + result + "\n"


class MigrationSetsTests(unittest.TestCase):
    """`release.py sets`: what each migrate item's image hosts, read from its `migrate sets --json` output."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        declared = copy.deepcopy(DESCRIPTOR)
        declared["migrate"] = copy.deepcopy(MIGRATE)
        release_bytes, compose_bytes = fixed_bundle(release.validate(declared))
        self.archive = self.root / "pilot-release.tar.gz"
        release.write_bundle(self.root / "bundle", compose_bytes, json.loads(release_bytes), self.archive)
        self.reports = self.root / "reports"
        self.reports.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def report(self, service, text):
        (self.reports / (service + ".out")).write_text(text)

    def test_migration_sets_list_each_items_sets_verbatim_with_their_levels(self):
        self.report("api", sets_output(SETS))
        self.report("edge", sets_output(SETS[1:], indent=2))
        compose_before = (self.root / "bundle" / "compose.json").read_bytes()
        manifest = release.record_sets(self.root / "bundle", self.reports, self.archive)
        written = json.loads((self.root / "bundle" / "release.json").read_text())
        self.assertEqual(manifest, written)
        self.assertIs(True, written["migrations"])
        self.assertEqual({"api": SETS, "edge": SETS[1:]}, written["migrationSets"])
        self.assertEqual(["api", "edge"], list(written["migrationSets"]))
        self.assertEqual(("pilot", 3), (written["migrationSets"]["api"][0]["name"], written["migrationSets"]["api"][0]["level"]))
        self.assertEqual(["migrate", "migrations", "migrationSets"],
                         [key for key in written if key.startswith("migrat")])
        self.assertEqual(compose_before, (self.root / "bundle" / "compose.json").read_bytes())
        with tarfile.open(self.archive) as package:
            self.assertEqual(["release.json", "compose.json"], package.getnames())
            self.assertEqual((self.root / "bundle" / "release.json").read_bytes(),
                             package.extractfile("release.json").read())

    def test_a_candidate_bundle_without_an_archive_is_rewritten_in_place(self):
        for service in ("api", "edge"):
            self.report(service, sets_output(SETS[:1]))
        self.archive.unlink()
        release.record_sets(self.root / "bundle", self.reports)
        self.assertFalse(self.archive.exists())
        self.assertEqual(SETS[:1], json.loads((self.root / "bundle" / "release.json").read_text())["migrationSets"]["edge"])

    def test_output_that_is_not_a_sets_report_fails_the_release(self):
        cases = (
            (json.dumps({"sets": SETS}) + "\n", "api: migrate sets --json printed no migrate-result line last"),
            ("", "printed no migrate-result line last"),
            (RESULT + "\n", "api: migrate sets --json printed no JSON report before its migrate-result line"),
            ("Unhandled exception\n" + RESULT, "printed no JSON report"),
            ("starting\n" + sets_output(SETS), "printed no JSON report"),
            (sets_output(SETS, 'migrate-result {"verb":"sets","exit":1}'), "api: migrate sets --json reported exit 1"),
            (sets_output([]), "api: migrate sets --json lists no set"),
            ("[]\n" + RESULT, "lists no set"),
            (json.dumps({"report": SETS}) + "\n" + RESULT, "lists no set"),
            (sets_output([{"name": "pilot"}]), "api: every set migrate sets --json lists needs a name and a level"),
            (sets_output([{"name": "", "level": 1}]), "needs a name and a level"),
            (sets_output([{"name": "pilot", "level": "3"}]), "needs a name and a level"),
            (sets_output([{"name": "pilot", "level": -1}]), "needs a name and a level"),
        )
        self.report("edge", sets_output(SETS[1:]))
        for text, message in cases:
            with self.subTest(message=message, text=text[:40]):
                self.report("api", text)
                with self.assertRaisesRegex(release.Invalid, message):
                    release.record_sets(self.root / "bundle", self.reports, self.archive)

    def test_a_malformed_result_line_and_blank_trailing_lines_are_tolerated(self):
        self.assertEqual(SETS, release.sets_report("api", sets_output(SETS, "migrate-result {not json") + "\n\n"))

    def test_a_missing_report_or_a_bundle_without_migrate_items_is_refused(self):
        self.report("api", sets_output(SETS))
        with self.assertRaisesRegex(release.Invalid, "edge: no migrate sets --json output at .*edge.out"):
            release.record_sets(self.root / "bundle", self.reports)
        plain = self.root / "plain"
        release_bytes, compose_bytes = fixed_bundle(release.validate(copy.deepcopy(DESCRIPTOR)))
        release.write_bundle(plain, compose_bytes, json.loads(release_bytes))
        with self.assertRaisesRegex(release.Invalid, "The bundle declares no migrate items"):
            release.record_sets(plain, self.reports)

    def test_the_command_line_records_the_sets_and_reports_a_refusal(self):
        self.report("api", sets_output(SETS))
        script = str(Path(release.__file__).resolve())
        missing = subprocess.run([sys.executable, "-B", script, "sets", "--output", str(self.root / "bundle"),
                                  "--sets", str(self.reports)], capture_output=True, text=True)
        self.assertEqual(1, missing.returncode)
        self.assertEqual("rejected", json.loads(missing.stderr)["status"])
        self.assertIn("edge: no migrate sets --json output", json.loads(missing.stderr)["reason"])
        self.report("edge", sets_output(SETS[1:]))
        done = subprocess.run([sys.executable, "-B", script, "sets", "--output", str(self.root / "bundle"),
                               "--sets", str(self.reports), "--archive", str(self.archive)],
                              capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual({"migrations": True, "sets": {"api": [{"name": "pilot", "level": 3},
                                                               {"name": "outbox", "level": 1}],
                                                       "edge": [{"name": "outbox", "level": 1}]}},
                         json.loads(done.stdout))


IDENTITY = "ghcr.io/wow-two-sdk-beta/wow-two-sdk-beta.identity/identity"
PIN = "sha256:" + "d" * 64


def with_pinned(declared=DESCRIPTOR, tag="0.3.0"):
    """The descriptor plus `identity`, a per-product instance run from another repository's image pinned by tag."""
    descriptor = copy.deepcopy(declared)
    descriptor["services"]["identity"] = {
        "image": IDENTITY + ":" + tag, "health": "/health", "needs": ["postgres"],
        "settings": {"keys": {"DatabaseSettings:ConnectionString": {"required": True}}}}
    return descriptor


class PinnedImageTests(unittest.TestCase):
    """A service that runs another repository's image at a release tag: never built, pinned by digest, versioned by
    its tag."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "engineering/codebase").mkdir(parents=True)
        (self.repo / "engineering/codebase/one.cs").write_text("one")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")
        self.first = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        self.temporary.cleanup()

    def test_a_pinned_image_validates_without_build_or_paths(self):
        identity = release.validate(with_pinned())["services"]["identity"]
        self.assertEqual((IDENTITY, "0.3.0", None, []),
                         (identity["image"], identity["pinned"], identity["build"], identity["paths"]))
        self.assertEqual(["DatabaseSettings:ConnectionString"], identity["settings"]["required"])
        self.assertNotIn("pinned", release.validate(copy.deepcopy(DESCRIPTOR))["services"]["api"])
        for image, tag in (("localhost:5000/identity:0.3.0", "0.3.0"), (IDENTITY + ":v0.3.0-rc.1", "v0.3.0-rc.1"),
                           (IDENTITY + ":Stable_2", "Stable_2")):
            with self.subTest(image=image):
                declared = with_pinned()
                declared["services"]["identity"]["image"] = image
                self.assertEqual(tag, release.validate(declared)["services"]["identity"]["pinned"])

    def test_rule_breaks_name_the_rule(self):
        identity = lambda d: d["services"]["identity"]
        cases = (
            (lambda d: identity(d).update(image=IDENTITY + ":latest"), "identity: a pinned image needs a release tag, never latest"),
            (lambda d: identity(d).update(image=IDENTITY), "identity: a pinned image needs a release tag"),
            (lambda d: identity(d).update(image="localhost:5000/identity"), "a pinned image needs a release tag"),
            (lambda d: identity(d).update(image=IDENTITY + "@" + PIN), "a pinned image needs a release tag"),
            (lambda d: identity(d).update(image=IDENTITY + ":0.3.0@" + PIN), "a pinned image needs a release tag"),
            (lambda d: identity(d).update(image="GHCR.io/o/identity:0.3.0"), "image must be a lowercase repository"),
            (lambda d: identity(d).update(image=IDENTITY + ":0.3+1"), "image must be a lowercase repository"),
            (lambda d: identity(d).update(image=IDENTITY + ":"), "image must be a lowercase repository"),
            (lambda d: identity(d).update(image=7), "a pinned image needs a release tag"),
            (lambda d: identity(d).update(build={"dockerfile": "deployment/identity.Dockerfile"}, paths=["codebase/**"]),
             "identity: with build, image must be a repository without a tag"),
            (lambda d: identity(d).update(paths=["codebase/**"]), "identity: a pinned image never builds, so it takes no paths"),
            (lambda d: identity(d).pop("image"), "identity: a service needs build, or an image pinned by its release tag"),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                descriptor = with_pinned()
                mutate(descriptor)
                with self.assertRaisesRegex(release.Invalid, message):
                    release.validate(descriptor)

    def test_no_path_rebuilds_a_pinned_image(self):
        descriptor = release.validate(with_pinned())
        self.assertEqual([], release.triggers(descriptor, "identity"))
        self.assertEqual(["api", "edge"], release.changed_services(descriptor, [
            "engineering/codebase/pilot.backend-services/Directory.Packages.props", "engineering/deployment/deploy.yml"]))

    def test_the_plan_versions_it_by_its_tag_and_never_builds_it(self):
        descriptor = release.validate(with_pinned())
        planned = release.plan(descriptor, self.repo, self.first, None, "v1.0.0")
        self.assertEqual({"build": False, "version": "0.3.0", "changedIn": "v1.0.0", "pinned": IDENTITY + ":0.3.0"},
                         planned["services"]["identity"])
        self.assertEqual({"api": True, "edge": True},
                         {name: entry["build"] for name, entry in planned["services"].items() if name != "identity"})
        base = {"product": "pilot", "release": "v1.0.0", "sourceCommit": self.first,
                "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST,
                           "identity": IDENTITY + "@" + PIN},
                "versions": {"api": {"version": "1.0.0", "changedIn": "v1.0.0"},
                             "edge": {"version": "1.0.0", "changedIn": "v1.0.0"},
                             "identity": {"version": "0.3.0", "changedIn": "v0.9.0"}}}
        kept = release.plan(descriptor, self.repo, self.first, base, "v1.0.1")
        self.assertEqual(("0.3.0", "v0.9.0", False), tuple(kept["services"]["identity"][key]
                                                           for key in ("version", "changedIn", "build")))
        self.assertTrue(release.nothing_to_publish(kept))
        bumped = release.plan(release.validate(with_pinned(tag="0.4.0")), self.repo, self.first, base, "v1.0.1")
        self.assertEqual(("0.4.0", "v1.0.1"), (bumped["services"]["identity"]["version"],
                                               bumped["services"]["identity"]["changedIn"]))
        lost = release.plan(descriptor, self.repo, self.first, {**base, "sourceCommit": "1" * 40}, "v1.0.1")
        self.assertEqual({"build": False, "version": "0.3.0", "changedIn": "v1.0.1", "pinned": IDENTITY + ":0.3.0"},
                         lost["services"]["identity"])

    def test_a_pinned_image_takes_the_digest_its_tag_resolved_to(self):
        descriptor = release.validate(with_pinned())
        self.assertEqual({"identity": IDENTITY + ":0.3.0"}, release.pins_of(descriptor))
        self.assertEqual({}, release.pins_of(release.validate(copy.deepcopy(DESCRIPTOR))))
        self.assertEqual({"identity": IDENTITY + "@" + PIN}, release.pinned_images(descriptor, {"identity": PIN}))
        for pins, message in (({}, r"identity: pass the digest .*identity:0\.3\.0 resolves to, as --pin identity="),
                              ({"identity": "sha256:abc"}, "--pin takes sha256: and 64 lowercase hex digits"),
                              ({"identity": "D" * 64}, "--pin takes sha256"),
                              ({"identity": PIN, "api": PIN}, "--pin names a service without a pinned image: api")):
            with self.subTest(pins=pins):
                with self.assertRaisesRegex(release.Invalid, message):
                    release.pinned_images(descriptor, pins)
        self.assertEqual({"identity": PIN}, release.pin_arguments(["identity=" + PIN]))
        for values in (["identity"], ["identity=" + PIN, "identity=" + PIN]):
            with self.assertRaisesRegex(release.Invalid, "--pin takes SERVICE=sha256:<digest>, once per service"):
                release.pin_arguments(values)

    def test_the_bundle_pins_the_digest_in_compose_and_versions_the_service_by_its_tag(self):
        descriptor = release.validate(with_pinned())
        planned = release.plan(descriptor, self.repo, self.first, None, "v1.0.0")
        images = release.build(descriptor, {**planned, "services": {
            name: ({**entry, "build": False, "image": "ghcr.io/o/pilot/" + name + DIGEST} if name != "identity" else entry)
            for name, entry in planned["services"].items()}}, self.repo, "ghcr.io/o/pilot", {}, pins={"identity": PIN})
        compose_bytes, manifest = release.render(descriptor, planned, images)
        self.assertEqual(IDENTITY + "@" + PIN, manifest["images"]["identity"])
        self.assertEqual({"version": "0.3.0", "changedIn": "v1.0.0"}, manifest["versions"]["identity"])
        self.assertEqual(IDENTITY + "@" + PIN, json.loads(compose_bytes)["services"]["identity"]["image"])
        self.assertNotIn("pinned", json.dumps(manifest))


@unittest.skipUnless(HAS_YAML, "PyYAML reads the workflow")
class PublishWorkflowTests(unittest.TestCase):
    """The publish workflow's steps around the generator."""

    def setUp(self):
        import yaml
        workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/publish.yml").read_text())
        self.steps = workflow["jobs"]["bundle"]["steps"]

    def step(self, prefix):
        return next(step for step in self.steps if step.get("name", "").startswith(prefix))

    def test_the_build_resolves_each_pinned_tag_to_a_digest_before_the_generator_runs(self):
        run = self.step("Version, build and tag")["run"]
        for part in ("release.py pins --repo . --commit \"$COMMIT\"",
                     "docker buildx imagetools inspect \"$reference\" --format '{{json .Manifest}}'",
                     "jq -re .digest", "arguments+=(--pin \"$service=$digest\")", "::error title=pinned image::"):
            self.assertIn(part, run)
        self.assertLess(run.index("imagetools"), run.index('release.py "${arguments[@]}"'))

    def test_each_migrate_items_image_runs_offline_before_the_bundle_is_published(self):
        record = self.step("Record the sets")
        self.assertEqual("steps.build.outputs.status == 'built'", record["if"])
        self.assertEqual("${{ steps.build.outputs.archive }}", record["env"]["ARCHIVE"])
        run = record["run"]
        for part in ("jq 'has(\"migrate\")' bundle/release.json", "docker run --rm --network none --cap-drop ALL",
                     "--entrypoint \"${command[0]}\" \"$image\" \"${command[@]:1}\" sets --json",
                     "> \"$reports/$service.out\"", "::error title=migrate sets::", "timeout 600 docker run",
                     "arguments=(sets --output bundle --sets \"$reports\")", "arguments+=(--archive \"$ARCHIVE\")"):
            self.assertIn(part, run)
        self.assertLess(run.index("exit 0"), run.index("docker run"))
        names = [step.get("name", "") for step in self.steps]
        record_at = names.index(record["name"])
        self.assertEqual(names.index(self.step("Version, build and tag")["name"]) + 1, record_at)
        self.assertLess(record_at, names.index(self.step("Publish the release")["name"]))
        self.assertLess(record_at, names.index(self.step("Upload the build's bundle")["name"]))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "one")
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "one")
        self.first = self.commit("first")
        self.descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        self.base = {"product": "pilot", "release": "v1.0.0", "sourceCommit": self.first,
                     "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST},
                     "versions": {"api": {"version": "1.0.0", "changedIn": "v1.0.0"},
                                  "edge": {"version": "0.9.0", "changedIn": "v0.9.0"}}}

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def test_a_candidate_rebuilds_changed_services_and_keeps_the_rest(self):
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "two")
        second = self.commit("api change")
        planned = release.plan(self.descriptor, self.repo, second, self.base)
        short = second[:7]
        self.assertEqual(("sha-" + short, "candidate", "v1.0.0"), (planned["release"], planned["kind"], planned["base"]))
        self.assertEqual({"build": True, "version": "1.0.0+" + short, "changedIn": "sha-" + short},
                         planned["services"]["api"])
        self.assertEqual({"build": False, "version": "0.9.0", "changedIn": "v0.9.0",
                          "image": "ghcr.io/o/pilot/edge" + DIGEST}, planned["services"]["edge"])

    def test_a_release_versions_changed_services_with_its_tag(self):
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        second = self.commit("edge change")
        planned = release.plan(self.descriptor, self.repo, second, self.base, "v1.1.0")
        self.assertEqual(("v1.1.0", "release"), (planned["release"], planned["kind"]))
        self.assertEqual(("1.1.0", "v1.1.0"), (planned["services"]["edge"]["version"], planned["services"]["edge"]["changedIn"]))
        self.assertEqual(("1.0.0", False), (planned["services"]["api"]["version"], planned["services"]["api"]["build"]))

    def test_without_a_base_every_service_builds(self):
        planned = release.plan(self.descriptor, self.repo, self.first)
        self.assertTrue(all(entry["build"] for entry in planned["services"].values()))
        self.assertEqual("0.0.0+" + self.first[:7], planned["services"]["api"]["version"])

    def test_a_service_the_base_never_shipped_builds(self):
        base = copy.deepcopy(self.base)
        del base["images"]["edge"], base["versions"]["edge"]
        planned = release.plan(self.descriptor, self.repo, self.first, base)
        self.assertEqual({"edge"}, {name for name, entry in planned["services"].items() if entry["build"]})

    def test_a_base_of_another_product_is_refused(self):
        with self.assertRaisesRegex(release.Invalid, "another product"):
            release.plan(self.descriptor, self.repo, self.first, {**self.base, "product": "other"})

    def assert_rebuilt_without_the_base(self, planned, label, version, release_name="v1.0.0"):
        self.assertEqual({"api", "edge"}, set(planned["services"]))
        for name, entry in planned["services"].items():
            with self.subTest(service=name):
                self.assertEqual({"build": True, "version": version, "changedIn": label}, entry)
        self.assertIsNone(planned["base"])
        self.assertFalse(planned["descriptorChanged"])
        self.assertEqual("Base " + release_name + "'s source commit (" + self.lost[:7]
                         + ") is not in this history; every service was rebuilt.", planned["warning"])
        self.assertFalse(release.nothing_to_publish(planned))

    lost = "1bcb4722ec27bbe4dff2f955f2ea50572f0af591"

    def test_a_base_commit_missing_from_the_history_rebuilds_every_service_with_a_warning(self):
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        second = self.commit("edge change")
        lost = {**self.base, "sourceCommit": self.lost}
        released = release.plan(self.descriptor, self.repo, second, lost, "v1.1.0")
        self.assert_rebuilt_without_the_base(released, "v1.1.0", "1.1.0")
        self.assertEqual("release", released["kind"])
        candidate = release.plan(self.descriptor, self.repo, second, lost)
        self.assert_rebuilt_without_the_base(candidate, "sha-" + second[:7], "1.0.0+" + second[:7])
        built = release.plan(self.descriptor, self.repo, second, lost, "v1.1.0", "1.1.0")
        self.assertEqual(released, built)

    def test_a_base_without_a_usable_source_commit_counts_as_lost(self):
        for source in (None, "", "main", "--output=x", "1bcb472"):
            with self.subTest(source=source):
                base = {key: value for key, value in self.base.items() if key != "sourceCommit"}
                if source is not None:
                    base["sourceCommit"] = source
                planned = release.plan(self.descriptor, self.repo, self.first, base, "v1.1.0")
                self.assertTrue(all(entry["build"] for entry in planned["services"].values()))
                self.assertIn("every service was rebuilt", planned["warning"])

    def test_a_base_commit_that_shares_no_history_counts_as_lost(self):
        tree = git(self.repo, "rev-parse", self.first + "^{tree}")
        twin = git(self.repo, "commit-tree", tree, "-m", "the same tree under another history")
        self.lost = twin
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        second = self.commit("edge change")
        planned = release.plan(self.descriptor, self.repo, second, {**self.base, "sourceCommit": twin}, "v1.1.0")
        self.assert_rebuilt_without_the_base(planned, "v1.1.0", "1.1.0")

    def test_a_failing_diff_counts_as_lost(self):
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        second = self.commit("edge change")
        with unittest.mock.patch.object(release, "base_commit_known", return_value=True):
            planned = release.plan(self.descriptor, self.repo, second, {**self.base, "sourceCommit": self.lost}, "v1.1.0")
        self.assert_rebuilt_without_the_base(planned, "v1.1.0", "1.1.0")

    def test_a_base_in_the_history_plans_as_before_without_a_warning(self):
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        second = self.commit("edge change")
        planned = release.plan(self.descriptor, self.repo, second, self.base, "v1.1.0")
        self.assertNotIn("warning", planned)
        self.assertEqual({"product": "pilot", "release": "v1.1.0", "kind": "release", "sourceCommit": second,
                          "base": "v1.0.0", "descriptorChanged": False,
                          "services": {"api": {"build": False, "version": "1.0.0", "changedIn": "v1.0.0",
                                               "image": "ghcr.io/o/pilot/api" + DIGEST},
                                       "edge": {"build": True, "version": "1.1.0", "changedIn": "v1.1.0"}}}, planned)

    def test_a_base_on_another_branch_still_diffs_when_it_shares_history(self):
        # A candidate on a branch behind main: the newest release is not its ancestor, but the trees still compare.
        git(self.repo, "checkout", "-q", "-b", "side")
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "side")
        side = self.commit("side change")
        git(self.repo, "checkout", "-q", "main")
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "main")
        head = self.commit("api change")
        planned = release.plan(self.descriptor, self.repo, head, {**self.base, "sourceCommit": side})
        self.assertNotIn("warning", planned)
        self.assertEqual({"api": True, "edge": True}, {name: entry["build"] for name, entry in planned["services"].items()})
        unchanged = release.plan(self.descriptor, self.repo, self.first, {**self.base, "sourceCommit": self.first})
        self.assertEqual({"api": False, "edge": False}, {name: entry["build"] for name, entry in unchanged["services"].items()})

    def test_a_shallow_clone_still_finds_a_reachable_base_commit(self):
        git(self.repo, "config", "uploadpack.allowAnySHA1InWant", "true")
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        self.commit("edge change")
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "three")
        head = self.commit("edge change again")
        clone = Path(self.temporary.name + "-shallow")
        self.addCleanup(shutil.rmtree, clone, True)
        git(self.repo, "clone", "-q", "--depth", "1", self.repo.as_uri(), str(clone))
        self.assertEqual("true", git(clone, "rev-parse", "--is-shallow-repository"))
        self.assertNotEqual(0, subprocess.run(["git", "-C", str(clone), "cat-file", "-e", self.first + "^{commit}"],
                                              capture_output=True).returncode)
        planned = release.plan(self.descriptor, clone, head, self.base, "v1.1.0")
        self.assertNotIn("warning", planned)
        self.assertEqual({"api": False, "edge": True}, {name: entry["build"] for name, entry in planned["services"].items()})
        self.assertEqual("v1.0.0", planned["base"])
        self.assertEqual(self.first, git(clone, "rev-parse", self.first))

    def test_a_shallow_clone_without_the_base_commit_anywhere_counts_as_lost(self):
        self.write("engineering/codebase/pilot.frontend-services/app.ts", "two")
        head = self.commit("edge change")
        clone = Path(self.temporary.name + "-shallow")
        self.addCleanup(shutil.rmtree, clone, True)
        git(self.repo, "clone", "-q", "--depth", "1", self.repo.as_uri(), str(clone))
        planned = release.plan(self.descriptor, clone, head, {**self.base, "sourceCommit": self.lost}, "v1.1.0")
        self.assert_rebuilt_without_the_base(planned, "v1.1.0", "1.1.0")

    def test_the_rendered_bundle_pins_every_image_and_its_compose_hash(self):
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "two")
        second = self.commit("api change")
        planned = release.plan(self.descriptor, self.repo, second, self.base)
        images = {"api": "ghcr.io/o/pilot/api@sha256:" + "b" * 64, "edge": planned["services"]["edge"]["image"]}
        compose_bytes, manifest = release.render(self.descriptor, planned, images, branch="main")
        output = self.repo / "bundle"
        release.write_bundle(output, compose_bytes, manifest, self.repo / "bundle.tar.gz")
        written = json.loads((output / "release.json").read_text())
        self.assertEqual(hashlib.sha256((output / "compose.json").read_bytes()).hexdigest(), written["composeSha256"])
        self.assertEqual(images, written["images"])
        self.assertEqual({"api": ["Database:Connection"]}, written["requiredConfiguration"])
        self.assertEqual({"api": {"version": "1.0.0+" + second[:7], "changedIn": "sha-" + second[:7]},
                          "edge": {"version": "0.9.0", "changedIn": "v0.9.0"}}, written["versions"])
        self.assertEqual({"api": 8080, "edge": 80}, written["ports"])
        compose = json.loads(compose_bytes)
        api, edge = compose["services"]["api"], compose["services"]["edge"]
        self.assertEqual(["pilot-${DEPLOY_ENVIRONMENT}-api"], api["networks"]["platform"]["aliases"])
        self.assertEqual({"type": "bind", "source": "${API_SETTINGS:?Set API_SETTINGS}",
                          "target": "/app/appsettings.Target.json", "read_only": True}, api["volumes"][0])
        self.assertEqual({"type": "volume", "source": "keys", "target": "/data/keys"}, api["volumes"][1])
        self.assertEqual((["NET_BIND_SERVICE"], ["ALL"]), (edge["cap_add"], edge["cap_drop"]))
        self.assertEqual({"keys": {}}, compose["volumes"])
        self.assertEqual("main", manifest["branch"])
        # Neither opt-in is declared, so the bundle carries neither key, and no service depends on another.
        self.assertNotIn("dotnetEnvironment", written)
        self.assertNotIn("verify", written)
        self.assertEqual(set(), {name for name, service in compose["services"].items() if "depends_on" in service})

    def test_a_declared_identity_leaves_the_bundle_byte_for_byte_the_same(self):
        plain = release.validate(copy.deepcopy(DESCRIPTOR))
        declared = copy.deepcopy(DESCRIPTOR)
        declared.update(name="Pilot", description="Flies the sample product.", homepage="https://pilot.example")
        declared["services"]["api"].update(name="API", description="Answers the app.")
        identified = release.validate(declared)
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in plain["services"]}
        self.assertEqual(release.render(plain, release.plan(plain, self.repo, self.first, None), images),
                         release.render(identified, release.plan(identified, self.repo, self.first, None), images))

    def test_declared_dependencies_render_as_health_gated_compose_dependencies(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["edge"]["dependsOn"] = ["api"]
        descriptor = release.validate(declared)
        planned = release.plan(descriptor, self.repo, self.first, None)
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
        compose_bytes, manifest = release.render(descriptor, planned, images)
        compose = json.loads(compose_bytes)
        # Health-gated, and never `restart: true`: a dependency's update does not restart its dependents.
        self.assertEqual({"api": {"condition": "service_healthy"}}, compose["services"]["edge"]["depends_on"])
        self.assertNotIn("depends_on", compose["services"]["api"])
        # Compose carries the order; the manifest needs no second copy of it.
        self.assertNotIn("dependsOn", json.dumps(manifest))

    def test_a_bundle_carries_typed_keys_for_the_runner_and_untyped_bundles_stay_the_same(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["api"]["settings"]["keys"] = {
            "Billing:Mode": {"type": "choice", "values": ["test", "live"], "default": "test"},
            "Stripe:ApiKey": {"secret": True, "required": True},
            "Promo:EndsOn": {"type": "date"},
        }
        descriptor = release.validate(declared)
        planned = release.plan(descriptor, self.repo, self.first, None)
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
        _, manifest = release.render(descriptor, planned, images)
        self.assertEqual({"api": ["Database:Connection", "Stripe:ApiKey"]}, manifest["requiredConfiguration"])
        self.assertEqual({"api": {"optional": {"Billing:Mode": "test"}, "secrets": ["Stripe:ApiKey"],
                                  "allowed": {"Billing:Mode": ["test", "live"]},
                                  "types": {"Billing:Mode": {"type": "choice"}, "Stripe:ApiKey": {"type": "text"},
                                            "Promo:EndsOn": {"type": "date", "format": "yyyy-MM-dd"}}}},
                         manifest["settings"])
        plain = release.validate(copy.deepcopy(DESCRIPTOR))
        _, untyped = release.render(plain, release.plan(plain, self.repo, self.first, None), images)
        self.assertNotIn("settings", untyped)

    def test_a_bundle_carries_generate_specs_and_links_beside_types(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared["services"]["api"]["settings"]["keys"] = {
            "Stripe:ApiKey": {"secret": True, "required": True},
            "Auth:ClientSecret": {"secret": True, "required": True, "generate": {"random": 32}},
            "Auth:Salt": {"secret": True, "generate": {"random": 16, "encoding": "hex"}},
            "Auth:SigningKey": {"secret": True, "generate": {"command": ["dotnet", "Pilot.Api.dll", "keys", "generate"],
                                                             "timeout": "5m"}},
            "Auth:Issuer": {"from": "Sites:app:Url"},
        }
        declared["services"]["edge"]["settings"] = {"keys": {"Desk:ClientSecret": {"from": "Auth:ClientSecret"}}}
        descriptor = release.validate(declared)
        planned = release.plan(descriptor, self.repo, self.first, None)
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
        _, manifest = release.render(descriptor, planned, images)
        settings = manifest["settings"]
        # `generate` and `links` sit after `types`; a linked key appears under `links` only, and a generated one still
        # lists under `secrets` and `types` like any secret text key.
        self.assertEqual(["secrets", "types", "generate", "links"], list(settings["api"]))
        self.assertEqual(["Stripe:ApiKey", "Auth:ClientSecret", "Auth:Salt", "Auth:SigningKey"], settings["api"]["secrets"])
        self.assertEqual({"Stripe:ApiKey", "Auth:ClientSecret", "Auth:Salt", "Auth:SigningKey"}, set(settings["api"]["types"]))
        self.assertEqual({"Auth:ClientSecret": {"random": 32, "encoding": "base64url"},
                          "Auth:Salt": {"random": 16, "encoding": "hex"},
                          "Auth:SigningKey": {"command": ["dotnet", "Pilot.Api.dll", "keys", "generate"],
                                              "timeoutSeconds": 300}}, settings["api"]["generate"])
        self.assertEqual({"Auth:Issuer": "Sites:app:Url"}, settings["api"]["links"])
        self.assertEqual({"links": {"Desk:ClientSecret": "Auth:ClientSecret"}}, settings["edge"])
        # Only a key that is not linked joins the required configuration.
        self.assertEqual({"api": ["Database:Connection", "Stripe:ApiKey", "Auth:ClientSecret"], "edge": []},
                         manifest["requiredConfiguration"])
        # The bundle holds the recipe, never a value.
        self.assertNotIn("optional", settings["api"])

    def test_a_descriptor_without_generate_or_from_builds_the_same_bundle_byte_for_byte(self):
        # The digests were taken from the generator before `generate` and `from` existed.
        planned = {"release": "v1.2.3", "kind": "release", "sourceCommit": "a" * 40,
                   "services": {name: {"version": "1.2.3", "changedIn": "v1.2.3"} for name in ("api", "edge")}}
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in ("api", "edge")}
        typed = copy.deepcopy(DESCRIPTOR)
        typed["services"]["api"]["settings"]["keys"] = {
            "Billing:Mode": {"type": "choice", "values": ["test", "live"], "default": "test"},
            "Stripe:ApiKey": {"secret": True, "required": True},
            "Promo:EndsOn": {"type": "date", "format": "dd.MM.yyyy"},
            "Notes:Banner": None,
        }
        expected = {
            "plain": ("a8e69c8526bbfe204dca10c7ec059508f2c0696148eea358407138be3ce15969",
                      "2124a7591228db8895e845e77b8a576c0a3547d9a328148b14613e7d1f723f87"),
            "typed": ("a8e69c8526bbfe204dca10c7ec059508f2c0696148eea358407138be3ce15969",
                      "5043ca5f8c561bf8138140474f8afdaa2375f7a98379978bde2b231bde40cfc9"),
        }
        for label, declared in (("plain", DESCRIPTOR), ("typed", typed)):
            with self.subTest(label):
                compose_bytes, manifest = release.render(release.validate(copy.deepcopy(declared)), planned, images)
                release_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
                self.assertEqual(expected[label], (hashlib.sha256(compose_bytes).hexdigest(),
                                                   hashlib.sha256(release_bytes).hexdigest()))
                self.assertNotIn(b'"generate"', release_bytes)
                self.assertNotIn(b'"links"', release_bytes)

    def test_a_bundle_carries_the_declared_environment_mapping_and_dev_check(self):
        declared = copy.deepcopy(DESCRIPTOR)
        declared.update(dotnetEnvironment=True,
                        verify={"service": "api", "command": ["sh", "-c", "curl --fail $VERIFY_SERVICE_API/health"],
                                "timeout": "2m"})
        descriptor = release.validate(declared)
        planned = release.plan(descriptor, self.repo, self.first, None)
        images = {name: "ghcr.io/o/pilot/" + name + "@sha256:" + "c" * 64 for name in descriptor["services"]}
        _, manifest = release.render(descriptor, planned, images)
        self.assertTrue(manifest["dotnetEnvironment"])
        self.assertEqual({"service": "api", "command": ["sh", "-c", "curl --fail $VERIFY_SERVICE_API/health"],
                          "timeoutSeconds": 120}, manifest["verify"])


class VersionTests(unittest.TestCase):
    """X.Y from the newest version-track folder, Z from the release tags, the rest from the branch."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "config", "tag.gpgsign", "false")
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "one")
        self.write("engineering/planning/version-track/version-track.md", "lead")
        self.write("engineering/planning/version-track/v0.2/v0.2.md", "old")
        self.write("engineering/planning/version-track/v0.10/v0.10.md", "active")
        self.first = self.commit("first")

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def test_the_line_is_the_newest_version_folder_compared_as_numbers(self):
        self.assertEqual((0, 10), release.version_line(self.repo, self.first))

    def test_a_repository_without_a_version_track_is_line_zero(self):
        git(self.repo, "rm", "-q", "-r", "engineering/planning")
        second = self.commit("no track")
        self.assertEqual((0, 0), release.version_line(self.repo, second))

    def test_the_patch_follows_the_highest_release_tag_of_the_line(self):
        self.assertEqual(0, release.next_patch(self.repo, (0, 10)))
        for tag in ("v0.10.0", "v0.10.3", "v0.10.4-rc.1", "v0.9.7"):
            git(self.repo, "tag", tag)
        self.assertEqual(4, release.next_patch(self.repo, (0, 10)))
        self.assertEqual(8, release.next_patch(self.repo, (0, 9)))

    def test_main_releases_the_next_patch(self):
        git(self.repo, "tag", "v0.10.0")
        identity = release.build_identity(self.repo, self.first, "main")
        self.assertEqual({"channel": "release", "kind": "release", "version": "0.10.1", "release": "v0.10.1",
                          "tag": "0.10.1", "moving": "latest"}, identity)

    def test_a_dev_branch_builds_an_ordinary_candidate(self):
        git(self.repo, "checkout", "-q", "-b", "dev")
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "two")
        second = self.commit("dev work")
        dev = release.build_identity(self.repo, second, "dev")
        self.assertEqual(("branch", "candidate", "0.10.0-dev.1", "sha-" + second[:7], "sha-" + second[:7], None),
                         (dev["channel"], dev["kind"], dev["version"], dev["release"], dev["tag"], dev["moving"]))

    def test_a_test_branch_builds_an_ordinary_candidate(self):
        git(self.repo, "checkout", "-q", "-b", "test")
        self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", "two")
        second = self.commit("test work")
        test = release.build_identity(self.repo, second, "test")
        self.assertEqual(("branch", "candidate", "0.10.0-test.1", "sha-" + second[:7], "sha-" + second[:7], None),
                         (test["channel"], test["kind"], test["version"], test["release"], test["tag"], test["moving"]))

    def test_any_other_branch_builds_a_candidate_named_after_it(self):
        git(self.repo, "checkout", "-q", "-b", "feat/Login")
        for step in ("a", "b"):
            self.write("engineering/codebase/pilot.backend-services/Pilot.Api/Program.cs", step)
            head = self.commit(step)
        identity = release.build_identity(self.repo, head, "feat/Login")
        self.assertEqual(("branch", "0.10.0-feat-login.2", "sha-" + head[:7], None),
                         (identity["channel"], identity["version"], identity["tag"], identity["moving"]))
        self.assertEqual("b2024", release.tag_safe("2024"))

    def test_a_dispatched_commit_carries_its_hash(self):
        identity = release.build_identity(self.repo, self.first)
        self.assertEqual(("candidate", "0.10.0-g" + self.first[:7]), (identity["kind"], identity["version"]))

    def test_a_docs_only_push_publishes_nothing_but_a_descriptor_change_does(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        base = {"product": "pilot", "release": "v0.10.0", "sourceCommit": self.first,
                "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST}}
        self.write("README.md", "docs")
        docs = self.commit("docs")
        self.assertTrue(release.nothing_to_publish(release.plan(descriptor, self.repo, docs, base, "v0.10.1")))
        self.write("engineering/deployment/deploy.yml", "descriptor: 1")
        changed = self.commit("descriptor")
        planned = release.plan(descriptor, self.repo, changed, base, "v0.10.1", "0.10.1")
        self.assertTrue(planned["descriptorChanged"])
        self.assertFalse(release.nothing_to_publish(planned))

    def test_the_build_version_names_each_rebuilt_service(self):
        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        planned = release.plan(descriptor, self.repo, self.first, None, None, "0.10.0-dev.3")
        self.assertEqual({"0.10.0-dev.3"}, {entry["version"] for entry in planned["services"].values()})
        images = {name: "ghcr.io/o/pilot/" + name + DIGEST for name in descriptor["services"]}
        identity = {"version": "0.10.0-dev.3", "channel": "branch"}
        _, manifest = release.render(descriptor, planned, images, "feat/login", identity=identity)
        self.assertEqual(("0.10.0-dev.3", "branch", "feat/login"),
                         (manifest["version"], manifest["channel"], manifest["branch"]))


class NotesTests(unittest.TestCase):
    """The commits a main release's notes list above GitHub's generated notes."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "config", "tag.gpgsign", "false")

    def tearDown(self):
        self.temporary.cleanup()

    def commit(self, message):
        git(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def test_notes_list_the_commits_since_the_previous_release_newest_first(self):
        self.commit("feat: first page")
        git(self.repo, "tag", "v0.1.0")
        self.commit("fix: kept the login\n\nA longer body that stays out of the notes.")
        head = self.commit("feat: added billing")
        short = git(self.repo, "rev-parse", "--short", "HEAD")
        notes = release.release_notes(self.repo, head, "v0.1.0").splitlines()
        self.assertEqual("Built by wow-two-platform.pipelines from " + head + ".", notes[0])
        self.assertEqual("## Commits since v0.1.0", notes[2])
        self.assertEqual(["- feat: added billing (" + short + ")", "- fix: kept the login"],
                         [notes[4], notes[5].rsplit(" (", 1)[0]])
        self.assertEqual(6, len(notes))

    def test_a_first_release_lists_its_history_and_a_long_one_is_cut_at_the_limit(self):
        for number in range(5):
            head = self.commit("feat: step " + str(number))
        with unittest.mock.patch.object(release, "NOTES_LIMIT", 3):
            notes = release.release_notes(self.repo, head).splitlines()
        self.assertEqual("## Commits", notes[2])
        self.assertEqual(["- feat: step 4", "- feat: step 3", "- feat: step 2"],
                         [line.rsplit(" (", 1)[0] for line in notes[4:7]])
        self.assertEqual("- …and 2 more", notes[7])

    def test_a_lost_base_warning_leads_the_notes_and_no_warning_leaves_them_unchanged(self):
        self.commit("feat: first page")
        git(self.repo, "tag", "v0.1.0")
        head = self.commit("feat: added billing")
        plain = release.release_notes(self.repo, head, "v0.1.0")
        self.assertEqual(plain, release.release_notes(self.repo, head, "v0.1.0", None))
        warning = "Base v0.1.0's source commit (1bcb472) is not in this history; every service was rebuilt."
        warned = release.release_notes(self.repo, head, "v0.1.0", warning).splitlines()
        self.assertEqual(["Built by wow-two-platform.pipelines from " + head + ".", "", "**Warning:** " + warning, "",
                          "## Commits since v0.1.0"], warned[:5])
        self.assertEqual(plain.splitlines()[3:], warned[5:])

    def test_no_commit_since_the_previous_release_says_so_and_an_unknown_tag_is_refused(self):
        head = self.commit("feat: only")
        git(self.repo, "tag", "v0.1.0")
        self.assertIn("- No commits since v0.1.0.", release.release_notes(self.repo, head, "v0.1.0"))
        with self.assertRaisesRegex(release.Invalid, "--previous names no commit"):
            release.release_notes(self.repo, head, "v9.9.9")

    @unittest.skipUnless(HAS_YAML, "PyYAML reads the workflow")
    def test_the_publish_workflow_writes_the_notes_and_github_generated_notes_on_a_main_release(self):
        import yaml
        workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/publish.yml").read_text())
        steps = workflow["jobs"]["bundle"]["steps"]
        fetch = next(step for step in steps if step.get("name", "").startswith("Fetch the previous release"))
        self.assertIn('echo "PREVIOUS_RELEASE=$previous" >> "$GITHUB_ENV"', fetch["run"])
        publish = next(step for step in steps if step.get("name", "").startswith("Publish the release"))
        self.assertIn("steps.build.outputs.kind == 'release'", publish["if"])
        for part in ("release.py \"${notes[@]}\"", "--previous \"$PREVIOUS_RELEASE\"", "--notes-file",
                     "--generate-notes", "--notes-start-tag \"$PREVIOUS_RELEASE\""):
            self.assertIn(part, publish["run"])
        self.assertNotIn("--notes \"", publish["run"])
        # The lost-base warning travels build output -> environment -> `notes --warning`, never into the shell text.
        self.assertEqual("${{ steps.build.outputs.warning }}", publish["env"]["BASE_WARNING"])
        self.assertIn('--warning "$BASE_WARNING"', publish["run"])
        self.assertNotIn("outputs.warning", publish["run"])
        self.assertEqual(0, next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))
                         ["with"]["fetch-depth"])


class CiTests(unittest.TestCase):
    """The workflow's `ci` step with Docker stubbed: what it publishes and what it reports."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "commit.gpgsign", "false")
        for path, content in (("engineering/codebase/pilot.backend-services/Program.cs", "one"),
                              ("engineering/planning/version-track/v0.4/v0.4.md", "active")):
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        self.built, self.moved, self.caches = [], [], []
        self.stubs = [unittest.mock.patch.object(release, "build_image", self.fake_build),
                      unittest.mock.patch.object(release, "move_tag", lambda image, tag: self.moved.append((image, tag)))]
        for stub in self.stubs:
            stub.start()
        self.outputs = Path(self.temporary.name) / "outputs"
        self.environment = unittest.mock.patch.dict("os.environ", {"GITHUB_OUTPUT": str(self.outputs)})
        self.environment.start()
        self.cwd = os.getcwd()
        os.chdir(self.temporary.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.environment.stop()
        for stub in self.stubs:
            stub.stop()
        self.temporary.cleanup()

    def fake_build(self, descriptor, name, checkout, repository, tag, app_version=None, vcs_ref=None, cache=None):
        self.built.append((name, tag, app_version, vcs_ref))
        self.caches.append(cache)
        return repository + "@sha256:" + hashlib.sha256((name + tag).encode()).hexdigest()

    def run_ci(self, branch, base=None, cache=None, pins=None):
        arguments = argparse.Namespace(repo=str(self.repo), branch=branch, main="main", registry="ghcr.io/o/pilot",
                                       output="bundle", rollback_compatible=False, cache=cache, pin=pins)
        with contextlib.redirect_stdout(io.StringIO()) as printed, contextlib.redirect_stderr(io.StringIO()) as logged:
            release.ci(arguments, self.commit, self.descriptor, base)
        self.logged = logged.getvalue()
        return json.loads(printed.getvalue()), dict(line.split("=", 1) for line in self.outputs.read_text().splitlines())

    def test_main_releases_tags_every_image_latest_and_writes_the_archive(self):
        result, outputs = self.run_ci("main")
        self.assertEqual({"status": "built", "kind": "release", "channel": "release", "version": "0.4.0",
                          "release": "v0.4.0", "archive": "pilot-release.tar.gz"}, outputs)
        self.assertEqual({("api", "0.4.0", "0.4.0", self.commit), ("edge", "0.4.0", "0.4.0", self.commit)},
                         set(self.built))
        self.assertEqual({"latest"}, {tag for _, tag in self.moved})
        self.assertTrue(Path(self.temporary.name, "pilot-release.tar.gz").is_file())
        self.assertEqual("0.4.0", json.loads(Path(self.temporary.name, "bundle", "release.json").read_text())["version"])

    def test_a_branch_build_tags_its_commit_and_writes_no_archive(self):
        result, outputs = self.run_ci("dev")
        self.assertEqual(("built", "candidate", "branch", ""), (outputs["status"], outputs["kind"], outputs["channel"],
                                                                outputs.get("archive", "")))
        self.assertEqual({"sha-" + self.commit[:7]}, {tag for _, tag, _, _ in self.built})
        self.assertEqual(set(), {tag for _, tag in self.moved})

    def test_a_lost_base_rebuilds_every_service_and_warns_in_the_log_and_the_outputs(self):
        base = {"product": "pilot", "release": "v0.3.0", "sourceCommit": "1bcb4722ec27bbe4dff2f955f2ea50572f0af591",
                "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST},
                "versions": {"api": {"version": "0.3.0", "changedIn": "v0.3.0"},
                             "edge": {"version": "0.2.0", "changedIn": "v0.2.0"}}}
        result, outputs = self.run_ci("main", base)
        warning = "Base v0.3.0's source commit (1bcb472) is not in this history; every service was rebuilt."
        self.assertEqual((warning, "built", "v0.4.0"), (outputs["warning"], outputs["status"], outputs["release"]))
        self.assertEqual(warning, result["warning"])
        self.assertIn("::warning title=release base::" + warning + "\n", self.logged)
        self.assertEqual({"api", "edge"}, {name for name, _, _, _ in self.built})
        manifest = json.loads(Path(self.temporary.name, "bundle", "release.json").read_text())
        self.assertEqual({"api": {"version": "0.4.0", "changedIn": "v0.4.0"},
                          "edge": {"version": "0.4.0", "changedIn": "v0.4.0"}}, manifest["versions"])
        self.assertTrue(all(DIGEST not in image for image in manifest["images"].values()))

    def test_a_lost_base_still_builds_a_branch_push_a_known_base_would_skip(self):
        base = {"product": "pilot", "release": "v0.3.0", "sourceCommit": "1" * 40,
                "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST}}
        result, outputs = self.run_ci("dev", base)
        self.assertEqual("built", outputs["status"])
        self.assertEqual({"api", "edge"}, {name for name, _, _, _ in self.built})

    def test_a_known_base_adds_no_warning_to_the_log_the_outputs_or_the_result(self):
        base = {"product": "pilot", "release": "v0.4.0", "sourceCommit": self.commit,
                "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST}}
        result, outputs = self.run_ci("main", base)
        self.assertEqual({"status", "kind", "channel", "version", "release", "archive"}, set(outputs))
        self.assertNotIn("warning", result)
        self.assertEqual("", self.logged)

    def test_the_layer_cache_reaches_every_build_and_no_cache_is_the_default(self):
        self.run_ci("main")
        self.assertEqual([None, None], self.caches)
        self.caches.clear()
        self.run_ci("main", cache="gha")
        self.assertEqual(["gha", "gha"], self.caches)

    def test_a_pinned_image_is_never_built_or_retagged_and_takes_its_resolved_digest(self):
        self.descriptor = release.validate(with_pinned())
        with self.assertRaisesRegex(release.Invalid, "identity: pass the digest"):
            self.run_ci("main")
        self.assertEqual([], self.built)
        result, outputs = self.run_ci("main", pins=["identity=" + PIN])
        self.assertEqual("built", outputs["status"])
        self.assertEqual({"api", "edge"}, {name for name, _, _, _ in self.built})
        self.assertEqual({("ghcr.io/o/pilot/api", "latest"), ("ghcr.io/o/pilot/edge", "latest")},
                         {(image.split("@", 1)[0], tag) for image, tag in self.moved})
        manifest = json.loads(Path(self.temporary.name, "bundle", "release.json").read_text())
        self.assertEqual(IDENTITY + "@" + PIN, manifest["images"]["identity"])
        self.assertEqual({"version": "0.3.0", "changedIn": "v0.4.0"}, manifest["versions"]["identity"])
        compose = json.loads(Path(self.temporary.name, "bundle", "compose.json").read_text())
        self.assertEqual(IDENTITY + "@" + PIN, compose["services"]["identity"]["image"])

    def test_a_push_that_changes_nothing_is_skipped(self):
        base = {"product": "pilot", "release": "v0.4.0", "sourceCommit": self.commit,
                "images": {"api": "ghcr.io/o/pilot/api" + DIGEST, "edge": "ghcr.io/o/pilot/edge" + DIGEST}}
        result, outputs = self.run_ci("main", base)
        self.assertEqual("skipped", outputs["status"])
        self.assertEqual([], self.built)


class BuildImageTests(unittest.TestCase):
    """The docker buildx call for one service, with Docker stubbed."""

    def test_a_build_receives_its_version_and_source_commit(self):
        calls = []

        def fake_docker(*arguments):
            calls.append(arguments)
            metadata = arguments[arguments.index("--metadata-file") + 1]
            Path(metadata).write_text(json.dumps({"containerimage.digest": DIGEST[1:]}))
            return ""

        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        commit = "c" * 40
        with unittest.mock.patch.object(release, "docker", fake_docker):
            image = release.build_image(descriptor, "api", "/checkout", "ghcr.io/o/pilot/api", "0.4.0", "0.4.0", commit)
        self.assertEqual("ghcr.io/o/pilot/api" + DIGEST, image)
        arguments = list(calls[0])
        build_args = [arguments[index + 1] for index, item in enumerate(arguments) if item == "--build-arg"]
        self.assertEqual(["SERVICE=Api", "APP_VERSION=0.4.0", "VCS_REF=" + commit], build_args)
        self.assertNotIn("--cache-from", arguments)

    def test_the_gha_cache_reads_and_writes_one_scope_per_service_and_never_fails_the_build(self):
        calls = []

        def fake_docker(*arguments):
            calls.append(arguments)
            Path(arguments[arguments.index("--metadata-file") + 1]).write_text(
                json.dumps({"containerimage.digest": DIGEST[1:]}))
            return ""

        descriptor = release.validate(copy.deepcopy(DESCRIPTOR))
        with unittest.mock.patch.object(release, "docker", fake_docker):
            release.build_image(descriptor, "api", "/checkout", "ghcr.io/o/pilot/api", "0.4.0", "0.4.0", "c" * 40,
                                "gha")
        arguments = list(calls[0])
        self.assertEqual("type=gha,scope=api", arguments[arguments.index("--cache-from") + 1])
        self.assertEqual("type=gha,mode=min,scope=api,ignore-error=true", arguments[arguments.index("--cache-to") + 1])
        self.assertEqual("/checkout/engineering/codebase", arguments[-1])


if __name__ == "__main__":
    unittest.main()
