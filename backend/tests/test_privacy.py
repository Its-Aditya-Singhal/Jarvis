"""Phase 9: preferences, performance modes, privacy actions, offline guard, health."""

import os
import socket
import stat

import numpy as np
import pytest
from test_command_service import make

from jarvis.health import issues
from jarvis.netguard import NetGuard, is_local
from jarvis.perf import MODES, PerfMonitor, effective_mode
from jarvis.prefs import PREFS, Prefs, coerce
from jarvis.privacy import export_data, validate_export_path


# -- preferences ----------------------------------------------------------------------
def test_prefs_are_fixed_choices(settings):
    svc, *_ = make(settings, [])
    p = svc.prefs
    assert p.get("voice.speed") == 1.0 and p.get("perf.mode") == "auto" and p.get("privacy.offline") is True
    assert p.set("security.away_lock_s", 20) == 20.0  # JSON ints map onto the float choice
    with pytest.raises(ValueError):
        p.set("voice.speed", 3.0)
    with pytest.raises(ValueError):
        coerce(PREFS["privacy.offline"], 1)  # 1 is not True here
    svc.db.set("pref.voice.speed", "9000")  # tampered -> default
    assert p.get("voice.speed") == 1.0


def test_loosening_vs_tightening(settings):
    p = Prefs(make(settings, [])[2])
    assert not p.loosens("security.face", "strict")
    p.set("security.face", "strict")
    assert p.loosens("security.face", "standard")
    assert p.loosens("security.away_lock_s", 60.0) and not p.loosens("security.away_lock_s", 8.0)
    assert p.loosens("privacy.offline", False)  # allowing the network widens exposure
    assert not p.loosens("voice.speed", 1.2)  # not a security setting


def test_security_presets_reach_the_live_auth_objects(settings):
    svc, *_ = make(settings, [])
    svc.set_pref("security.face", "strict")
    svc.set_pref("security.away_lock_s", 8.0)
    svc.set_pref("security.liveness", "frequent")
    assert svc.auth.threshold == 0.50 and svc.auth.reject_threshold == 0.30
    assert svc.live.cfg.rechallenge_max_s == 300.0
    assert svc.db.security_events()[0]["kind"] == "settings_changed"


def test_loosening_needs_a_confirmation(settings):
    svc, _, _, state = make(settings, [])
    svc.set_pref("security.face", "strict")
    r = svc.request_pref("security.face", "standard")
    assert r["ok"] and svc.pending() is not None and svc.prefs.get("security.face") == "strict"
    state["l3"] = False  # stale liveness: refused until a fresh check
    assert not svc.confirm(r["pending"], True, "click")["ok"]
    state["l3"] = True
    assert svc.confirm(r["pending"], True, "click")["ok"]
    assert svc.prefs.get("security.face") == "standard" and svc.auth.threshold == 0.42


# -- privacy actions ------------------------------------------------------------------
def _enrolled(svc):
    svc.store.save("face", np.eye(4, 512, dtype=np.float32))
    svc.db.set("setup_complete", "1")
    svc.db.set("owner_name", "Aditya")
    svc.begin_verification()


def test_delete_face_opens_a_rescan_window_then_needs_the_voice(settings):
    svc, *_ = make(settings, [])
    _enrolled(svc)
    assert svc.face_enroll_allowed()[0] is False  # a re-scan needs a confirmation
    r = svc.request_privacy("delete_face")
    assert svc.confirm(r["pending"], True, "click")["ok"]
    assert not svc.face_enrolled and svc.mode == "idle" and svc.face_enroll_allowed()[0]
    assert svc.status()["face_reenroll"]["kind"] == "deleted"
    now = svc.clock()
    svc.clock = lambda: now + 601  # the window closes; without a voice profile only a reset helps
    ok, why = svc.face_enroll_allowed()
    assert not ok and "Factory reset" in why
    assert svc.locked_out()  # nobody can verify any more: a reset without verification is allowed
    svc._factory_reset()
    assert not svc.setup_complete and not svc.locked_out()


def test_not_locked_out_while_a_profile_remains(settings):
    svc, *_ = make(settings, [])
    _enrolled(svc)
    assert not svc.locked_out()


def test_rescan_keeps_the_old_profile_until_the_new_one_is_saved(settings):
    svc, *_ = make(settings, [])
    _enrolled(svc)
    r = svc.request_privacy("reenroll_face")
    svc.confirm(r["pending"], True, "click")
    assert svc.face_enrolled and svc.mode == "verifying" and svc.face_enroll_allowed() == (True, "re-scan authorised (redo)")


def test_clear_tools_and_security_log(settings):
    svc, tstore, db, _ = make(settings, [])
    tstore.add_note("buy milk")
    db.add_security_event("unknown_face", "x")
    svc.confirm(svc.request_privacy("clear_tools")["pending"], True, "click")
    assert tstore.notes() == []
    svc.confirm(svc.request_privacy("clear_security_log")["pending"], True, "click")
    # only the record that the log was cleared remains
    assert [e["kind"] for e in db.security_events()] == ["privacy_action"]


def test_one_confirmation_at_a_time(settings):
    svc, *_ = make(settings, [])
    assert svc.request_privacy("clear_tools")["ok"]
    assert not svc.request_privacy("clear_security_log")["ok"]


def test_factory_reset_erases_everything_and_the_key(settings):
    svc, tstore, db, _ = make(settings, [])
    _enrolled(svc)
    svc.store.save("voice", np.eye(3, 192, dtype=np.float32))
    tstore.add_note("secret note")
    svc.prefs.set("voice.speed", 1.2)
    key = svc.store.keys.get_key()
    svc.confirm(svc.request_privacy("factory_reset")["pending"], True, "click")
    assert not svc.setup_complete and svc.owner_name == "" and svc.mode == "idle"
    assert not any(settings.templates_dir.glob("*")) and tstore.notes() == []
    assert svc.prefs.get("voice.speed") == 1.0 and svc.store.keys.get_key() != key
    assert [e["kind"] for e in db.security_events()] == ["factory_reset"]
    assert svc.face_enroll_allowed()[0]  # setup is open again


def test_export_is_readable_owner_only_and_has_no_templates(settings, tmp_path):
    svc, tstore, _, _ = make(settings, [])
    _enrolled(svc)
    tstore.add_note("call the bank")
    with pytest.raises(ValueError):
        validate_export_path("relative.json", settings.data_dir)
    with pytest.raises(ValueError):
        validate_export_path(str(settings.data_dir / "x.json"), settings.data_dir)
    out = validate_export_path(str(tmp_path / "mine.txt"), settings.data_dir)
    assert out.suffix == ".json"
    svc.confirm(svc.request_privacy("export", path=str(out))["pending"], True, "click")
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    text = out.read_text()
    assert "call the bank" in text and "Aditya" in text and "embedding" not in text
    assert "face" not in export_data(svc)  # templates are never exported


# -- performance modes -------------------------------------------------------------------
def test_auto_mode_follows_the_power_source():
    assert effective_mode("auto", True) == "fast" and effective_mode("auto", False) == "balanced"
    assert effective_mode("quality", True) == "quality"
    assert MODES["fast"].llm == "fast" and not MODES["fast"].suggestions


def test_power_change_switches_mode(settings):
    svc, *_ = make(settings, [])
    svc.brain.override, svc.brain.installed = None, ["qwen2.5:7b", "qwen2.5:3b"]
    svc.brain.installed_models = lambda: svc.brain.installed
    svc.perf.on_battery = True
    svc._power_changed(True)
    assert svc._mode == "fast" and svc.s.process_fps == 4.0 and svc.brain.override == "qwen2.5:3b"
    assert not svc.suggestions_on()
    svc.perf.on_battery = False
    svc._power_changed(False)
    assert svc._mode == "balanced" and svc.brain.override is None and svc.suggestions_on()


def test_fast_mode_without_the_small_model_says_so(settings):
    svc, *_ = make(settings, [])
    svc.brain.override, svc.brain.installed = None, ["qwen2.5:7b"]
    svc.brain.installed_models = lambda: svc.brain.installed
    svc.set_pref("perf.mode", "fast")
    assert svc.brain.override is None and "ollama pull qwen2.5:3b" in svc.mode_info()["note"]


def test_perf_monitor_reports_power_changes():
    seen = []
    readings = iter([(False, 80)] + [(True, 55)] * 1000)  # plugged in at start, then unplugged
    m = PerfMonitor(seen.append, power=lambda: next(readings), period_s=0.01)
    m.start()
    import time
    time.sleep(0.2)
    m.stop()
    assert seen == [True] and m.battery_pct == 55 and "cpu" in m.stats


# -- offline guard ----------------------------------------------------------------------
def test_offline_guard_blocks_the_internet_but_not_loopback():
    assert is_local("127.0.0.1") and is_local("::1") and is_local("localhost") and not is_local("8.8.8.8")
    offline = {"on": True}
    g = NetGuard(offline=lambda: offline["on"])
    g.install()
    try:
        socket.getaddrinfo("localhost", 80)  # allowed, not recorded
        with pytest.raises(OSError, match="offline mode"):
            socket.getaddrinfo("example.com", 443)
        s = socket.socket()
        with pytest.raises(OSError, match="offline mode"):
            s.connect(("93.184.215.14", 80))
        s.close()
        offline["on"] = False
        g.check("example.com", 443, "dns")  # allowed but still recorded
    finally:
        g.uninstall()
    hosts = [(a["host"], a["blocked"]) for a in g.recent()]
    assert hosts == [("example.com", False), ("93.184.215.14", True), ("example.com", True)]


# -- health ------------------------------------------------------------------------------
def test_issues_come_with_a_fix():
    st = {"camera": {"status": "error"}, "mic": {"status": "active"},
          "models": {"face": "ready", "llm": "model qwen2.5:7b not installed — run: ollama pull qwen2.5:7b",
                     "memory": "word match only — model bge-m3 is not installed", "stt": "ready", "tts": "ready",
                     "liveness": "challenges only — passive model not loaded"}}
    got = {i["id"]: i for i in issues(st)}
    assert got["camera"]["level"] == "error" and "Privacy & Security" in got["camera"]["fix"]
    assert got["llm"]["fix"] == "Run: ollama pull qwen2.5:7b"
    assert "bge-m3" in got["memory"]["fix"] and got["model_liveness"]["level"] == "warn"
    down = issues({"models": {"llm": "Ollama not running"}})
    assert "start.sh" in down[0]["fix"]
    assert issues({"models": {"face": "ready", "llm": "ready"}}) == []


def test_verified_voice_unlocks_a_face_rescan_after_the_window(settings):
    from types import SimpleNamespace

    from jarvis.auth.voice.verification import VoiceAuth

    svc, *_ = make(settings, [])
    _enrolled(svc)
    svc.confirm(svc.request_privacy("delete_face")["pending"], True, "click")
    now = svc.clock()
    svc.clock = lambda: now + 700  # re-scan window over
    auth = VoiceAuth(0.5, 0.3)
    svc.voice = SimpleNamespace(mode="verifying", enrolled=True, available=True, auth=auth)
    ok, why = svc.face_enroll_allowed()
    assert not ok and "it's me" in why and not svc.locked_out()
    assert "voice first" in svc._unverified_reply("uncertain")
    auth.judge(0.8, 0.9, now + 690)  # the owner's voice, 10 s ago
    assert svc.face_enroll_allowed() == (True, "voice verified")
    assert "scan your face" in svc._unverified_reply("verified")
    auth.judge(0.1, 0.9, now + 695)  # then a stranger spoke
    assert not svc.face_enroll_allowed()[0]


def test_quality_mode_without_whisper_medium_keeps_small(settings, tmp_path):
    from types import SimpleNamespace

    from jarvis.speech.stt import SpeechToText

    settings.models_dir = tmp_path  # no medium weights here
    svc, *_ = make(settings, [])
    small = SpeechToText(tmp_path, "small")
    svc.speech = SimpleNamespace(stt=small)
    svc._swap_stt("medium")
    assert svc.speech.stt is small and "not downloaded" in svc._mode_note
