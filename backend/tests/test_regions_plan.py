"""The pure helpers behind named regions (backend/regions.py, backend/filter_query.py): keys, slugs, what a boundary
file's features make, and which regions a query names. The loading, lookups and filter are tested against a database
elsewhere."""

import hashlib

import pytest

from backend.filter_query import canonical_region_key, region_names
from backend.regions import SETS, RegionLoadError, check_file, longitude_span, plan_regions, slug, summary


def feature(
    iso='US-KS',
    adm1='USA-3514',
    name='Kansas',
    iso_a2='US',
    adm0='USA',
    admin='United States',
    region=None,
    geonunit=None,
):
    return {
        'iso_3166_2': iso,
        'adm1_code': adm1,
        'name_en': name,
        'name': name,
        'iso_a2': iso_a2,
        'adm0_a3': adm0,
        'admin': admin,
        'geonunit': geonunit or admin,
        'region': region,
    }


def test_slug_folds_accents_and_runs_of_other_characters():
    assert slug('Midwest') == 'midwest'
    assert slug('Provence-Alpes-Côte-d\'Azur') == 'provence-alpes-cote-d-azur'
    assert slug('Đông Bắc') == 'dong-bac'
    assert slug('  Norte, Centro  ') == 'norte-centro'
    assert slug('Îles—du  Sud!') == 'iles-du-sud'  # an em dash separates like any other punctuation
    assert slug('---') == ''


def test_keys_are_canonical_whatever_their_case():
    assert canonical_region_key(' us-ks ') == 'US-KS'
    assert canonical_region_key('us') == 'US'
    assert canonical_region_key('us:Midwest') == 'US:midwest'
    assert canonical_region_key('ata+99?') == 'ATA+99?'


def test_a_state_is_keyed_by_its_iso_code_unless_another_feature_shares_it_or_it_is_malformed():
    plan = plan_regions(
        [
            feature(),
            feature(iso='PH-MNL', adm1='PHL-1', name='Manila', iso_a2='PH', adm0='PHL', admin='Philippines'),
            feature(iso='PH-MNL', adm1='PHL-2', name='Pasay', iso_a2='PH', adm0='PHL', admin='Philippines'),
            feature(iso='XK-X01~', adm1='KOS-1', name='Pristina', iso_a2='XK', adm0='KOS', admin='Kosovo'),
        ],
    )
    assert [state['key'] for state in plan.states] == ['US-KS', 'PHL-1', 'PHL-2', 'KOS-1']
    assert plan.report['iso_3166_2_duplicated'] == {'PH-MNL': 2}
    assert plan.report['iso_3166_2_malformed'] == 1
    assert plan.report['state_keys'] == {'iso_3166_2': 1, 'adm1_code': 3}


def test_a_country_is_keyed_by_iso_a2_else_adm0_a3_and_named_by_most_of_its_features():
    plan = plan_regions(
        [
            feature(iso='AU-NSW', adm1='AUS-1', iso_a2='AU', adm0='AUS', admin='Australia'),
            feature(iso='AU-VIC', adm1='AUS-2', iso_a2='AU', adm0='AUS', admin='Australia'),
            feature(iso='AU-X1~', adm1='ATC-1', iso_a2='AU', adm0='ATC', admin='Ashmore and Cartier Islands'),
            feature(iso='-99-X11~', adm1='SOL+00?', iso_a2='-1', adm0='SOL', admin='Somaliland'),
            feature(iso='PS-GZ', adm1='GAZ-1', iso_a2='PS', adm0='PSX', admin='Gaza Strip'),
            feature(iso='PS-WB', adm1='WEB-1', iso_a2='PS', adm0='PSX', admin='West Bank'),
        ],
    )
    assert plan.countries == {'AU': 'Australia', 'PS': 'Gaza Strip and West Bank', 'SOL': 'Somaliland'}
    assert plan.report['country_keys_from_adm0_a3'] == ['SOL']
    assert plan.report['countries_with_several_names']['AU'] == ['Ashmore and Cartier Islands', 'Australia']


def test_groups_come_from_the_region_property_within_a_country():
    plan = plan_regions(
        [
            feature(region='Midwest'),
            feature(iso='US-IA', adm1='USA-1', name='Iowa', region='Midwest'),
            feature(iso='US-CA', adm1='USA-2', name='California', region='West'),
            feature(iso='CA-BC', adm1='CAN-1', name='British Columbia', iso_a2='CA', adm0='CAN', region='West'),
            feature(iso='US-TX', adm1='USA-3', name='Texas', region=''),
        ],
    )
    assert plan.groups == {'CA:west': ('West', 'CA'), 'US:midwest': ('Midwest', 'US'), 'US:west': ('West', 'US')}
    assert [state['group_key'] for state in plan.states] == ['US:midwest', 'US:midwest', 'US:west', 'CA:west', None]
    assert plan.report['groups_by_country'] == {'CA': 1, 'US': 2}


def test_no_two_countries_share_a_name():
    nz = {'iso_a2': 'NZ', 'adm0': 'NZL', 'admin': 'New Zealand'}
    plan = plan_regions(
        [
            feature(iso='NZ-AUK', adm1='NZL-1', name='Auckland', **nz),
            feature(iso='NZ-CAN', adm1='NZL-2', name='Canterbury', **nz),
            feature(
                iso='TK-X1~',
                adm1='NZL-3',
                name='Tokelau Islands',
                iso_a2='TK',
                adm0='NZL',
                admin='New Zealand',
                geonunit='Tokelau',
            ),
            feature(iso='XA-1', adm1='XAA-1', name='One', iso_a2='XA', adm0='XAA', admin='Elsewhere'),
            feature(iso='XB-1', adm1='XBB-1', name='Lone Isle', iso_a2='XB', adm0='XBB', admin='Elsewhere'),
        ],
    )
    # The one with the most features keeps the name; the other takes its geonunit, else its single feature's name.
    assert plan.countries == {'NZ': 'New Zealand', 'TK': 'Tokelau', 'XA': 'Elsewhere', 'XB': 'Lone Isle'}
    assert plan.report['countries_renamed']['TK'] == {
        'name': 'Tokelau',
        'instead_of': 'New Zealand',
        'admin': ['New Zealand'],
        'geonunit': ['Tokelau'],
        'feature_names': ['Tokelau Islands'],
    }
    assert set(plan.report['countries_renamed']) == {'TK', 'XB'}


def test_a_damaged_group_name_makes_no_group():
    plan = plan_regions(
        [
            feature(iso='VN-01', adm1='VNM-1', name='A', iso_a2='VN', adm0='VNM', region='Đông Bắc'),
            feature(iso='VN-02', adm1='VNM-2', name='B', iso_a2='VN', adm0='VNM', region='Ðông B?c'),
            feature(iso='VN-03', adm1='VNM-3', name='C', iso_a2='VN', adm0='VNM', region='Ta\ufffdy'),
        ],
    )
    assert plan.groups == {'VN:dong-bac': ('Đông Bắc', 'VN')}
    assert plan.report['group_names_damaged'] == {'VN-02': 'Ðông B?c', 'VN-03': 'Ta\ufffdy'}


def test_what_a_feature_lacks_is_reported():
    plan = plan_regions(
        [
            None,  # no geometry
            feature(iso_a2=None, adm0=None),  # no country
            feature(iso='AQ-X02~', adm1='ATA+99?', name=None, iso_a2='AQ', adm0='ATA', admin='Antarctica'),
        ],
    )
    assert plan.report['no_geometry'] == [0] and plan.report['no_country'] == [1]
    assert plan.report['unnamed_states'] == ['ATA+99?'] and plan.states[0]['name'] == 'ATA+99?'
    assert plan.report['counts'] == {'country': 1, 'state': 1, 'group': 0}


def test_features_sharing_a_key_after_the_fallback_are_merged_not_dropped():
    plan = plan_regions(
        [
            feature(iso='XX-X1~', adm1='DUP-1', name='East half'),
            feature(iso='XX-X2~', adm1='DUP-1', name='West half'),
            feature(iso=None, adm1=None, name='Nameless'),
        ],
    )
    assert [state['key'] for state in plan.states] == ['DUP-1']
    assert plan.states[0]['index'] == 0 and plan.states[0]['merged'] == [1]
    assert plan.report['keys_merged'] == {'DUP-1': ['East half', 'West half']}
    assert plan.report['no_key'] == [2]


def test_a_box_is_the_narrowest_way_round_the_globe_from_each_polygons_longitudes():
    assert longitude_span([(-102.05, -94.6)]) == (-102.05, -94.6)
    # Russia: Chukotka across 180, the rest from Kaliningrad: across the antimeridian, west > east.
    assert longitude_span([(-180.0, -169.0), (19.6, 180.0), (27.0, 60.0)]) == (19.6, -169.0)
    # The United States: the widest stretch with no land is between Maine and the Aleutians.
    assert longitude_span([(-180.0, -129.9), (-124.7, -66.9), (172.4, 180.0), (-178.3, -154.8)]) == (172.4, -66.9)
    # Antarctica covers every longitude: no stretch is left, so the whole circle, not a box missing 0.
    assert longitude_span([(-180.0, 180.0), (-61.0, -54.0)]) == (-180.0, 180.0)
    # A sparse band wider than 180 degrees that doesn't cross: its own extent, not its complement.
    assert longitude_span([(-170.0, 170.0)]) == (-170.0, 170.0)
    # Fiji-like: two polygons either side of 180.
    assert longitude_span([(170.0, 175.0), (-175.0, -170.0)]) == (170.0, -170.0)


def test_the_file_check_takes_the_pinned_file_or_one_vouched_for():
    natural_earth = SETS['natural-earth-admin1']
    data = b'{"type": "FeatureCollection", "features": []}'
    with pytest.raises(RegionLoadError, match='SHA-256'):
        check_file(data, natural_earth)
    assert check_file(data, natural_earth, verify=False) == hashlib.sha256(data).hexdigest()
    small = type(natural_earth)(**{**natural_earth.__dict__, 'max_bytes': 10})
    with pytest.raises(RegionLoadError, match='over the limit'):
        check_file(data, small, verify=False)


def test_region_names_finds_what_follows_within():
    query = 'location within "US-KS" AND split = within AND (location not within Georgia OR within = 1)'
    assert region_names(query) == ['US-KS', 'Georgia']
    assert region_names('location within') == []
    assert region_names('location within "unclosed') == []


def test_the_summary_counts_the_keys_a_reload_removed():
    report = {'counts': {'country': 1, 'state': 2, 'group': 0}, 'version': '5.1.1', 'removed_keys': ['US-KS']}
    assert summary(report) == (
        '1 country, 2 states or provinces and 0 groups loaded (5.1.1); 1 key of the earlier load is gone'
    )
    assert summary({**report, 'removed_keys': ['A', 'B']}).endswith('; 2 keys of the earlier load are gone')
    assert summary({**report, 'removed_keys': []}).endswith('loaded (5.1.1)')
