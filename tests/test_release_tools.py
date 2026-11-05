import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from tools.extract_image_layers import extract
from tools.check_release import missing_modules


class SourceExportTests(unittest.TestCase):
    def test_omitted_runtime_import_is_rejected_even_if_manifest_omits_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'plex_bridge').mkdir()
            (root / 'plex_bridge/__init__.py').write_text('')
            (root / 'plex_bridge/playback.py').write_text('from plex_bridge.companion import CompanionRelay\n')
            files = {'plex_bridge/__init__.py', 'plex_bridge/playback.py'}
            self.assertEqual(len(missing_modules(root, files)), 1)
            (root / 'plex_bridge/companion.py').write_text('class CompanionRelay: pass\n')
            files.add('plex_bridge/companion.py')
            self.assertEqual(missing_modules(root, files), [])


class ImageLayerTests(unittest.TestCase):
    def test_docker_deduplicated_layers_and_unsafe_paths_stay_inside_scan_directory(self):
        layer_bytes = io.BytesIO()
        with tarfile.open(fileobj=layer_bytes, mode='w') as layer:
            item = tarfile.TarInfo('../../outside')
            item.size = 7
            layer.addfile(item, io.BytesIO(b'payload'))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / 'images.tar'
            with tarfile.open(archive, 'w') as outer:
                for name, data in [('manifest.json', json.dumps([{'Config':'config.json','Layers':['alias/layer.tar','symbol/layer.tar']}]).encode()),
                                   ('config.json', b'{}'), ('base/layer.tar', layer_bytes.getvalue())]:
                    item = tarfile.TarInfo(name);item.size=len(data)
                    outer.addfile(item, io.BytesIO(data))
                alias=tarfile.TarInfo('alias/layer.tar');alias.type=tarfile.LNKTYPE;alias.linkname='base/layer.tar'
                outer.addfile(alias)
                alias=tarfile.TarInfo('symbol/layer.tar');alias.type=tarfile.SYMTYPE;alias.linkname='../base/layer.tar';outer.addfile(alias)
            destination=root/'scan'
            extract(archive,destination)
            index=json.loads((destination/'index.json').read_text())
            payload=next(i for i in index if i['path']=='../../outside')
            self.assertEqual((destination/payload['file']).read_bytes(),b'payload')
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['images.tar','scan'])

    def test_cyclic_hardlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);archive=root/'image.tar'
            with tarfile.open(archive,'w') as outer:
                data=json.dumps([{'Config':'loop','Layers':[]}]).encode()
                item=tarfile.TarInfo('manifest.json');item.size=len(data);outer.addfile(item,io.BytesIO(data))
                item=tarfile.TarInfo('loop');item.type=tarfile.LNKTYPE;item.linkname='loop';outer.addfile(item)
            with self.assertRaises(ValueError):extract(archive,root/'scan')
