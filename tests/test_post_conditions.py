import pytest

import nykp_conditions.post_conditions as pc

# Every function `main` calls to post a section, and the name it records.
# Anything left unstubbed here would reach the real upstream sources and post
# to a real Slack channel, so the fixture below stubs all of them by default
# and tests opt into replacing the one they care about.
SECTION_POSTERS = {
    'post_currents': 'currents',
    'post_water_temps': 'water-temp',
    'post_forecast': 'forecast',
    'post_observed_precip': 'precip',
    'post_waterbody_advisories': 'advisories',
    'post_waterbody_advisory': 'DEP advisory',
}


@pytest.fixture(autouse=True)
def posted(monkeypatch):
    """Record which sections posted, without any of them doing real work."""
    recorded = []

    def _recorder(name):
        def post(channel, **kwargs):
            recorded.append(name)
        return post

    for attr, name in SECTION_POSTERS.items():
        monkeypatch.setattr(pc, attr, _recorder(name))
    return recorded


ALL_SECTIONS = sorted(SECTION_POSTERS.values())


def test_post_sections_runs_everything_in_order():
    calls = []
    failed = pc.post_sections([
        ('a', lambda: calls.append('a')),
        ('b', lambda: calls.append('b')),
    ])
    assert calls == ['a', 'b']
    assert failed == []


def test_post_sections_continues_after_a_failure(capsys):
    """One dead upstream source shouldn't cost us the rest of the post."""
    calls = []

    def boom():
        raise RuntimeError('NOAA is down')

    failed = pc.post_sections([
        ('currents', boom),
        ('forecast', lambda: calls.append('forecast')),
    ])

    assert calls == ['forecast']  # Later sections still posted
    assert failed == ['currents']
    assert 'NOAA is down' in capsys.readouterr().err


def test_post_sections_reports_every_failure():
    def boom():
        raise RuntimeError('nope')

    failed = pc.post_sections([('a', boom), ('b', boom), ('c', lambda: None)])
    assert failed == ['a', 'b']


def test_main_raises_when_a_section_fails(monkeypatch):
    """The workflow run should still go red even though we posted what we could."""
    def boom(channel, **kwargs):
        raise RuntimeError('down')

    monkeypatch.setattr(pc, 'post_currents', boom)

    args = pc.parse_args().parse_args(['--channel', 'test-channel'])
    expected = f'Failed to post 1 of {len(ALL_SECTIONS)} sections: currents'
    with pytest.raises(RuntimeError, match=expected):
        pc.main(args)


def test_main_succeeds_when_all_sections_post(posted):
    args = pc.parse_args().parse_args(['--channel', 'test-channel'])
    pc.main(args)
    # Compared order-agnostically: which sections run is the contract here,
    # while the order they're posted in is a presentation choice that changes.
    # `post_sections` running its list in order is covered separately above.
    assert sorted(posted) == ALL_SECTIONS


def test_sections_can_be_disabled(posted):
    args = pc.parse_args().parse_args(
        ['--currents', 'false', '--forecast', 'no', '--precip', '0', '--dep-advisory', 'false']
    )
    pc.main(args)
    assert sorted(posted) == sorted(['advisories', 'water-temp'])


@pytest.mark.parametrize('argv, expected', [([], False), (['--water-temp-plot', 'true'], True)])
def test_water_temp_plot_is_opt_in(monkeypatch, argv, expected):
    plotted = []
    monkeypatch.setattr(pc, 'post_water_temps', lambda channel, plot: plotted.append(plot))

    pc.main(pc.parse_args().parse_args(argv))
    assert plotted == [expected]
