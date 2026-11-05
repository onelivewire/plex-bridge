"""Expose every regular file in Docker-save layers for scanning, including deleted files.

No filesystem paths from the image are trusted; outputs use numeric names.
The filename index preserves auditability without symlink/path traversal risks.
"""
import json
import posixpath
from pathlib import Path
import shutil
import sys
import tarfile


def extract(archive, destination):
    destination.mkdir(parents=True, exist_ok=False)
    index = []
    with tarfile.open(archive) as outer:
        manifests = json.load(outer.extractfile('manifest.json'))
        names = {item['Config'] for item in manifests}
        layers = {name for item in manifests for name in item['Layers']}
        for number, name in enumerate(sorted(names | layers)):
            member = outer.getmember(name)
            seen = set()
            while member.islnk() or member.issym():
                if member.name in seen:
                    raise ValueError('Cyclic archive link')
                seen.add(member.name)
                target = (posixpath.join(posixpath.dirname(member.name), member.linkname)
                          if member.issym() else member.linkname)
                target = posixpath.normpath(target)
                if target.startswith('/') or target == '..' or target.startswith('../'):
                    raise ValueError('Archive link escapes image')
                member = outer.getmember(target)
            if not member.isfile():
                raise ValueError('Non-regular image member')
            stream = outer.extractfile(member)
            if name in layers:
                with tarfile.open(fileobj=stream, mode='r|*') as layer:
                    for position, item in enumerate(layer):
                        if item.isfile():
                            target = destination / (str(number) + '-' + str(position))
                            with target.open('wb') as output:
                                shutil.copyfileobj(layer.extractfile(item), output)
                            index.append({'file':target.name,'layer':name,'path':item.name})
            else:
                target = destination / (str(number) + '-config.json')
                with target.open('wb') as output:
                    shutil.copyfileobj(stream, output)
                index.append({'file':target.name,'layer':name,'path':'image config'})
    (destination / 'index.json').write_text(json.dumps(index,indent=2)+'\n')
    print('Extracted',len(index),'files/configs from',len(layers),'distinct layers')


if __name__ == '__main__':
    extract(Path(sys.argv[1]), Path(sys.argv[2]))
