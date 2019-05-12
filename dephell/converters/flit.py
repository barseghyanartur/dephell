from pathlib import Path

# external
import tomlkit
from dephell_specifier import RangeSpecifier
from packaging.requirements import Requirement

# app
from ..controllers import DependencyMaker, Readme
from .base import BaseConverter
from ..models import RootDependency, Author, EntryPoint
from .egginfo import EggInfoConverter


class FlitConverter(BaseConverter):
    lock = False

    def loads(self, content: str) -> RootDependency:
        doc = tomlkit.parse(content)
        section = doc['tool']['flit']['metadata']
        root = RootDependency(
            raw_name=section.get('dist-name') or section['module'],
            python=RangeSpecifier(section.get('requires-python')),
            classifiers=section.get('classifiers', tuple()),
            license=section.get('license', ''),
            keywords=tuple(section.get('keywords', '').split(',')),
        )

        # description
        if 'description-file' in section:
            root.readme = Readme(path=section['description-file'])

        # entrypoints
        entrypoints = []
        path = Path(section.get('entry-points-file', 'entry_points.txt'))
        if path.exists():
            with path.open('rb', encoding='utf-8') as stream:
                tmp_root = EggInfoConverter().parse_entrypoints(content=stream.read())
                entrypoints = list(tmp_root.entrypoints)
        for group, subentrypoints in section.get('entrypoints', {}).items():
            for entrypoint in subentrypoints:
                entrypoints.append(EntryPoint.parse(text=entrypoint, group=group))
        for entrypoint in section.get('scripts', {}).values():
            entrypoints.append(EntryPoint.parse(text=entrypoint))
        root.entrypoints = tuple(entrypoints)

        # authors
        authors = []
        if 'author' in section:
            authors.append(Author(
                name=section['author'],
                mail=section['author-email'],
            ))
        if 'maintainer' in section:
            authors.append(Author(
                name=section['maintainer'],
                mail=section['maintainer-email'],
            ))
        root.authors = tuple(authors)

        # links
        if 'home-page' in section:
            root.links['home'] = section['home-page']
        if 'urls' in section:
            root.links.update(section['urls'])

        # requirements
        for req in section['requires']:
            root.attach_dependencies(DependencyMaker.from_requirement(
                source=root,
                req=Requirement(req),
            ))
        for req in section['dev-requires']:
            root.attach_dependencies(DependencyMaker.from_requirement(
                source=root,
                req=Requirement(req),
                envs={'dev'},
            ))

        # extras
        for extra, reqs in section.get('requires-extra', {}).items():
            for req in reqs:
                req = Requirement(req)
                root.attach_dependencies(DependencyMaker.from_requirement(
                    source=root,
                    req=req,
                    envs={'main', extra},
                ))

        return root

    def dumps(self, reqs, project: RootDependency, content=None) -> str:
        # read config
        if content:
            doc = tomlkit.parse(content)
        else:
            doc = tomlkit.document()

        # get tool section from config
        if 'tool' not in doc:
            doc['tool'] = {'flit': {'metadata': tomlkit.table()}}
        elif 'flit' not in doc['tool']:
            doc['tool']['flit'] = {'metadata': tomlkit.table()}
        elif 'metadata' not in doc['tool']['flit']:
            doc['tool']['flit']['metadata'] = tomlkit.table()
        section = doc['tool']['flit']['metadata']

        # project and module names
        module = project.package.packages[0].module
        section['module'] = module
        if project.raw_name != module:
            section['dist-name'] = project.raw_name
        elif 'dist-name' in section:
            del section['dist-name']

        # author and maintainer
        for field, author in zip(('author', 'maintainer'), project.authors):
            # add name
            section[field] = author.name
            # add new or remove old mail
            field = field + '-email'
            if author.mail:
                section[field] = author.mail
            elif field in section:
                del section[field]
        if not project.authors:         # remove old author
            if 'author' in section:
                del section['author']
            if 'author-email' in section:
                del section['author-email']
        if len(project.authors) < 2:    # remove old maintainer
            if 'maintainer' in section:
                del section['maintainer']
            if 'maintainer-email' in section:
                del section['maintainer-email']

        # metainfo
        for field in ('license', 'keywords', 'classifiers'):
            value = getattr(project, field)
            if isinstance(value, tuple):
                value = list(value)
            if not value:   # delete
                if field in section:
                    del section[field]
            elif field not in section:  # insert
                section[field] = value
            elif section[field].value != value:  # update
                section[field] = value

        # write links
        if 'homepage' in project.links:
            section['home-page'] = project.links['homepage']
        if set(project.links) - {'homepage'}:
            if 'urls' in section:
                # remove old
                for name in section['urls']:
                    if name not in project.links:
                        del section['urls'][name]
            else:
                section['urls'] = tomlkit.table()
            # add and update
            for name, url in project.links.items():
                if name == 'homepage':
                    continue
            section['urls'][name] = url
        elif 'urls' in section:
            del section['urls']

        # readme
        if project.readme:
            section['description-file'] = project.readme.path.name
        elif 'description-file' in section:
            del section['description-file']

        # python constraint
        python = str(project.python)
        if python not in ('', '*'):
            section['requires-python'] = python
        elif 'requires-python' in section:
            del section['requires-python']

        # dependencies
        for section_name, is_dev in [('requires', False), ('dev-requires', True)]:
            if section_name not in section:
                section[section_name] = tomlkit.array()
            for req in sorted(reqs):
                if req.main_envs:
                    continue
                if is_dev is req.is_dev:
                    section[section_name].append(self._format_req(req=req))
            if not section[section_name].value:
                del section[section_name]

        # extras
        if 'requires-extra' in section:
            envs = sum((req.main_envs for req in reqs), set())
            for env in section['requires-extra']:
                if env in envs:
                    # clean env from old packages
                    section['requires-extra'][env] = tomlkit.array()
                else:
                    # remove old env
                    del section['requires-extra'][env]
        else:
            section['requires-extra'] = tomlkit.table()
        # write new extra packages
        for req in sorted(reqs):
            for env in req.main_envs:
                section['requires-extra'][env].append(self._format_req(req=req))

        return tomlkit.dumps(doc)

    def _format_req(self, req):
        line = req.name
        if req.extras:
            line += '[{extras}]'.format(extras=','.join(req.extras))
        line += req.version
        if req.markers:
            line += '; ' + req.markers
        return line
