from __future__ import annotations

import json
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from linode_image_lab.cli import main
from linode_image_lab.registry_version_selection import VersionSelectionError, load_selection


class VersionSelectionTests(unittest.TestCase):
    def test_private_file_produces_distinct_opaque_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "version.json"
            path.write_text(json.dumps({"version_id": "synthetic-a", "proof_key": "ab" * 32}))
            path.chmod(0o600)
            first = load_selection(str(path))
            self.assertEqual(first.version_id, "synthetic-a")
            self.assertEqual(len(first.proof()), 64)
            path.write_text(json.dumps({"version_id": "synthetic-b", "proof_key": "ab" * 32}))
            second = load_selection(str(path))
            self.assertNotEqual(first.proof(), second.proof())
            path.write_text('{"version_id":"synthetic-a","version_id":"synthetic-b","proof_key":"' + "ab" * 32 + '"}')
            with self.assertRaises(VersionSelectionError):
                load_selection(str(path))

    def test_two_cli_reads_use_same_version_after_latest_advances(self) -> None:
        class Frozen(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2029, 12, 31, 23, 30, tzinfo=tz)

        class Client:
            writes = 0
            def get_firewall_rules(self, _):
                return {"inbound": [], "outbound": [],
                        "inbound_policy": "DROP", "outbound_policy": "ACCEPT"}
            def update_firewall_rules(self, *_):
                self.writes += 1
                raise AssertionError("dry-run wrote firewall")

        class Response:
            status = 200
            headers = {"x-amz-version-id": "synthetic-version-a"}
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def read(self):
                return json.dumps({"schema_version": 1,
                    "registry": {"name": "trusted-network-registry",
                                 "generated_at": "2029-12-31T23:00:00Z",
                                 "valid_until": "2030-01-01T00:00:00Z",
                                 "publisher_version": "0.1.0"},
                    "entries": [{"id": "synthetic", "cidr": "198.51.100.1/32",
                                 "address_family": "ipv4", "kind": "static",
                                 "source_type": "config", "source_ref": "synthetic", "status": "active"}],
                    "summary": {"entry_count": 1, "static_count": 1,
                                "discovered_count": 0}}).encode()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "version.json"
            path.write_text(json.dumps({"version_id": "synthetic-version-a", "proof_key": "ab" * 32}))
            path.chmod(0o600)
            client = Client()
            urls = []
            latest_version = "synthetic-version-a"
            def storage(request, timeout):
                nonlocal latest_version
                urls.append(request.full_url)
                self.assertIn("?versionId=synthetic-version-a", request.full_url)
                latest_version = "synthetic-newer"
                return Response()
            args = ["firewall-sync", "--firewall-id", "12345",
                    "--registry-endpoint-url", "https://us-east-1.linodeobjects.com",
                    "--registry-bucket", "example-bucket", "--registry-object-key", "registry.json",
                    "--ports", "22", "--registry-version-file", str(path)]
            results = []
            with (patch.dict(os.environ, {"LINODE_OBJ_ACCESS_KEY": "synthetic-access",
                                       "LINODE_OBJ_SECRET_KEY": "synthetic-secret"}),
                  patch("linode_image_lab.firewall_sync.LinodeClient.from_env", return_value=client),
                  patch("linode_image_lab.trusted_registry.urlopen", side_effect=storage),
                  patch("linode_image_lab.trusted_registry.dt",
                        SimpleNamespace(datetime=Frozen, timezone=dt.timezone))):
                for extra in ([], ["--output-format", "summary"]):
                    output = StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(main([*args, *extra]), 0)
                    results.append(json.loads(output.getvalue()))
            self.assertEqual(len(urls), 2)
            self.assertEqual(latest_version, "synthetic-newer")
            self.assertEqual(client.writes, 0)
            proof = load_selection(str(path)).proof()
            self.assertEqual(proof, "8e52fa611aed897a6d08ae3cbb50e59c288d244524d09d545cf5ecddb4ca6d0e")
            self.assertEqual(results[0]["registry"]["version_proof"], proof)
            self.assertEqual(results[1]["version_proof"], proof)
            self.assertNotIn("synthetic-version-a", json.dumps(results))
            path.chmod(0o644)
            with self.assertRaises(VersionSelectionError):
                load_selection(str(path))
            path.unlink()
            with self.assertRaises(VersionSelectionError):
                load_selection(str(path))

    def test_secret_projection_symlink_and_nonregular_file_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            revision = root / "..synthetic-revision"
            revision.mkdir()
            target = revision / "version.json"
            target.write_text(json.dumps({"version_id": "synthetic-a", "proof_key": "ab" * 32}))
            target.chmod(0o440)
            (root / "..data").symlink_to(revision, target_is_directory=True)
            projected = root / "version.json"
            projected.symlink_to("..data/version.json")
            self.assertEqual(load_selection(str(projected)).version_id, "synthetic-a")
            projected.unlink()
            with tempfile.TemporaryDirectory() as outside:
                foreign = Path(outside) / "version.json"
                foreign.write_bytes(target.read_bytes())
                foreign.chmod(0o600)
                projected.symlink_to(foreign)
                with self.assertRaises(VersionSelectionError):
                    load_selection(str(projected))
            projected.unlink()
            os.mkfifo(projected, 0o600)
            with self.assertRaises(VersionSelectionError):
                load_selection(str(projected))
            with self.assertRaises(VersionSelectionError):
                load_selection("relative/version.json")
