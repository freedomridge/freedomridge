"""Which plans tell the first page's strategy (datasets._plan_indexes_samples) that a filter has an indexed path to the
rows of the page: an index condition of the statement's own scans, never a subquery's or a probe of a row's image or
objects (a filter on a dataset with objects, backend/grain_reading.py)."""

from backend.routers.datasets import _plan_indexes_samples


def scan(condition: str, node: str = 'Index Scan', **extra: object) -> dict:
    key = 'Recheck Cond' if node == 'Bitmap Heap Scan' else 'Index Cond'
    return {'Node Type': node, 'Relation Name': 'samples', key: condition, **extra}


def with_children(node: dict, *children: dict) -> dict:
    return {**node, 'Plans': list(children)}


SEQ = {'Node Type': 'Seq Scan', 'Relation Name': 'samples'}


def test_an_index_condition_of_the_statement_s_own_scan_is_a_path():
    assert _plan_indexes_samples(scan("((metadata ->> 'batch'::text) = 'late'::text)"))
    assert _plan_indexes_samples({'Node Type': 'Limit', 'Plans': [scan('(id = ANY (\'{1,2}\'::integer[]))')]})


def test_the_dataset_alone_is_no_path():
    assert not _plan_indexes_samples(scan('(dataset_id = 18)'))
    assert not _plan_indexes_samples(scan('(dataset_id = 18)', 'Bitmap Heap Scan'))


def test_an_index_condition_in_a_subquery_is_no_path():
    # The images' grid of `colour = "red"`: the objects' lookup finds partners through the metadata index.
    partners = scan("(metadata ? 'colour'::text)", 'Bitmap Heap Scan', **{'Parent Relationship': 'SubPlan'})
    assert not _plan_indexes_samples(with_children(SEQ, partners))
    images = scan('((dataset_id = 18) AND (media_type = \'object\'::text))', **{'Parent Relationship': 'SubPlan'})
    assert not _plan_indexes_samples(with_children(SEQ, images))
    guard = scan("((metadata ->> 'batch'::text) = 'late'::text)", **{'Parent Relationship': 'InitPlan'})
    assert not _plan_indexes_samples(with_children(SEQ, guard))


def test_a_probe_of_a_row_s_image_or_objects_in_a_join_is_no_path():
    image = scan('((dataset_id = 18) AND (id = samples.parent_id))', **{'Parent Relationship': 'Inner'})
    objects = scan('((dataset_id = 18) AND (parent_id = samples.id))', **{'Parent Relationship': 'Inner'})
    for probe in (image, objects):
        anti = {'Node Type': 'Nested Loop', 'Plans': [{**SEQ, 'Parent Relationship': 'Outer'}, probe]}
        assert not _plan_indexes_samples(anti)


def test_a_join_s_inner_scan_of_the_page_s_rows_is_a_path():
    # A selection token: its rows are looked up by id from region_selections.
    rows = scan('(id = rs.sample_id)', **{'Parent Relationship': 'Inner'})
    outer = {'Node Type': 'Index Scan', 'Relation Name': 'region_selections', 'Parent Relationship': 'Outer'}
    assert _plan_indexes_samples({'Node Type': 'Nested Loop', 'Plans': [outer, rows]})


def test_the_objects_kind_on_the_statement_s_own_scan_is_a_path():
    # The objects of a dataset can be few and come after its images: their index finds them, where the walk of the id
    # index would read every image first.
    kind = scan("((dataset_id = 18) AND (media_type = 'object'::text))")
    assert _plan_indexes_samples(
        with_children(kind, scan('(id = samples.parent_id)', **{'Parent Relationship': 'SubPlan'})),
    )
