from jarvis import hardware
from jarvis.hardware import SMALL, STANDARD, profile_for


def test_small_macs_get_the_small_profile():
    assert profile_for(8) is SMALL and profile_for(7.9) is SMALL
    assert profile_for(16) is STANDARD and profile_for(36) is STANDARD
    assert SMALL.llm_model == "qwen2.5:3b" and not SMALL.stt_medium
    assert SMALL.num_ctx < STANDARD.num_ctx and SMALL.threads < STANDARD.threads


def test_ram_can_be_overridden_for_testing(monkeypatch):
    monkeypatch.setenv("JARVIS_RAM_GB", "8")
    hardware.profile.cache_clear()
    try:
        assert hardware.profile() is SMALL
        from jarvis.config import Settings

        assert Settings().llm_model == "qwen2.5:3b"
    finally:
        hardware.profile.cache_clear()


def test_a_bad_ram_override_falls_back_to_the_real_size(monkeypatch):
    for bad in ("eight", "", "0", "-4"):
        monkeypatch.setenv("JARVIS_RAM_GB", bad)
        assert hardware.ram_gb() > 0
    monkeypatch.setenv("JARVIS_RAM_GB", "8")
    assert hardware.ram_gb() == 8.0
