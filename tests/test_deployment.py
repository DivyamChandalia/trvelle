import importlib.util
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('deployment', Path(__file__).parents[1] / 'deploy/actions-deploy.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)

class DeploymentTests(unittest.TestCase):
    def test_only_component_and_commit_are_accepted(self):
        with patch('sys.argv', ['deploy', 'website ' + 'a' * 40]):
            self.assertEqual(deployment.command(), ('website', 'a' * 40))
        for command in ('bash', 'website main', 'backend ' + 'a' * 40 + '; id'):
            with patch('sys.argv', ['deploy', command]), self.assertRaises(ValueError):
                deployment.command()

    def test_archive_rejects_escape_secrets_and_links(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for name, kind in [('../escape', tarfile.REGTYPE), ('.env', tarfile.REGTYPE),
                               ('node_modules/code.js', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)]:
                bundle = root / 'source.tar'
                with tarfile.open(bundle, 'w') as output:
                    member = tarfile.TarInfo(name); member.type = kind
                    output.addfile(member)
                with self.assertRaises(ValueError):
                    deployment.unpack(bundle, root / 'out')
            with tarfile.open(root / 'source.tar', 'w') as output:
                member = tarfile.TarInfo('app.py'); member.size = 4
                output.addfile(member, io.BytesIO(b'pass'))
            deployment.unpack(root / 'source.tar', root / 'out')
            self.assertEqual((root / 'out/app.py').read_text(), 'pass')
