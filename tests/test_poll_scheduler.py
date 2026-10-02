"""PollScheduler retries a failed coordinator on the next tick.

Runs without Home Assistant installed: the HA and aiopowerwall imports are
stubbed so the real coordinator.py PollScheduler is exercised.
    python3 tests/test_poll_scheduler.py
Regression (v0.8.4): a config poll that failed during a Wi-Fi blip left the
backup-reserve number unavailable for the full 1800 s interval.
"""
import sys, types, asyncio, importlib.util, pathlib
def mod(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m
class _Any:
    def __init__(self,*a,**k): pass
    def __class_getitem__(cls, item): return cls
mod("aiohttp", ClientSession=_Any)
class PE(Exception): pass
mod("aiopowerwall", BackupEventsPayload=dict, PowerwallAuthenticationError=type("A",(PE,),{}),
    PowerwallClient=_Any, PowerwallConnectionError=type("C",(PE,),{}), PowerwallError=PE)
for n in ["homeassistant","homeassistant.helpers"]: mod(n)
mod("homeassistant.config_entries", ConfigEntry=_Any)
mod("homeassistant.core", CALLBACK_TYPE=object, HomeAssistant=_Any, callback=lambda f: f)
class AuthFailed(Exception): pass
mod("homeassistant.exceptions", ConfigEntryAuthFailed=AuthFailed)
mod("homeassistant.helpers.event", async_track_time_interval=lambda *a, **k: (lambda: None))
class DUC:
    def __init__(self,*a,**k): pass
    def __class_getitem__(cls, item): return cls
mod("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=DUC, UpdateFailed=Exception)
root = pathlib.Path(__file__).resolve().parents[1]/"custom_components/powerwall_fleet"
pkg = mod("pwf"); pkg.__path__ = [str(root)]
for name in ("const","coordinator"):
    spec = importlib.util.spec_from_file_location(f"pwf.{name}", root/f"{name}.py")
    m = importlib.util.module_from_spec(spec); sys.modules[f"pwf.{name}"] = m; spec.loader.exec_module(m)
C = sys.modules["pwf.coordinator"]

class FakeCoord:
    def __init__(self, label, interval, fail_ticks=()):
        self._label, self.poll_interval = label, interval
        self.fail_ticks, self.calls, self.last_update_success, self.last_exception = set(fail_ticks), [], True, None
        self._listeners = {1: 1}
    async def async_refresh(self):
        self.calls.append(sched._tick)
        self.last_update_success = sched._tick not in self.fail_ticks

class FakeHass:
    def async_create_task(self, coro): return asyncio.ensure_future(coro)
class Entry: pref_disable_polling = False

async def main():
    global sched
    soe = FakeCoord("soe", 30)
    cfg = FakeCoord("config", 1800, fail_ticks={60})   # explicit 1800 s here; tick 60 = 30 min: the blip
    sched = C.PollScheduler(FakeHass(), Entry(), (soe, cfg))
    for _ in range(125):
        await sched._async_tick(None)
    return cfg.calls
calls = asyncio.run(main())
print("config polled at ticks:", calls)
assert calls[:3] == [60, 61, 120], calls      # failed at 60 -> retried at 61 (30 s later), then normal cadence
print("OK: failed config poll retried on the next tick, then back to every 60 ticks")


assert sys.modules["pwf.const"].SCAN_CONFIG_SECONDS == 300
print("OK: config polled every 300 s")
