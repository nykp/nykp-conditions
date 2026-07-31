import pytest

import post_conditions as pc


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
    monkeypatch.setattr(pc, 'post_currents', lambda channel, days: (_ for _ in ()).throw(RuntimeError('down')))
    monkeypatch.setattr(pc, 'post_waterbody_advisories', lambda channel, days: None)
    monkeypatch.setattr(pc, 'post_observed_precip', lambda channel: None)
    monkeypatch.setattr(pc, 'post_water_temps', lambda channel, plot: None)
    monkeypatch.setattr(pc, 'post_forecast', lambda channel: None)

    args = pc.parse_args().parse_args(['--channel', 'test-channel'])
    with pytest.raises(RuntimeError, match='Failed to post 1 of 5 sections: currents'):
        pc.main(args)


def test_main_succeeds_when_all_sections_post(monkeypatch):
    posted = []
    monkeypatch.setattr(pc, 'post_currents', lambda channel, days: posted.append('currents'))
    monkeypatch.setattr(pc, 'post_waterbody_advisories', lambda channel, days: posted.append('advisories'))
    monkeypatch.setattr(pc, 'post_observed_precip', lambda channel: posted.append('precip'))
    monkeypatch.setattr(pc, 'post_water_temps', lambda channel, plot: posted.append('water-temp'))
    monkeypatch.setattr(pc, 'post_forecast', lambda channel: posted.append('forecast'))

    args = pc.parse_args().parse_args(['--channel', 'test-channel'])
    pc.main(args)
    # Compared order-agnostically: which sections run is the contract here,
    # while the order they're posted in is a presentation choice that changes.
    # `post_sections` running its list in order is covered separately above.
    assert sorted(posted) == sorted(['currents', 'advisories', 'precip', 'water-temp', 'forecast'])


def test_sections_can_be_disabled(monkeypatch):
    posted = []
    monkeypatch.setattr(pc, 'post_currents', lambda channel, days: posted.append('currents'))
    monkeypatch.setattr(pc, 'post_waterbody_advisories', lambda channel, days: posted.append('advisories'))
    monkeypatch.setattr(pc, 'post_observed_precip', lambda channel: posted.append('precip'))
    monkeypatch.setattr(pc, 'post_water_temps', lambda channel, plot: posted.append('water-temp'))
    monkeypatch.setattr(pc, 'post_forecast', lambda channel: posted.append('forecast'))

    args = pc.parse_args().parse_args(['--currents', 'false', '--forecast', 'no', '--precip', '0'])
    pc.main(args)
    assert sorted(posted) == sorted(['advisories', 'water-temp'])


@pytest.mark.parametrize('argv, expected', [([], False), (['--water-temp-plot', 'true'], True)])
def test_water_temp_plot_is_opt_in(monkeypatch, argv, expected):
    plotted = []
    monkeypatch.setattr(pc, 'post_water_temps', lambda channel, plot: plotted.append(plot))
    for name in ('post_currents', 'post_waterbody_advisories', 'post_observed_precip', 'post_forecast'):
        monkeypatch.setattr(pc, name, lambda *a, **kw: None)

    pc.main(pc.parse_args().parse_args(argv))
    assert plotted == [expected]
