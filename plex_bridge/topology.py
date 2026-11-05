#!/usr/bin/env python3
"""Local, state-aware coordinator for WiiM preset group actions."""

from __future__ import annotations
from plex_bridge.config import load_config, load_or_create_identity, ConfigError
from plex_bridge.provisioning import credential_settings

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


from plex_bridge.diagnostics import SafeServerErrors, configure, request_event

LOGGER = logging.getLogger("wiim-group-control")
ACTIVE_STATES = {"play", "loading"}


class WiiMError(RuntimeError):
    pass


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    host: str
    auto_join_target: bool = True


@dataclass
class DeviceState:
    device: Device
    online: bool
    playback: str = "unknown"
    role: str = "unknown"
    group_name: str = ""
    master_host: str = ""
    model: str = ""
    firmware: str = ""
    volume: str = ""
    error: str = ""

    @property
    def active(self) -> bool:
        return self.online and self.playback in ACTIVE_STATES


def load_devices(path: str) -> list[Device]:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, list) or not raw:
        raise ValueError("devices configuration must be a non-empty list")
    devices = []
    ids = set()
    hosts = set()
    for item in raw:
        device = Device(
            str(item["id"]),
            str(item["name"]),
            str(item["host"]),
            bool(item.get("auto_join_target", True)),
        )
        if device.id in ids or device.host in hosts:
            raise ValueError("device ids and hosts must be unique")
        ids.add(device.id)
        hosts.add(device.host)
        devices.append(device)
    return devices


class WiiMClient:
    def __init__(self, timeout: float = 3.0):
        self.timeout = timeout
        self.ssl_context = ssl._create_unverified_context()

    def command(self, device: Device, command: str):
        encoded = urllib.parse.quote(command, safe=":,.[]{}")
        url = f"https://{device.host}/httpapi.asp?command={encoded}"
        try:
            with urllib.request.urlopen(
                url, timeout=self.timeout, context=self.ssl_context
            ) as response:
                body = response.read(1024 * 1024).decode("utf-8", errors="replace")
        except (OSError, urllib.error.URLError) as exc:
            raise WiiMError(str(exc)) from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body.strip()

    def state(self, device: Device) -> DeviceState:
        try:
            info = self.command(device, "getStatusEx")
            player = self.command(device, "getPlayerStatus")
            if not isinstance(info, dict) or not isinstance(player, dict):
                raise WiiMError("unexpected status response")
            follower = str(info.get("group", "")) == "1" or str(
                player.get("type", "")
            ) == "1"
            return DeviceState(
                device=device,
                online=True,
                playback=str(player.get("status", "unknown")),
                role="follower" if follower else "standalone/leader",
                group_name=str(info.get("GroupName", "")),
                master_host=str(info.get("master_ip", "")),
                model=str(info.get("project", "")),
                firmware=str(info.get("firmware", "")),
                volume=str(player.get("vol", "")),
            )
        except WiiMError as exc:
            return DeviceState(device=device, online=False, error=str(exc))

    def join(self, requester: Device, leader: Device):
        return self.command(
            requester,
            f"ConnectMasterAp:JoinGroupMaster:eth{leader.host}",
        )

    def leave_follower(self, requester: Device):
        return self.command(requester, "ConnectMasterAp:JoinGroupMaster:eth0")

    def kick_follower(self, leader: Device, follower: Device):
        return self.command(
            leader,
            f"multiroom:SlaveKickout:{follower.host}",
        )

    def follower_hosts(self, leader: Device) -> set[str]:
        payload = self.command(leader, "multiroom:getSlaveList")
        if not isinstance(payload, dict):
            return set()
        followers = payload.get("slave_list")
        if not isinstance(followers, list):
            return set()
        return {
            str(item.get("ip"))
            for item in followers
            if isinstance(item, dict) and item.get("ip")
        }

    def dissolve(self, leader: Device):
        return self.command(leader, "multiroom:Ungroup")

    def shutdown(self, device: Device):
        return self.command(device, "setShutdown:0")

    def trigger_off(self, device: Device):
        return self.command(device, "setTriggeroutStatus:0")

    def trigger_status(self, device: Device) -> int | None:
        payload = self.command(device, "getTriggeroutStatus")
        if not isinstance(payload, dict):
            return None
        try:
            status = int(payload.get("status"))
        except (TypeError, ValueError):
            return None
        return status if status in {0, 1} else None

    def resume(self, device: Device):
        return self.command(device, "setPlayerCmd:resume")

    def stop(self, device: Device):
        return self.command(device, "setPlayerCmd:stop")


class EventHistory:
    def __init__(self, path: str | None, limit: int = 200):
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
        self._events: list[dict[str, str]] = []
        self._load()

    def record(
        self,
        action: str,
        requester: str,
        outcome: str,
        detail: str,
        leader: str = "",
    ) -> dict[str, str]:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "requester": requester,
            "outcome": outcome,
            "detail": detail,
            "leader": leader,
        }
        with self._lock:
            self._events.append(event)
            self._events = self._events[-self.limit :]
            try:
                self._persist()
            except OSError:
                # A completed group mutation must still be reported if the
                # history volume is temporarily unavailable or full.
                LOGGER.exception("could not persist event history")
        return event

    def snapshot(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(event) for event in reversed(self._events)]

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                self._events = [e for e in payload[-self.limit :] if isinstance(e, dict)]
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("ignoring unreadable event history")

    def _persist(self) -> None:
        if not self.path:
            return
        path = Path(self.path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self._events, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)


class LeadershipPolicy:
    def __init__(self, devices: list[Device], path: str | None):
        self.path = path
        self._lock = threading.Lock()
        self._allowed = {
            device.id: device.auto_join_target for device in devices
        }
        self._load()

    def allowed(self, device_id: str) -> bool:
        with self._lock:
            return self._allowed.get(device_id, False)

    def set(self, device_id: str, enabled: bool) -> None:
        with self._lock:
            if device_id not in self._allowed:
                raise KeyError(device_id)
            self._allowed[device_id] = enabled
            self._persist()

    def set_all(self, enabled_ids: set[str]) -> None:
        with self._lock:
            previous = dict(self._allowed)
            self._allowed = {
                device_id: device_id in enabled_ids for device_id in self._allowed
            }
            try:
                self._persist()
            except OSError:
                self._allowed = previous
                raise

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                for device_id, enabled in payload.items():
                    if device_id in self._allowed and isinstance(enabled, bool):
                        self._allowed[device_id] = enabled
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("ignoring unreadable leadership policy")

    def _persist(self) -> None:
        if not self.path:
            return
        path = Path(self.path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self._allowed, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)


class GroupManager:
    def __init__(
        self,
        devices: list[Device],
        client: WiiMClient,
        history: EventHistory,
        leadership: LeadershipPolicy | None = None,
    ):
        self.devices = devices
        self.by_id = {device.id: device for device in devices}
        self.by_host = {device.host: device for device in devices}
        self.client = client
        self.history = history
        self.leadership = leadership or LeadershipPolicy(devices, None)
        self.lock = threading.Lock()

    def snapshot(self) -> list[DeviceState]:
        states: dict[str, DeviceState] = {}
        with ThreadPoolExecutor(max_workers=len(self.devices)) as pool:
            pending = {pool.submit(self.client.state, device): device for device in self.devices}
            for future in as_completed(pending):
                device = pending[future]
                try:
                    states[device.id] = future.result()
                except Exception as exc:  # preserve dashboard availability
                    states[device.id] = DeviceState(device, False, error=str(exc))
        return [states[device.id] for device in self.devices]

    def join(self, requester_host: str) -> dict[str, str]:
        requester = self.by_host.get(requester_host)
        if requester is None:
            return self.history.record(
                "join", requester_host, "rejected", "request did not come from a known WiiM"
            )
        with self.lock:
            states = self.snapshot()
            by_id = {state.device.id: state for state in states}
            request_state = by_id[requester.id]
            if not request_state.online:
                return self._record("join", requester, "failed", "requester status unavailable")

            active = [
                state
                for state in states
                if state.device != requester
                and self.leadership.allowed(state.device.id)
                and state.active
            ]
            if not active:
                return self._record("join", requester, "no_active", "no other WiiM is playing")

            leaders = []
            for state in active:
                leader = self._resolve_leader(state, states)
                if leader is None:
                    return self._record(
                        "join", requester, "ambiguous", f"cannot resolve leader for {state.device.name}"
                    )
                if leader.device.id not in {item.device.id for item in leaders}:
                    leaders.append(leader)
            if len(leaders) != 1:
                names = ", ".join(state.device.name for state in leaders)
                return self._record(
                    "join", requester, "ambiguous", f"multiple independent sessions: {names}"
                )
            leader = leaders[0]

            if (
                request_state.role == "follower"
                and request_state.master_host == leader.device.host
            ):
                return self._record(
                    "join", requester, "already_grouped", "already in the active group", leader.device
                )

            requester_followers = [
                state
                for state in states
                if state.device != requester
                and state.role == "follower"
                and state.master_host == requester.host
            ]
            if request_state.role == "follower" or requester_followers:
                return self._record(
                    "join",
                    requester,
                    "conflict",
                    "requester already belongs to a different group; leave first",
                )

            try:
                self.client.join(requester, leader.device)
                verified = self._verify_join(requester, leader)
            except WiiMError as exc:
                return self._record("join", requester, "failed", str(exc), leader.device)
            if not verified:
                return self._record(
                    "join", requester, "failed", "join response could not be verified", leader.device
                )
            return self._record(
                "join", requester, "joined", "joined the only active session", leader.device
            )

    def join_device(self, requester_id: str) -> dict[str, str]:
        device = self.by_id.get(requester_id)
        if device is None:
            return self.history.record(
                "join", requester_id, "rejected", "unknown requester id"
            )
        return self.join(device.host)

    def join_device_to(self, requester_id: str, leader_id: str) -> dict[str, str]:
        requester = self.by_id.get(requester_id)
        requested_leader = self.by_id.get(leader_id)
        if requester is None or requested_leader is None:
            return self.history.record(
                "join",
                requester_id,
                "rejected",
                "unknown requester or leader id",
                leader_id,
            )
        if requester == requested_leader:
            return self._record(
                "join", requester, "rejected", "requester and leader must differ"
            )

        with self.lock:
            states = self.snapshot()
            by_id = {state.device.id: state for state in states}
            request_state = by_id[requester.id]
            leader_state = by_id[requested_leader.id]
            if not request_state.online or not leader_state.online:
                return self._record(
                    "join",
                    requester,
                    "failed",
                    "requester or requested leader status unavailable",
                    requested_leader,
                )
            leader = self._resolve_leader(leader_state, states)
            if leader is None:
                return self._record(
                    "join",
                    requester,
                    "ambiguous",
                    "requested player's current leader cannot be resolved",
                    requested_leader,
                )
            if requester == leader.device:
                return self._record(
                    "join", requester, "already_grouped", "requester is the group leader"
                )
            if (
                request_state.role == "follower"
                and request_state.master_host == leader.device.host
            ):
                return self._record(
                    "join",
                    requester,
                    "already_grouped",
                    "already in the requested group",
                    leader.device,
                )
            requester_followers = [
                state
                for state in states
                if state.device != requester
                and state.role == "follower"
                and state.master_host == requester.host
            ]
            if request_state.role == "follower" or requester_followers:
                return self._record(
                    "join",
                    requester,
                    "conflict",
                    "requester already belongs to a different group; leave first",
                    leader.device,
                )
            try:
                self.client.join(requester, leader.device)
                verified = self._verify_join(requester, leader)
            except WiiMError as exc:
                return self._record(
                    "join", requester, "failed", str(exc), leader.device
                )
            if not verified:
                return self._record(
                    "join",
                    requester,
                    "failed",
                    "join response could not be verified",
                    leader.device,
                )
            return self._record(
                "join",
                requester,
                "joined",
                "joined explicitly requested group",
                leader.device,
            )

    def leave(self, requester_host: str) -> dict[str, str]:
        return self._leave(requester_host, shutdown=False)

    def leave_and_sleep(self, requester_host: str) -> dict[str, str]:
        return self._leave(requester_host, shutdown=True)

    def _leave(self, requester_host: str, shutdown: bool) -> dict[str, str]:
        action = "leave_sleep" if shutdown else "leave"
        requester = self.by_host.get(requester_host)
        if requester is None:
            return self.history.record(
                action,
                requester_host,
                "rejected",
                "request did not come from a known WiiM",
            )
        with self.lock:
            states = self.snapshot()
            request_state = next(state for state in states if state.device == requester)
            if not request_state.online:
                return self._record(
                    action, requester, "failed", "requester status unavailable"
                )
            followers = [
                state
                for state in states
                if state.device != requester
                and state.role == "follower"
                and state.master_host == requester.host
            ]
            if request_state.role != "follower" and not followers:
                if not shutdown:
                    return self._record(
                        action, requester, "standalone", "already standalone"
                    )
                try:
                    verified, shutdown_detail = self._shutdown_and_verify(requester)
                except WiiMError as exc:
                    return self._record(action, requester, "failed", str(exc))
                if not verified:
                    return self._record(action, requester, "failed", shutdown_detail)
                return self._record(
                    action,
                    requester,
                    "slept",
                    f"already standalone; {shutdown_detail}",
                )
            leader = None
            if request_state.role == "follower":
                leader = self._resolve_leader(request_state, states)
                if leader is None:
                    return self._record(
                        action,
                        requester,
                        "failed",
                        "current leader could not be resolved",
                    )
            try:
                if request_state.role == "follower":
                    self.client.kick_follower(leader.device, requester)
                    detail = "leader removed follower from current group"
                else:
                    self.client.dissolve(requester)
                    detail = "dissolved current group"
                verified = self._verify_standalone(requester)
            except WiiMError as exc:
                return self._record(
                    action,
                    requester,
                    "failed",
                    str(exc),
                    leader.device if leader else None,
                )
            if not verified:
                return self._record(
                    action,
                    requester,
                    "failed",
                    "durable standalone state could not be verified",
                    leader.device if leader else None,
                )
            if shutdown:
                try:
                    verified, shutdown_detail = self._shutdown_and_verify(requester)
                except WiiMError as exc:
                    return self._record(
                        action,
                        requester,
                        "failed",
                        f"{detail}, but shutdown failed: {exc}",
                        leader.device if leader else None,
                    )
                if not verified:
                    return self._record(
                        action,
                        requester,
                        "failed",
                        f"{detail}, but {shutdown_detail}",
                        leader.device if leader else None,
                    )
                return self._record(
                    action,
                    requester,
                    "slept",
                    f"{detail}; {shutdown_detail}",
                    leader.device if leader else None,
                )
            return self._record(
                action,
                requester,
                "left",
                detail,
                leader.device if leader else None,
            )

    def leave_device(self, requester_id: str) -> dict[str, str]:
        device = self.by_id.get(requester_id)
        if device is None:
            return self.history.record(
                "leave", requester_id, "rejected", "unknown requester id"
            )
        return self.leave(device.host)

    def leave_and_sleep_device(self, requester_id: str) -> dict[str, str]:
        device = self.by_id.get(requester_id)
        if device is None:
            return self.history.record(
                "leave_sleep", requester_id, "rejected", "unknown requester id"
            )
        return self.leave_and_sleep(device.host)

    def disband_device(self, requester_id: str) -> dict[str, str]:
        """Dissolve the requester's complete group without changing playback."""
        requester = self.by_id.get(requester_id)
        if requester is None:
            return self.history.record(
                "disband", requester_id, "rejected", "unknown requester id"
            )
        with self.lock:
            states = self.snapshot()
            request_state = next(state for state in states if state.device == requester)
            if not request_state.online:
                return self._record(
                    "disband", requester, "failed", "requester status unavailable"
                )
            leader_state = self._resolve_leader(request_state, states)
            if leader_state is None:
                return self._record(
                    "disband", requester, "failed", "current leader could not be resolved"
                )
            members = [
                state.device
                for state in states
                if state.device == leader_state.device
                or (
                    state.role == "follower"
                    and state.master_host == leader_state.device.host
                )
            ]
            if len(members) == 1:
                return self._record(
                    "disband",
                    requester,
                    "standalone",
                    "requester is already standalone",
                    leader_state.device,
                )
            try:
                self.client.dissolve(leader_state.device)
                verified = self._verify_members_standalone(members)
            except WiiMError as exc:
                return self._record(
                    "disband", requester, "failed", str(exc), leader_state.device
                )
            if not verified:
                return self._record(
                    "disband",
                    requester,
                    "failed",
                    "not every former member reached durable standalone state",
                    leader_state.device,
                )
            return self._record(
                "disband",
                requester,
                "disbanded",
                f"dissolved a {len(members)}-player group",
                leader_state.device,
            )

    def sleep_device(self, requester_id: str) -> dict[str, str]:
        """Sleep one player, but never while it still belongs to a group."""
        requester = self.by_id.get(requester_id)
        if requester is None:
            return self.history.record(
                "sleep", requester_id, "rejected", "unknown requester id"
            )
        with self.lock:
            states = self.snapshot()
            request_state = next(state for state in states if state.device == requester)
            if not request_state.online:
                return self._record(
                    "sleep", requester, "failed", "requester status unavailable"
                )
            has_followers = any(
                state.role == "follower" and state.master_host == requester.host
                for state in states
            )
            if request_state.role == "follower" or has_followers:
                return self._record(
                    "sleep",
                    requester,
                    "conflict",
                    "player must be standalone before shutdown",
                )
            try:
                verified, detail = self._shutdown_and_verify(requester)
            except WiiMError as exc:
                return self._record("sleep", requester, "failed", str(exc))
            if not verified:
                return self._record("sleep", requester, "failed", detail)
            return self._record(
                "sleep", requester, "slept", detail
            )

    def stop_session_device(self, requester_id: str) -> dict[str, str]:
        """Stop the requester's current session at its resolved leader."""
        requester = self.by_id.get(requester_id)
        if requester is None:
            return self.history.record(
                "stop", requester_id, "rejected", "unknown requester id"
            )
        with self.lock:
            states = self.snapshot()
            request_state = next(state for state in states if state.device == requester)
            if not request_state.online:
                return self._record(
                    "stop", requester, "failed", "requester status unavailable"
                )
            leader_state = self._resolve_leader(request_state, states)
            if leader_state is None:
                return self._record(
                    "stop", requester, "failed", "current leader could not be resolved"
                )
            try:
                self.client.stop(leader_state.device)
            except WiiMError as exc:
                return self._record(
                    "stop", requester, "failed", str(exc), leader_state.device
                )
            return self._record(
                "stop",
                requester,
                "stopped",
                "stopped the resolved playback session",
                leader_state.device,
            )

    def handle_trigger_standby(self, requester_id: str) -> dict[str, str]:
        """Detach a follower whose local 12 V trigger just turned off.

        A Gen 1 remote Standby press on a follower pauses the leader but leaves
        the topology intact. The follower's trigger falls while the leader's
        remains high, distinguishing that room-local action from a normal
        group-wide stop.
        """
        requester = self.by_id.get(requester_id)
        if requester is None:
            return self.history.record(
                "standby_leave", requester_id, "rejected", "unknown requester id"
            )
        with self.lock:
            request_state = self.client.state(requester)
            if not request_state.online:
                return self._record(
                    "standby_leave",
                    requester,
                    "failed",
                    "requester status unavailable after trigger transition",
                )
            leader = (
                self.by_host.get(request_state.master_host)
                if request_state.role == "follower"
                else self._find_leader_by_slave_list(requester)
            )
            if leader is None:
                return self._record(
                    "standby_leave",
                    requester,
                    "ignored",
                    "no configured leader reports this player as a follower",
                )
            try:
                leader_trigger = self.client.trigger_status(leader)
            except WiiMError as exc:
                return self._record(
                    "standby_leave", requester, "failed", str(exc), leader
                )
            if leader_trigger != 1:
                return self._record(
                    "standby_leave",
                    requester,
                    "ignored",
                    "leader trigger is not on; treating this as a group-wide stop",
                    leader,
                )
            try:
                self.client.kick_follower(leader, requester)
                # Restore the leader before durable verification so the remote
                # does not create an avoidable multi-second playback pause.
                self.client.resume(leader)
                verified = self._verify_standalone(requester)
            except WiiMError as exc:
                return self._record(
                    "standby_leave", requester, "failed", str(exc), leader
                )
            if not verified:
                return self._record(
                    "standby_leave",
                    requester,
                    "failed",
                    "leader was resumed but durable standalone state was not verified",
                    leader,
                )
            return self._record(
                "standby_leave",
                requester,
                "restored",
                "detected local Standby; removed follower and resumed leader",
                leader,
            )

    def _find_leader_by_slave_list(self, follower: Device) -> Device | None:
        """Resolve firmware's transient follower-as-standalone report."""
        matches = []
        candidates = [device for device in self.devices if device != follower]
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            pending = {
                pool.submit(self.client.follower_hosts, candidate): candidate
                for candidate in candidates
            }
            for future in as_completed(pending):
                candidate = pending[future]
                try:
                    if follower.host in future.result():
                        matches.append(candidate)
                except Exception:
                    continue
        return matches[0] if len(matches) == 1 else None

    def set_smart_leadership(
        self, requester_id: str, enabled: bool
    ) -> dict[str, str]:
        device = self.by_id.get(requester_id)
        if device is None:
            return self.history.record(
                "leadership", requester_id, "rejected", "unknown requester id"
            )
        try:
            self.leadership.set(requester_id, enabled)
        except OSError as exc:
            return self._record("leadership", device, "failed", str(exc))
        return self._record(
            "leadership",
            device,
            "enabled" if enabled else "disabled",
            "smart leadership enabled" if enabled else "smart leadership disabled",
        )

    def set_smart_leadership_all(self, enabled_ids: set[str]) -> dict[str, str]:
        known_ids = set(self.by_id)
        enabled_ids &= known_ids
        try:
            self.leadership.set_all(enabled_ids)
        except OSError as exc:
            return self.history.record(
                "leadership", "dashboard", "failed", str(exc)
            )
        enabled_names = [
            device.name for device in self.devices if device.id in enabled_ids
        ]
        return self.history.record(
            "leadership",
            "dashboard",
            "updated",
            "allowed smart leaders: " + (", ".join(enabled_names) or "none"),
        )

    @staticmethod
    def _resolve_leader(state: DeviceState, states: list[DeviceState]):
        if state.role != "follower":
            return state
        matches = [
            candidate
            for candidate in states
            if candidate.online
            and candidate.role != "follower"
            and candidate.device.host == state.master_host
        ]
        return matches[0] if len(matches) == 1 else None

    def _verify_join(self, requester: Device, leader: DeviceState) -> bool:
        for _ in range(5):
            time.sleep(1)
            state = self.client.state(requester)
            if (
                state.online
                and state.role == "follower"
                and state.master_host == leader.device.host
            ):
                return True
        return False

    def _verify_standalone(self, requester: Device) -> bool:
        consecutive = 0
        for _ in range(5):
            time.sleep(1)
            state = self.client.state(requester)
            if state.online and state.role != "follower":
                consecutive += 1
            else:
                consecutive = 0
        return consecutive == 5

    def _verify_members_standalone(self, members: list[Device]) -> bool:
        consecutive = 0
        for _ in range(5):
            time.sleep(1)
            states = [self.client.state(device) for device in members]
            if all(state.online and state.role != "follower" for state in states):
                consecutive += 1
            else:
                consecutive = 0
        return consecutive == 5

    def _shutdown_and_verify(self, requester: Device) -> tuple[bool, str]:
        # Sleep's contract is quiet playback and amplifier power off. Firmware
        # setShutdown:0 alone can merely pause while leaving the trigger high.
        self.client.stop(requester)
        for _ in range(5):
            observed = self.client.state(requester)
            if observed.online and observed.playback == "stop":
                break
            time.sleep(1)
        else:
            return False, "could not verify playback stopped; trigger left unchanged"
        status = self.client.trigger_status(requester)
        if status is None:
            result = self.client.shutdown(requester)
            acknowledged = result.get("status") if isinstance(result, dict) else result
            if str(acknowledged).upper() != "OK":
                return False, "playback stopped, but shutdown was not acknowledged"
            return True, "playback stopped; shutdown requested (no trigger telemetry)"
        if status != 0:
            self.client.trigger_off(requester)
        for _ in range(5):
            time.sleep(1)
            observed = self.client.state(requester)
            if not observed.online or observed.playback != "stop":
                return False, "playback did not remain stopped after sleep"
            if self.client.trigger_status(requester) != 0:
                return False, "playback stopped, but 12 V trigger remained active or turned back on"
        return True, "playback stopped; 12 V trigger verified off for five seconds"

    def _record(
        self,
        action: str,
        requester: Device,
        outcome: str,
        detail: str,
        leader: Device | None = None,
    ) -> dict[str, str]:
        LOGGER.info("", extra={"event": "operation", "action": action, "outcome": outcome})
        return self.history.record(
            action,
            requester.name,
            outcome,
            detail,
            leader.name if leader else "",
        )


class TriggerStandbyMonitor:
    """Watch lightweight trigger state without continuously polling topology."""

    def __init__(self, manager: GroupManager, interval: float = 1.0):
        self.manager = manager
        self.interval = max(interval, 0.5)
        self._last: dict[str, int | None] = {}

    def poll_once(self) -> None:
        statuses: dict[str, int | None] = {}
        with ThreadPoolExecutor(max_workers=len(self.manager.devices)) as pool:
            pending = {
                pool.submit(self.manager.client.trigger_status, device): device
                for device in self.manager.devices
            }
            for future in as_completed(pending):
                device = pending[future]
                try:
                    statuses[device.id] = future.result()
                except Exception:
                    statuses[device.id] = None
        for device in self.manager.devices:
            current = statuses.get(device.id)
            previous = self._last.get(device.id)
            self._last[device.id] = current
            if previous == 1 and current == 0:
                try:
                    self.manager.handle_trigger_standby(device.id)
                except Exception:
                    LOGGER.exception(
                        "standby trigger handler failed for %s", device.name
                    )

    def run_forever(self) -> None:
        LOGGER.info(
            "12 V trigger Standby monitor started interval=%.2fs", self.interval
        )
        while True:
            started = time.monotonic()
            self.poll_once()
            time.sleep(max(0, self.interval - (time.monotonic() - started)))


def pbkdf2_password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"pbkdf2-sha256:600000:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, expected_hex = encoded.split(":", 3)
        if algorithm != "pbkdf2-sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(actual.hex(), expected_hex)
    except (ValueError, TypeError):
        return False


class App:
    def __init__(
        self,
        manager: GroupManager,
        username: str,
        password_hash: str,
        secret: str,
        api_token: str,
    ):
        self.manager = manager
        self.username = username
        self.password_hash = password_hash
        self.secret = secret.encode()
        self.api_token = api_token

    def session_token(self) -> str:
        nonce = secrets.token_urlsafe(24)
        signature = hmac.new(self.secret, nonce.encode(), hashlib.sha256).hexdigest()
        return f"{nonce}.{signature}"

    def valid_session(self, token: str) -> bool:
        try:
            nonce, signature = token.rsplit(".", 1)
        except ValueError:
            return False
        expected = hmac.new(self.secret, nonce.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)


class Server(SafeServerErrors, ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app: App):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "WiiMGroupControl/1"

    @property
    def app(self) -> App:
        return self.server.app

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
        elif path == "/preset/leave":
            # Selecting a stream preset on an existing follower changes the
            # leader's source before this HTTP request arrives. That makes a
            # playback URL intrinsically unsafe for leave/ungroup actions.
            self._send(
                HTTPStatus.GONE,
                b"WiiM playback presets cannot safely leave a group.\n",
                "text/plain; charset=utf-8",
            )
        elif path in {"/join", "/leave", "/preset/join"}:
            if path in {"/join", "/preset/join"}:
                event = self.app.manager.join(self.client_address[0])
            else:
                event = self.app.manager.leave(self.client_address[0])
            status = (
                HTTPStatus.OK
                if event["outcome"] not in {"rejected", "failed"}
                else HTTPStatus.CONFLICT
            )
            self._send(status, silent_wav(), "audio/wav")
        elif path == "/api/v1/players":
            if not self._api_authenticated():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            else:
                states = []
                for state in self.app.manager.snapshot():
                    payload = asdict(state)
                    payload["allow_smart_leadership"] = (
                        self.app.manager.leadership.allowed(state.device.id)
                    )
                    states.append(payload)
                self._json(HTTPStatus.OK, {"api_version": 1, "players": states})
        elif path == "/api/v1/events":
            if not self._api_authenticated():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            else:
                self._json(
                    HTTPStatus.OK,
                    {
                        "api_version": 1,
                        "events": self.app.manager.history.snapshot(),
                    },
                )
        elif path == "/login":
            self._login_page()
        elif path in {"/", "/dashboard"}:
            if not self._authenticated():
                self._redirect("/login")
            else:
                self._dashboard()
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in {
            "/api/v1/join",
            "/api/v1/leave",
            "/api/v1/groups/auto-join",
            "/api/v1/groups/join",
            "/api/v1/groups/leave",
            "/api/v1/groups/leave-and-sleep",
            "/api/v1/groups/disband",
            "/api/v1/players/sleep",
            "/api/v1/players/stop-session",
        }:
            if not self._api_authenticated():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            payload = self._read_json()
            if payload is None or not isinstance(payload.get("requester"), str):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "requester is required"})
                return
            if path == "/api/v1/groups/join":
                if not isinstance(payload.get("leader"), str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "leader is required"})
                    return
                event = self.app.manager.join_device_to(
                    payload["requester"], payload["leader"]
                )
            elif path in {"/api/v1/join", "/api/v1/groups/auto-join"}:
                event = self.app.manager.join_device(payload["requester"])
            elif path == "/api/v1/groups/disband":
                event = self.app.manager.disband_device(payload["requester"])
            elif path == "/api/v1/groups/leave-and-sleep":
                event = self.app.manager.leave_and_sleep_device(payload["requester"])
            elif path == "/api/v1/players/sleep":
                event = self.app.manager.sleep_device(payload["requester"])
            elif path == "/api/v1/players/stop-session":
                event = self.app.manager.stop_session_device(payload["requester"])
            else:
                event = self.app.manager.leave_device(payload["requester"])
            status = (
                HTTPStatus.OK
                if event["outcome"] not in {"rejected", "failed"}
                else HTTPStatus.CONFLICT
            )
            self._json(status, {"api_version": 1, "event": event})
        elif path == "/api/v1/policy/leadership":
            if not self._api_authenticated():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            payload = self._read_json()
            enabled = payload.get("enabled") if payload is not None else None
            if not isinstance(enabled, list) or not all(
                isinstance(item, str) for item in enabled
            ):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "enabled must be a list of player ids"},
                )
                return
            unknown = sorted(set(enabled) - set(self.app.manager.by_id))
            if unknown:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "unknown player ids", "players": unknown},
                )
                return
            event = self.app.manager.set_smart_leadership_all(set(enabled))
            status = (
                HTTPStatus.OK
                if event["outcome"] not in {"rejected", "failed"}
                else HTTPStatus.CONFLICT
            )
            self._json(status, {"api_version": 1, "event": event})
        elif path == "/login":
            length = min(int(self.headers.get("Content-Length", "0")), 8192)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            user = form.get("username", [""])[0]
            password = form.get("password", [""])[0]
            if user == self.app.username and verify_password(password, self.app.password_hash):
                token = self.app.session_token()
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/dashboard")
                self.send_header(
                    "Set-Cookie",
                    f"wiim_session={token}; Path=/; HttpOnly; SameSite=Strict",
                )
                self.end_headers()
            else:
                self._login_page("Invalid username or password.", HTTPStatus.UNAUTHORIZED)
        elif path in {
            "/dashboard/leave",
            "/dashboard/leave-sleep",
            "/dashboard/disband",
            "/dashboard/leadership",
        }:
            if not self._authenticated():
                self._redirect("/login")
                return
            length = min(int(self.headers.get("Content-Length", "0")), 8192)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            if path == "/dashboard/leadership":
                self.app.manager.set_smart_leadership_all(
                    set(form.get("enabled", []))
                )
            elif path.endswith("leave-sleep"):
                requester = form.get("requester", [""])[0]
                self.app.manager.leave_and_sleep_device(requester)
            elif path.endswith("disband"):
                requester = form.get("requester", [""])[0]
                self.app.manager.disband_device(requester)
            else:
                requester = form.get("requester", [""])[0]
                self.app.manager.leave_device(requester)
            self._redirect("/dashboard")
        elif path == "/logout":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/login")
            self.send_header(
                "Set-Cookie", "wiim_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
            )
            self.end_headers()
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    def _dashboard(self):
        states = self.app.manager.snapshot()
        rows = "".join(
            f"<tr><td>{html.escape(state.device.name)}</td>"
            f"<td>{html.escape(state.device.host)}</td>"
            f"<td>{'Online' if state.online else 'Unavailable'}</td>"
            f"<td>{html.escape(state.playback)}</td>"
            f"<td>{html.escape(state.role)}</td>"
            f"<td>{html.escape(state.master_host or '—')}</td>"
            f"<td>{html.escape(state.group_name or '—')}</td>"
            f"<td>{html.escape(state.volume or '—')}</td>"
            f'<td><div class="actions"><form method="post" action="/dashboard/leave">'
            f'<input type="hidden" name="requester" value="{html.escape(state.device.id, quote=True)}">'
            f'<button type="submit">Leave</button></form><form method="post" action="/dashboard/leave-sleep">'
            f'<input type="hidden" name="requester" value="{html.escape(state.device.id, quote=True)}">'
            f'<button type="submit">Leave &amp; sleep</button></form><form method="post" action="/dashboard/disband">'
            f'<input type="hidden" name="requester" value="{html.escape(state.device.id, quote=True)}">'
            f'<button type="submit">Disband group</button></form></div></td></tr>'
            for state in states
        )
        events = self.app.manager.history.snapshot()[:50]
        event_rows = "".join(
            f"<tr><td>{html.escape(event.get('timestamp', ''))}</td>"
            f"<td>{html.escape(event.get('requester', ''))}</td>"
            f"<td>{html.escape(event.get('action', ''))}</td>"
            f"<td>{html.escape(event.get('outcome', ''))}</td>"
            f"<td>{html.escape(event.get('leader', '') or '—')}</td>"
            f"<td>{html.escape(event.get('detail', ''))}</td></tr>"
            for event in events
        ) or '<tr><td colspan="6">No actions recorded yet.</td></tr>'
        leadership_fields = "".join(
            f'<label class="check"><input type="checkbox" name="enabled" '
            f'value="{html.escape(device.id, quote=True)}" '
            f'{"checked" if self.app.manager.leadership.allowed(device.id) else ""}>'
            f'{html.escape(device.name)}</label>'
            for device in self.app.manager.devices
        )
        body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="15">
<title>WiiM group control</title>{style()}</head><body><main>
<h1>WiiM group control</h1><p>Live state is queried when this page refreshes. Audio never passes through this service.</p>
<form class="refresh" method="get" action="/dashboard"><button type="submit">Refresh now</button></form>
<h2>Players</h2><table><thead><tr><th>Player</th><th>IP</th><th>Reachability</th><th>Playback</th><th>Role</th><th>Leader IP</th><th>Device group name</th><th>Volume</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Smart leadership</h2><p>Only checked players can be selected by “join whatever is playing.” Explicit API grouping is unaffected.</p>
<form class="policy" method="post" action="/dashboard/leadership"><div class="checks">{leadership_fields}</div><button type="submit">Submit changes</button></form>
<h2>Recent decisions</h2><table><thead><tr><th>UTC</th><th>Requester</th><th>Action</th><th>Outcome</th><th>Leader</th><th>Detail</th></tr></thead><tbody>{event_rows}</tbody></table>
<form method="post" action="/logout"><button type="submit">Sign out</button></form>
</main></body></html>"""
        self._html(HTTPStatus.OK, body)

    def _login_page(self, message: str = "", status=HTTPStatus.OK):
        notice = f'<p class="error">{html.escape(message)}</p>' if message else ""
        body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>WiiM group control</title>{style()}</head>
<body><main class="login"><h1>WiiM group control</h1>{notice}<form method="post" action="/login">
<label>Username<input name="username" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Sign in</button></form></main></body></html>"""
        self._html(status, body)

    def _authenticated(self) -> bool:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        morsel = cookie.get("wiim_session")
        return bool(morsel and self.app.valid_session(morsel.value))

    def _api_authenticated(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.app.api_token}"
        return hmac.compare_digest(supplied, expected)

    def _read_json(self):
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 8192)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except (ValueError, json.JSONDecodeError):
            return None

    def _redirect(self, location: str):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _html(self, status, body: str):
        self._send(status, body.encode(), "text/html; charset=utf-8")

    def _json(self, status, payload):
        self._send(
            status,
            (json.dumps(payload, separators=(",", ":")) + "\n").encode(),
            "application/json",
        )

    def _send(self, status, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        request_event(LOGGER, getattr(self, "command", ""), args)


def silent_wav() -> bytes:
    sample_rate = 8000
    samples = b"\x00\x00" * 800
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(samples))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(samples))
        + samples
    )


def style() -> str:
    return """<style>:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#111;color:#eee}body{margin:0;padding:1rem}main{max-width:86rem;margin:4vh auto;background:#1d1d1d;border:1px solid #444;border-radius:1rem;padding:1.4rem}.login{max-width:28rem}h1{margin-top:0}table{width:100%;border-collapse:collapse;font-size:.9rem;display:block;overflow-x:auto}th,td{text-align:left;border-bottom:1px solid #444;padding:.55rem;white-space:nowrap}label{display:block;margin:.8rem 0}input,button{box-sizing:border-box;width:100%;font:inherit;padding:.7rem;border:1px solid #666;border-radius:.5rem;background:#111;color:#fff}button{margin-top:1rem;background:#355d77;font-weight:650}.actions{display:flex;gap:.4rem}.actions form{margin:0}.actions input{display:none}.actions button{margin:0;padding:.45rem .6rem;width:auto}.actions form:last-child button{background:#66402e}.refresh button{margin:.4rem 0;width:auto}.policy{max-width:38rem;border:1px solid #444;border-radius:.7rem;padding:1rem}.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(12rem,1fr));gap:.45rem}.check{display:flex;align-items:center;gap:.55rem;margin:0}.check input{width:auto}.error{background:#552424;padding:.7rem;border-radius:.5rem}</style>"""


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def main():
    configure("topology")
    config = load_config(required_env("CONFIG_PATH"))
    if config is None:
        raise SystemExit("Setup required: create configuration before starting topology")
    credentials = credential_settings("topology")
    devices = [Device(p.id, p.name, p.host, p.auto_join_target) for p in config.players]
    history = EventHistory(os.environ.get("HISTORY_PATH", "/state/history.json"))
    leadership = LeadershipPolicy(
        devices, os.environ.get("POLICY_PATH", "/state/leadership.json")
    )
    manager = GroupManager(devices, WiiMClient(), history, leadership)
    if os.environ.get("TRIGGER_STANDBY_ENABLED", "0") == "1":
        monitor = TriggerStandbyMonitor(
            manager, float(os.environ.get("TRIGGER_MONITOR_INTERVAL", "1.0"))
        )
        threading.Thread(target=monitor.run_forever, daemon=True).start()
    app = App(
        manager,
        credentials["WEB_USERNAME"],
        credentials["WEB_PASSWORD_HASH"],
        credentials["WEB_SESSION_SECRET"],
        credentials["SERVICE_API_TOKEN"],
    )
    host = os.environ.get("LISTEN_HOST", "127.0.0.1")
    port = int(os.environ.get("LISTEN_PORT", "8793"))
    LOGGER.info("listening on %s:%s for %d WiiMs", host, port, len(devices))
    Server((host, port), app).serve_forever()


if __name__ == "__main__":
    main()
