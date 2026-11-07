"""detect_llama: process/unit detection driven by fake /proc trees."""
from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

_AGENT = Path(__file__).resolve().parent.parent
_FIX = Path(__file__).resolve().parent / "fixtures" / "detect_llama"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dl = _load("detect_llama", _AGENT / "install" / "detect_llama.py")


def _fx(name):
    return (_FIX / name).read_text()


def _proc(root, pid, args, cgroup="cgroup_session.txt", comm="llama-server"):
    d = Path(root) / str(pid)
    d.mkdir(parents=True)
    (d / "comm").write_text(comm + "\n")
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
    (d / "cgroup").write_text(_fx(cgroup))
    (d / "status").write_text("Name:\tllama-server\nUid:\t0\t0\t0\t0\n")


def _run(show="systemctl_show_llama.txt", units="", extra=""):
    def run(cmd, **kw):
        if cmd[:2] == ["systemctl", "show"]:
            out = _fx(show) + extra
        elif cmd[:2] == ["systemctl", "list-unit-files"]:
            out = units
        else:
            out = ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    return run


_UNITS = ("foo.service loaded active running Foo\n"
          "llama-server.service loaded inactive dead llama.cpp server\n"
          "llama_server.service loaded inactive dead llama.cpp server\n")


def test_cmdline_port_host_presets(tmp_path):
    _proc(tmp_path, 100, ["llama-server", "--host", "0.0.0.0", "--port", "9931",
                          "--models-preset", "/etc/llama/p.ini", "--metrics"])
    d = dl.detect(proc_root=str(tmp_path), run=_run())
    assert (d.port, d.host, d.presets_file, d.mode) == (9931, "0.0.0.0", "/etc/llama/p.ini", "router")


def test_single_mode_from_m_flag(tmp_path):
    _proc(tmp_path, 100, ["llama-server", "-m", "/models/a.gguf"])
    d = dl.detect(proc_root=str(tmp_path), run=_run())
    assert (d.mode, d.configured_by) == ("single", "flags")


def test_env_configured_single(tmp_path):
    _proc(tmp_path, 100, ["llama-server"], cgroup="cgroup_service.txt")
    d = dl.detect(proc_root=str(tmp_path), run=_run("systemctl_show_env_model.txt"))
    assert (d.mode, d.configured_by) == ("single", "environment")


def test_unit_from_cgroup(tmp_path):
    _proc(tmp_path, 100, ["llama-server", "--port", "9931"], cgroup="cgroup_service.txt")
    d = dl.detect(proc_root=str(tmp_path), run=_run())
    assert d.unit == "llama-server.service" and d.managed is True
    assert d.unit_path == "/usr/lib/systemd/system/llama-server.service"
    assert d.drop_ins == ["/etc/systemd/system/llama-server.service.d/50-llm-systems-agent.conf"]
    assert d.unit_user == "llama"


def test_no_unit_is_unmanaged(tmp_path):
    _proc(tmp_path, 100, ["llama-server"])
    d = dl.detect(proc_root=str(tmp_path), run=_run())
    assert d.unit == "" and d.managed is False and d.found is True


def test_several_servers_prefer_configured_port(tmp_path):
    _proc(tmp_path, 100, ["llama-server", "--port", "8080"], cgroup="cgroup_service.txt")
    _proc(tmp_path, 200, ["llama-server", "--port", "9931"])
    assert dl.detect(9931, proc_root=str(tmp_path), run=_run()).pid == 200
    assert dl.detect(None, proc_root=str(tmp_path), run=_run()).pid == 100
    assert dl.detect(None, proc_root=str(tmp_path), run=_run()).candidates == [100, 200]


def test_several_servers_none_with_unit_lowest_pid(tmp_path):
    _proc(tmp_path, 300, ["llama-server", "--port", "9931"])
    _proc(tmp_path, 200, ["llama-server", "--port", "8080"])
    assert dl.detect(None, proc_root=str(tmp_path), run=_run()).pid == 200


def test_flags_equals_and_quotes(tmp_path):
    _proc(tmp_path, 100, ["llama-server", "--models-preset=/opt/my models/p.ini", "--port=9931"])
    d = dl.detect(proc_root=str(tmp_path), run=_run())
    assert (d.presets_file, d.port) == ("/opt/my models/p.ini", 9931)


def test_nothing_running(tmp_path):
    _proc(tmp_path, 100, ["bash"], comm="bash")
    d = dl.detect(proc_root=str(tmp_path), run=_run())
    assert d.found is False and d.unit == "" and d.pid is None
    for k, v in vars(d).items():
        if isinstance(v, str):
            assert v == "", k


def test_sh_output_is_shell_safe(tmp_path, capsys, monkeypatch):
    path = "/opt/my models/it's.ini"
    _proc(tmp_path, 100, ["llama-server", "--models-preset", path])
    det = dl.detect(proc_root=str(tmp_path), run=_run())
    monkeypatch.setattr(dl, "detect", lambda port=None, **kw: det)
    assert dl.main(["--sh", "--port", ""]) == 0
    got = {}
    for line in capsys.readouterr().out.splitlines():
        k, _, v = line.partition("=")
        got[k] = shlex.split(v)[0] if shlex.split(v) else ""
    assert got["LLAMA_DETECTED_PRESETS_FILE"] == path
    assert got["LLAMA_DETECTED_FOUND"] == "true"
    assert "LLAMA_DETECTED_DROP_INS" not in got


def test_json_output(tmp_path, capsys, monkeypatch):
    _proc(tmp_path, 100, ["llama-server", "--port", "9931"])
    det = dl.detect(proc_root=str(tmp_path), run=_run())
    monkeypatch.setattr(dl, "detect", lambda port=None, **kw: det)
    assert dl.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["port"] == 9931


def test_parse_exec_flags_matches_llama_sse():
    sse = _load("llama_sse_sync", _AGENT / "providers" / "llama_sse.py")
    for line in ["llama-server --port 9931 -m x.gguf", "a --b=c 'd e' -f", "",
                 "x 'unterminated --y", "--models-preset \"/a b/p.ini\" --metrics"]:
        assert dl.parse_exec_flags(line) == sse.parse_exec_flags(line)


def test_user_manager_unit_is_unmanaged_and_user_blank(tmp_path):
    _proc(tmp_path, 100, ["llama-server", "--port", "9931"], cgroup="cgroup_user_unit.txt")
    d = dl.detect(proc_root=str(tmp_path), run=_run())
    assert d.found is True and d.unit == "" and d.managed is False
    assert d.process_user == "" and d.port == 9931


def test_unit_not_loaded_is_unmanaged(tmp_path):
    _proc(tmp_path, 100, ["llama-server"], cgroup="cgroup_service.txt")
    d = dl.detect(proc_root=str(tmp_path), run=_run("systemctl_show_not_found.txt"))
    assert d.unit == "" and d.managed is False and d.process_user == ""


def test_loaded_system_unit_reports_process_user(tmp_path):
    _proc(tmp_path, 100, ["llama-server"], cgroup="cgroup_service.txt")
    d = dl.detect(proc_root=str(tmp_path), run=_run())
    assert d.managed is True and d.process_user == "root"


def test_single_model_flags_match_agent_provider():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_llama_mode_switch import llama
    assert set(dl._SINGLE_MODEL_FLAGS) == set(llama._SINGLE_MODEL_FLAGS)


def test_stopped_unit_is_found(tmp_path):
    _proc(tmp_path, 100, ["bash"], comm="bash")
    d = dl.detect(proc_root=str(tmp_path), run=_run(units=_UNITS))
    assert (d.found, d.running, d.unit, d.port, d.process_user) == (
        True, False, "llama-server.service", 9931, "llama")
    assert d.pid is None and d.binary == "/usr/local/bin/llama-server" and d.managed is True


def test_no_process_no_llama_unit_not_found(tmp_path):
    d = dl.detect(proc_root=str(tmp_path), run=_run(units="foo.service loaded active running Foo\n"))
    assert d.found is False and d.running is False and d.unit == ""


def test_stopped_unit_not_loaded_is_skipped(tmp_path):
    d = dl.detect(proc_root=str(tmp_path), run=_run("systemctl_show_not_found.txt", units=_UNITS))
    assert d.found is False


def test_running_process_wins_over_units(tmp_path):
    _proc(tmp_path, 100, ["llama-server", "--port", "8080"], cgroup="cgroup_service.txt")
    d = dl.detect(proc_root=str(tmp_path), run=_run(units=_UNITS))
    assert (d.running, d.pid, d.port) == (True, 100, 8080)


def test_sh_output_has_running(tmp_path, capsys, monkeypatch):
    det = dl.detect(proc_root=str(tmp_path), run=_run(units=_UNITS))
    monkeypatch.setattr(dl, "detect", lambda port=None, **kw: det)
    assert dl.main(["--sh"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "LLAMA_DETECTED_RUNNING=false" in out and "LLAMA_DETECTED_FOUND=true" in out


def _env_show(tmp_path, env="", files="", argv="/usr/bin/llama-server"):
    f = tmp_path / "show.txt"
    f.write_text(f"Environment={env}\nEnvironmentFiles={files}\n"
                 f"ExecStart={{ path=/usr/bin/llama-server ; argv[]={argv} ; ignore_errors=no }}\n"
                 "User=llama\nLoadState=loaded\nFragmentPath=/etc/systemd/system/llama-server.service\n"
                 "DropInPaths=\n")
    return f


def _run_file(path):
    def run(cmd, **kw):
        out = path.read_text() if cmd[:2] == ["systemctl", "show"] else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    return run


def test_port_from_environment(tmp_path):
    show = _env_show(tmp_path, env="LLAMA_ARG_PORT=9000 LLAMA_ARG_HOST=127.0.0.1")
    _proc(tmp_path / "p", 100, ["llama-server"], cgroup="cgroup_service.txt")
    d = dl.detect(proc_root=str(tmp_path / "p"), run=_run_file(show))
    assert (d.port, d.host) == (9000, "127.0.0.1")


def test_presets_from_environment_file(tmp_path):
    ef = tmp_path / "llama.env"
    ef.write_text('# comment\n\nLLAMA_ARG_MODELS_PRESET="/etc/llama/p.ini"\n'
                  "LLAMA_ARG_MODELS_DIR='/srv/models'\n")
    show = _env_show(tmp_path, files=f"{ef} (ignore_errors=no)")
    _proc(tmp_path / "p", 100, ["llama-server"], cgroup="cgroup_service.txt")
    d = dl.detect(proc_root=str(tmp_path / "p"), run=_run_file(show))
    assert (d.presets_file, d.models_dir) == ("/etc/llama/p.ini", "/srv/models")


def test_environment_file_model_counts_as_single(tmp_path):
    ef = tmp_path / "llama.env"
    ef.write_text("LLAMA_ARG_MODEL=/m/a.gguf\n")
    show = _env_show(tmp_path, files=f"{ef} (ignore_errors=yes)")
    _proc(tmp_path / "p", 100, ["llama-server"], cgroup="cgroup_service.txt")
    d = dl.detect(proc_root=str(tmp_path / "p"), run=_run_file(show))
    assert (d.mode, d.configured_by) == ("single", "environment")


def test_argv_port_beats_environment(tmp_path):
    show = _env_show(tmp_path, env="LLAMA_ARG_PORT=9000")
    _proc(tmp_path / "p", 100, ["llama-server", "--port", "9931"], cgroup="cgroup_service.txt")
    d = dl.detect(proc_root=str(tmp_path / "p"), run=_run_file(show))
    assert d.port == 9931


def test_missing_environment_file_ignored(tmp_path):
    show = _env_show(tmp_path, env="LLAMA_ARG_PORT=9000",
                     files=f"{tmp_path / 'nope.env'} (ignore_errors=yes)")
    _proc(tmp_path / "p", 100, ["llama-server"], cgroup="cgroup_service.txt")
    d = dl.detect(proc_root=str(tmp_path / "p"), run=_run_file(show))
    assert d.port == 9000 and d.found is True


def test_quoted_environment_value(tmp_path):
    env = dl._unit_env({"Environment": 'LLAMA_ARG_MODELS_PRESET="/a b/p.ini" X=1'})
    assert env == {"LLAMA_ARG_MODELS_PRESET": "/a b/p.ini", "X": "1"}


def test_show_parser_joins_repeated_environment_files(tmp_path):
    f1 = tmp_path / "a.env"; f1.write_text("LLAMA_ARG_PORT=9000\n")
    f2 = tmp_path / "b.env"; f2.write_text("LLAMA_ARG_MODELS_PRESET=/etc/llama/p.ini\n")
    show = _fx("systemctl_show_llama.txt").replace(
        "ExecStart={ path=/usr/local/bin/llama-server ; argv[]=/usr/local/bin/llama-server --host 0.0.0.0 --port 9931 ; ignore_errors=no ; start_time=[n/a] }",
        "ExecStart={ path=/usr/local/bin/llama-server ; argv[]=/usr/local/bin/llama-server ; ignore_errors=no }"
    ) + f"EnvironmentFiles={f1} (ignore_errors=no)\nEnvironmentFiles={f2} (ignore_errors=no)\n"

    def run(cmd, **kw):
        out = show if cmd[:2] == ["systemctl", "show"] else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    _proc(tmp_path, 100, ["llama-server"], cgroup="cgroup_service.txt")
    d = dl.detect(proc_root=str(tmp_path), run=run)
    assert d.port == 9000 and d.presets_file == "/etc/llama/p.ini"


def test_stopped_scan_prefers_the_configured_unit(tmp_path):
    def run(cmd, **kw):
        if cmd[:2] == ["systemctl", "list-unit-files"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="llama-cpp.service enabled\nllama_server.service disabled\n", stderr="")
        if cmd[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=_fx("systemctl_show_llama.txt"), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    d = dl.detect(proc_root=str(tmp_path), run=run, cfg_unit="llama_server.service")
    assert d.found is True and d.unit == "llama_server.service" and d.running is False
    d2 = dl.detect(proc_root=str(tmp_path), run=run)
    assert d2.unit == "llama-cpp.service"


def test_custom_slice_system_unit_is_managed(tmp_path):
    (tmp_path / "cg.txt").write_text("0::/llama.slice/llama-server.service\n")
    assert dl._unit_from_cgroup("0::/llama.slice/llama-server.service\n") == "llama-server.service"
    assert dl._unit_from_cgroup("0::/user.slice/user-1000.slice/user@1000.service/app.slice/x.service\n") == ""
