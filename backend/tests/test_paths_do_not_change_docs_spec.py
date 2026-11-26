"""Section 3 of the paths contract (g2-contract-paths-do-not-change), checked lightly in the docs' text: one section
says how paths work and what to do when media moves, the wrong sentences are gone, the SDK reference and the
CHANGELOG name the new parts. Written from the contract, not from the change; the behaviour is
test_paths_do_not_change_spec.py.

How "lightly" is read:
- 3.1: "one section" is a heading of docs/SDK.md or docs/DATASET_MANAGEMENT.md below the page title, whose title names
  paths, files, media, a move or where things are, with the text under it (its subsections and fenced code included) up
  to the next heading of its level or higher: a whole page or chapter that names everything in different places isn't
  one section. It must name the three kinds of path (relative, with `FRIDGE_DATA_ROOT`; full local, as "absolute" or
  "full"; cloud, as `s3://`), the three ways to deal with moved media (put it back: "mount" or "put ... back"; the copy:
  `add_samples_from` with `path_prefixes`; add the samples again: "again" or "re-add"), and that a path can't be
  changed. "Pointing to it" is a Markdown link to the file holding the section with the anchor (as GitHub makes
  anchors) of that section's heading or of a heading inside it; a link to the page or to a chapter it lies in is not.
  "Prefer relative paths" and what a copy carries are not checked: their wording is free. The private marks: a
  paragraph of the section names private fields, the assistant and marking them again.
- 3.2: the two wrong sentences quoted by the survey are gone; the `to_patches` docstring names the copy
  (`add_samples_from` or `path_prefixes`); the backup guide points to the section (a link as in 3.1) anywhere in the
  part of the page, between two headings, that says a restored database's media loads only from the same paths.
- 3.3: the SDK reference is docs/SDK.md's "API Reference": its `def add_samples_from(` line, the comment lines directly
  above it and the lines below it up to a blank line or the next `def`, together name `path_prefixes` and
  `rewritten`.
- 3.4: in CHANGELOG.md's Unreleased section, an item under "Added" names `path_prefixes`, and an item under "Fixed" or
  "Changed" names the path (`filepath` or "path") and says it can't be changed or was ignored.
- 5.6: a paragraph of the section names objects and their images; an Unreleased "Fixed" item names objects and the
  copy (`add_samples_from`, or "copy"). 5.3: a paragraph of the uploader docs with the one after it, and an item of
  CHANGELOG.md's Unreleased section, name objects and the copy and say to delete them and copy again.
"""

import inspect
import re
from pathlib import Path

from fridge.core.dataset import Dataset

from backend.tests.changelog_helpers import release_heading

ROOT = Path(__file__).resolve().parents[2]
UPLOADER_DOCS = ('docs/SDK.md', 'docs/DATASET_MANAGEMENT.md')
CANT_CHANGE = r"can(?:no|'|’)t be changed|cannot change|can(?:'|’)t change"
ABOUT_PATHS = r'path|file|media|mov|where|locat'  # what the section's title is about


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding='utf-8')


def headings(text: str) -> list[tuple[int, int, str]]:
    """(line index, level, title) of each Markdown heading outside fenced code."""
    found, fenced = [], False
    for index, line in enumerate(text.splitlines()):
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
            continue
        match = re.match(r'^(#{1,6})\s+(.*)', line)
        if match and not fenced:
            found.append((index, len(match.group(1)), match.group(2).strip()))
    return found


def sections(text: str, below: int = 0) -> list[tuple[str, str]]:
    """(title, text) of each heading deeper than level `below` with everything under it, up to the next heading of its
    level or higher."""
    lines, heads = text.splitlines(), headings(text)
    out = []
    for position, (index, level, title) in enumerate(heads):
        end = next((i for i, lvl, _ in heads[position + 1 :] if lvl <= level), len(lines))
        if level > below:
            out.append((title, '\n'.join(lines[index:end])))
    return out


def missing_from(body: str) -> list[str]:
    """What 3.1 asks the section to name that `body` doesn't."""
    wanted = {
        'path_prefixes': r'path_prefixes',
        'add_samples_from': r'add_samples_from',
        'FRIDGE_DATA_ROOT': r'FRIDGE_DATA_ROOT',
        'relative paths': r'\brelative\b',
        'full local paths': r'\b(?:absolute|full)\b',
        'cloud paths': r's3://',
        'put the media back': r'\bmount|put (?:it|them|the media|the files) back',
        'add the samples again': r'\bagain\b|\bre-?add',
        "a path can't be changed": CANT_CHANGE,
    }
    return [name for name, pattern in wanted.items() if not re.search(pattern, body, re.IGNORECASE)]


def says_all(text: str, patterns: tuple[str, ...]) -> bool:
    return all(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def slug(title: str) -> str:
    """A heading's anchor as GitHub makes it."""
    return re.sub(r'[^\w\- ]', '', title.strip().lower()).replace(' ', '-')


def the_section() -> tuple[str, str, str]:
    """(file, title, text) of the smallest section about paths in an uploader doc that names everything 3.1 asks."""
    about = [
        (len(missing_from(body)), len(body), name, title, body)
        for name in UPLOADER_DOCS
        for title, body in sections(read(name), below=1)
        if re.search(ABOUT_PATHS, title, re.IGNORECASE)
    ]
    assert about, f'no section of {UPLOADER_DOCS} has a title about paths'
    lacking, _, name, title, body = min(about)
    assert not lacking, f'no section about paths in {UPLOADER_DOCS} names all of 3.1; {name} "{title}" lacks ' + str(
        missing_from(body),
    )
    return name, title, body


def points_to(text: str, name: str, body: str) -> bool:
    """`text` has a Markdown link to the section `body` of the file `name` (a docs/ file): to that file, with the anchor
    of the section's heading or of a heading inside it."""
    anchors = {slug(title) for title, section in sections(read(name), below=1) if section in body}
    for target in re.findall(r'\]\(([^)\s]+)\)', text):
        file, _, anchor = target.partition('#')
        if Path(file).name == Path(name).name and anchor in anchors:
            return True
    return False


def test_one_section_says_how_paths_work_and_what_to_do_when_media_moves():
    """3.1: one section of the uploader docs names the three kinds of path, the three ways and `path_prefixes`."""
    the_section()


def test_the_other_uploader_doc_points_to_the_section():
    """3.1: "with the other pointing to it"."""
    name, title, body = the_section()
    [other] = [doc for doc in UPLOADER_DOCS if doc != name]
    assert points_to(read(other), name, body), f'{other} has no link to {name}#{slug(title)}'


def test_the_object_patch_view_no_longer_says_an_object_has_its_images_path():
    """3.2: an object has a path of its own; the link to its image is by id."""
    assert "the **parent image's** filepath" not in read('docs/OBJECT_PATCH_VIEW.md')


def test_the_s3_permissions_page_no_longer_says_paths_are_absolute():
    """3.2: relative paths work."""
    assert 'Sample filepaths are absolute' not in read('docs/S3_PERMISSIONS.md')


def test_to_patches_says_how_to_give_the_crops_other_paths():
    """3.2: the docstring's "rewrite filepaths afterward" says how: the copy."""
    doc = inspect.getdoc(Dataset.to_patches) or ''
    assert 'add_samples_from' in doc or 'path_prefixes' in doc, doc


def own_parts(text: str) -> list[str]:
    """The text between each two headings (outside fenced code), and before the first."""
    lines = text.splitlines()
    starts = [0, *[index for index, _, _ in headings(text)], len(lines)]
    return ['\n'.join(lines[a:b]) for a, b in zip(starts, starts[1:]) if b > a]


def test_the_backup_guide_points_to_the_section_where_it_speaks_of_a_restored_databases_media():
    """3.2: where the backup guide says a restored database loads its media only where the target sees the same files at
    the same paths (the part of the page between two headings that says it), it links to the new section."""
    name, _, body = the_section()
    parts = own_parts(read('docs/BACKUP_AND_MIGRATION.md'))
    about_media = [p for p in parts if says_all(p, (r'\brestor', r'\bmedia\b', r'\bsame\b'))]
    assert about_media, 'the backup guide no longer says where a restored database finds its media'
    assert any(points_to(p, name, body) for p in about_media), about_media


def test_the_sdk_reference_documents_path_prefixes_and_rewritten():
    """3.3: the reference's entry for add_samples_from (its `def` line with the comments directly above it and the
    lines below it up to a blank line or the next `def`) names both."""
    reference = dict(sections(read('docs/SDK.md')))['API Reference'].splitlines()
    starts = [i for i, line in enumerate(reference) if 'def add_samples_from(' in line]
    assert starts, 'the API Reference has no add_samples_from'
    first = last = starts[0]
    while first > 0 and reference[first - 1].strip().startswith('#'):
        first -= 1
    while last + 1 < len(reference) and reference[last + 1].strip() and 'def ' not in reference[last + 1]:
        last += 1
    entry = '\n'.join(reference[first : last + 1])
    assert 'path_prefixes' in entry and 'rewritten' in entry, entry


def unreleased_items(heading: str) -> list[str]:
    """The list items under `### <heading>` in CHANGELOG.md's Unreleased section."""
    unreleased = dict(sections(read('CHANGELOG.md')))[release_heading('2.14.0')[3:]]
    items = []
    for title, body in sections(unreleased):
        if title == heading:
            items += [item for item in re.split(r'\n(?=- )', body) if item.startswith('- ')]
    return items


def test_the_changelog_names_the_copy_rule_and_the_refused_path_change():
    """3.4: an Added entry for the copy's path rule, and a Fixed or Changed entry saying a sample's path change now
    says it can't (it was silently ignored)."""
    assert any('path_prefixes' in item for item in unreleased_items('Added')), unreleased_items('Added')
    told = [
        item
        for item in unreleased_items('Fixed') + unreleased_items('Changed')
        if says_all(item, (r'\bpath\b|filepath', f'{CANT_CHANGE}|ignored'))
    ]
    assert told, 'no Fixed or Changed entry says a filepath change is refused (it was ignored)'


COPY = r'add_samples_from|\bcop(?:y|ies|ied)\b'
EARLIER_OBJECTS = (r'\bobjects?\b', COPY, r'\bdelet', r'\bagain\b|\bre-?cop')


def paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r'\n\s*\n', text) if p.strip()]


def test_the_section_says_a_copy_keeps_objects_with_their_images():
    """5.6."""
    _, _, body = the_section()
    assert any(says_all(p, (r'\bobjects?\b', r'\bimages?\b')) for p in paragraphs(body)), body


def test_the_docs_say_what_to_do_with_objects_an_earlier_copy_left():
    """5.3: objects copied by an earlier SDK aren't repaired by copying again: delete them in the copy, copy again (said
    in one paragraph or across it and the next)."""
    found = [
        '\n\n'.join(ps[index : index + 2])
        for ps in (paragraphs(read(name)) for name in UPLOADER_DOCS)
        for index in range(len(ps))
        if says_all('\n\n'.join(ps[index : index + 2]), EARLIER_OBJECTS)
    ]
    assert found, f'no paragraph of {UPLOADER_DOCS} says what to do with objects an earlier copy left'


def test_the_changelog_has_the_object_fix_and_what_to_do_with_earlier_copies():
    """5.6 and 5.3."""
    fixed = [item for item in unreleased_items('Fixed') if says_all(item, (r'\bobjects?\b', COPY))]
    assert fixed, 'no Fixed entry names objects and the copy'
    unreleased = dict(sections(read('CHANGELOG.md')))[release_heading('2.14.0')[3:]]
    items = [item for item in re.split(r'\n(?=- )', unreleased) if item.startswith('- ')]
    assert any(says_all(item, EARLIER_OBJECTS) for item in items), 'no Unreleased entry says what to do with them'


def test_the_section_says_to_mark_private_fields_again_before_the_assistant_reads_the_copy():
    """3.1: private marks aren't copied: the assistant can read those fields in the copy until they're marked again."""
    _, _, body = the_section()
    wanted = (r'\bprivate\b', r'\bassistant\b', r'\bagain\b')
    assert any(says_all(p, wanted) for p in paragraphs(body)), body
