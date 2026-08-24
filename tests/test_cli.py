import pytest

import nykp_conditions.cli as cli


def test_every_command_maps_to_a_runnable_module():
    """A subcommand is only useful if the module behind it has the CLI pair the
    dispatcher calls. This catches a module renamed out from under the table."""
    for name, (module, description) in cli.COMMANDS.items():
        assert callable(module.parse_args), name
        assert callable(module.main), name
        assert description, name


def test_no_args_prints_usage(capsys):
    assert cli.main([]) == 0
    assert 'usage: nykp-conditions' in capsys.readouterr().out


def test_help_lists_every_command(capsys):
    assert cli.main(['--help']) == 0
    out = capsys.readouterr().out
    for name in cli.COMMANDS:
        assert name in out


def test_unknown_command_is_an_error(capsys):
    assert cli.main(['bogus']) == 2
    err = capsys.readouterr().err
    assert "unknown command 'bogus'" in err
    assert 'usage:' in err  # Tell them what they could have typed


def test_command_receives_the_remaining_arguments(monkeypatch):
    """The dispatcher must not consume flags meant for the subcommand."""
    seen = {}

    def fake_main(args):
        seen['out'] = args.out
        seen['days'] = args.days

    monkeypatch.setattr(cli.web_export, 'main', fake_main)
    cli.main(['web-export', '--out', 'somewhere.json', '--days', '3'])

    assert seen == {'out': 'somewhere.json', 'days': 3}


def test_command_help_names_itself(capsys):
    """`nykp-conditions post --help` should say so, not 'post_conditions.py'."""
    with pytest.raises(SystemExit):
        cli.main(['post', '--help'])
    assert 'nykp-conditions post' in capsys.readouterr().out


def test_dispatcher_does_not_leak_state_between_commands(monkeypatch):
    """Each call builds a fresh parser. Reusing one would make try_main add
    --pdb/--postmortem twice and raise on the second run."""
    monkeypatch.setattr(cli.web_export, 'main', lambda args: None)
    assert cli.main(['web-export', '--out', 'a.json']) == 0
    assert cli.main(['web-export', '--out', 'b.json']) == 0


def test_subcommand_names_are_command_line_friendly():
    """Underscores in a command name are a typo waiting to happen next to the
    hyphenated ones."""
    for name in cli.COMMANDS:
        assert '_' not in name, name
        assert name == name.lower(), name
