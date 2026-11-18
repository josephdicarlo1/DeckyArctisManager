import asyncio
import json
import os
import re
import shutil
import subprocess
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

try:
    import pwd
except ImportError:
    pwd = None  # type: ignore[assignment]

import decky

INSTALL_SCRIPT_URL = (
    "https://github.com/elegos/Linux-Arctis-Manager/releases/latest/download/install.sh"
)

DBUS_BUS_NAME = "name.giacomofurlan.ArctisManager.Next"

DBUS_SETTINGS_PATH = "/name/giacomofurlan/ArctisManager/Next/Settings"
DBUS_SETTINGS_IFACE = "name.giacomofurlan.ArctisManager.Next.Settings"

DBUS_STATUS_PATH = "/name/giacomofurlan/ArctisManager/Next/Status"
DBUS_STATUS_IFACE = "name.giacomofurlan.ArctisManager.Next.Status"

DBUS_CONFIG_PATH = "/name/giacomofurlan/ArctisManager/Next/Config"
DBUS_CONFIG_IFACE = "name.giacomofurlan.ArctisManager.Next.Config"

SERVICES = ["lam-hidraw-helper.service", "lam-daemon.service"]

ACRONYMS = {
    "anc": "ANC",
    "eq": "EQ",
    "usb": "USB",
    "led": "LED",
    "id": "ID",
    "bt": "BT",
    "nc": "NC",
    "vc": "VC",
    "db": "dB",
}


def _humanize_key(key: str) -> str:
    """Convert a snake_case identifier into a readable label."""
    parts = [p for p in re.split(r"[_\-]+", key.strip()) if p]
    if not parts:
        return key
    words: List[str] = []
    for part in parts:
        lower = part.lower()
        if lower in ACRONYMS:
            words.append(ACRONYMS[lower])
        else:
            words.append(part.capitalize())
    return " ".join(words)


def _classify_setting(key: str) -> str:
    """Categorize a setting key for UI grouping and prioritization."""
    lower = key.lower()
    tokens = set(re.split(r"[_\-]+", lower))
    if "anc" in tokens or "noise_cancell" in lower or "noise_control" in lower:
        return "anc"
    if "transparen" in lower or "ambient" in lower:
        return "transparency"
    if "eq" in tokens or "equalizer" in lower:
        return "eq"
    if "sidetone" in lower:
        return "sidetone"
    if "mic" in tokens or "microphone" in lower:
        return "mic"
    if any(word in lower for word in ("auto_off", "sleep", "idle", "power")):
        return "power"
    return "other"


def _is_valid_battery(value: Any) -> bool:
    """Return True if value looks like a valid numeric battery percentage."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0 <= value <= 100


class Plugin:
    def _get_deck_user(self) -> Tuple[str, int, int, str]:
        """Resolve the target non-root SteamOS user ('deck')."""
        candidates: List[str] = []
        if os.path.isdir("/home/deck"):
            candidates.append("deck")
        env_user = os.environ.get("DECKY_USER") or getattr(decky, "DECKY_USER", "")
        if env_user and env_user != "root" and env_user not in candidates:
            candidates.append(env_user)
        if "deck" not in candidates:
            candidates.append("deck")

        if pwd is not None:
            for username in candidates:
                try:
                    pw = pwd.getpwnam(username)
                    return (pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir)
                except KeyError:
                    continue

        return ("deck", 1000, 1000, "/home/deck")

    def _get_deck_env(self, extra_path: Optional[str] = None) -> Dict[str, str]:
        """Build the environment dictionary for running commands as the deck user."""
        username, uid, _, home = self._get_deck_user()
        env = os.environ.copy()
        path_parts: List[str] = []
        if extra_path:
            path_parts.append(extra_path)
        path_parts.extend([f"{home}/.local/bin", "/usr/local/bin", "/usr/bin", "/bin"])

        env["HOME"] = home
        env["USER"] = username
        env["LOGNAME"] = username
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
        env["XDG_CONFIG_HOME"] = f"{home}/.config"
        env["XDG_DATA_HOME"] = f"{home}/.local/share"
        env["PATH"] = ":".join(path_parts)
        return env

    async def _run_as_deck(
        self,
        cmd: List[str],
        input_data: Optional[str] = None,
        timeout: float = 15.0,
        extra_path: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        """Execute a subprocess dropped to the deck user's uid/gid and session env."""
        _, uid, gid, home = self._get_deck_user()
        env = self._get_deck_env(extra_path=extra_path)
        cwd = home if os.path.isdir(home) else None

        kwargs: Dict[str, Any] = {
            "env": env,
            "cwd": cwd,
            "input": input_data,
            "capture_output": True,
            "text": True,
            "timeout": timeout,
        }
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            kwargs["user"] = uid
            kwargs["group"] = gid

        def _exec() -> subprocess.CompletedProcess[str]:
            return subprocess.run(cmd, **kwargs)

        try:
            proc = await asyncio.to_thread(_exec)
            return (proc.returncode, proc.stdout or "", proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            decky.logger.error(f"Command timed out ({cmd}): {exc}")
            return (-1, "", f"Command timed out after {timeout}s")
        except Exception as exc:
            decky.logger.error(f"Command failed ({cmd}): {exc}")
            return (-1, "", str(exc))

    async def _run_as_root(
        self, cmd: List[str], timeout: float = 15.0
    ) -> Tuple[int, str, str]:
        """Execute a subprocess using Decky's current (root) privileges."""
        def _exec() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )

        try:
            proc = await asyncio.to_thread(_exec)
            return (proc.returncode, proc.stdout or "", proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            decky.logger.error(f"Root command timed out ({cmd}): {exc}")
            return (-1, "", f"Command timed out after {timeout}s")
        except Exception as exc:
            decky.logger.error(f"Root command failed ({cmd}): {exc}")
            return (-1, "", str(exc))

    def _find_cap_tool(self, name: str) -> Optional[str]:
        """Locate setcap or getcap across standard SteamOS/Linux paths."""
        found = shutil.which(name)
        if found:
            return found
        for candidate in (
            f"/usr/bin/{name}",
            f"/usr/sbin/{name}",
            f"/bin/{name}",
            f"/sbin/{name}",
        ):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    async def _check_helper_capabilities(self, helper_bin: str) -> bool:
        """Check whether lam-hidraw-helper has cap_dac_override applied."""
        if not os.path.isfile(helper_bin):
            return False
        getcap_bin = self._find_cap_tool("getcap")
        if not getcap_bin:
            return False
        rc, stdout, _ = await self._run_as_root([getcap_bin, helper_bin], timeout=5.0)
        return rc == 0 and "cap_dac_override" in stdout.lower()

    async def _apply_helper_setcap(
        self, helper_bin: str, cap_spec: str = "cap_dac_override+ep"
    ) -> Tuple[bool, str]:
        """Apply required capabilities to lam-hidraw-helper as root."""
        if not os.path.isfile(helper_bin):
            return (False, f"Helper binary not found at {helper_bin}")

        setcap_bin = self._find_cap_tool("setcap")
        if not setcap_bin:
            return (False, "setcap binary not found on system")

        specs_to_try = [cap_spec]
        if cap_spec != "cap_dac_override+ep":
            specs_to_try.append("cap_dac_override+ep")

        last_err = ""
        for spec in specs_to_try:
            rc, stdout, stderr = await self._run_as_root(
                [setcap_bin, spec, helper_bin], timeout=10.0
            )
            if rc == 0:
                decky.logger.info(f"Applied setcap '{spec}' to {helper_bin}")
                return (True, "")
            last_err = stderr.strip() or stdout.strip() or f"setcap exited {rc}"
            decky.logger.warning(f"setcap '{spec}' failed on {helper_bin}: {last_err}")

        return (False, last_err)

    async def _dbus_call(
        self,
        object_path: str,
        interface: str,
        method: str,
        signature: str = "",
        *args: str,
    ) -> Tuple[bool, Any, str]:
        """Invoke a method on the deck user's session D-Bus via busctl."""
        cmd = [
            "busctl",
            "--user",
            "--json=short",
            "call",
            DBUS_BUS_NAME,
            object_path,
            interface,
            method,
        ]
        if signature:
            cmd.append(signature)
            cmd.extend(args)

        rc, stdout, stderr = await self._run_as_deck(cmd, timeout=10.0)
        if rc != 0:
            err = stderr.strip() or stdout.strip() or f"busctl exited with code {rc}"
            return (False, None, err)

        raw = stdout.strip()
        if not raw:
            return (True, None, "")

        try:
            outer = json.loads(raw)
        except json.JSONDecodeError as exc:
            return (False, None, f"Invalid busctl JSON output: {exc}")

        data = outer.get("data", []) if isinstance(outer, dict) else []
        if not data:
            return (True, None, "")

        first = data[0]
        if isinstance(first, str):
            try:
                return (True, json.loads(first), "")
            except json.JSONDecodeError:
                return (True, first, "")
        return (True, first, "")

    async def get_status(self) -> Dict[str, Any]:
        """Check linux-arctis-manager installation, capabilities, and systemd services."""
        _, _, _, home = self._get_deck_user()
        daemon_bin = os.path.join(home, ".local", "bin", "lam-daemon")
        helper_bin = os.path.join(home, ".local", "bin", "lam-hidraw-helper")

        installed = os.path.isfile(daemon_bin) and os.access(daemon_bin, os.X_OK)
        helper_installed = os.path.isfile(helper_bin) and os.access(
            helper_bin, os.X_OK
        )
        has_capabilities = (
            await self._check_helper_capabilities(helper_bin)
            if helper_installed
            else False
        )

        daemon_state = "unknown"
        helper_state = "unknown"
        if installed:
            _, stdout, _ = await self._run_as_deck(
                [
                    "systemctl",
                    "--user",
                    "is-active",
                    "lam-daemon.service",
                    "lam-hidraw-helper.service",
                ],
                timeout=10.0,
            )
            lines = [line.strip() for line in stdout.splitlines() if line.strip()]
            if len(lines) > 0:
                daemon_state = lines[0]
            if len(lines) > 1:
                helper_state = lines[1]

        daemon_active = daemon_state == "active"
        helper_active = helper_state == "active"

        return {
            "installed": installed,
            "helper_installed": helper_installed,
            "daemon_active": daemon_active,
            "helper_active": helper_active,
            "daemon_state": daemon_state,
            "helper_state": helper_state,
            "service_running": daemon_active and helper_active,
            "has_capabilities": has_capabilities,
            "daemon_path": daemon_bin,
            "helper_path": helper_bin,
        }

    async def _download_installer(self, dest_path: str) -> Tuple[bool, str]:
        """Download upstream install.sh to dest_path."""
        rc, stdout, stderr = await self._run_as_deck(
            [
                "curl",
                "--proto",
                "=https",
                "--tlsv1.2",
                "-fsSL",
                INSTALL_SCRIPT_URL,
                "-o",
                dest_path,
            ],
            timeout=60.0,
        )
        if rc == 0 and os.path.isfile(dest_path):
            return (True, "")

        curl_err = stderr.strip() or stdout.strip() or f"curl exited {rc}"
        decky.logger.warning(f"curl download failed, trying urllib: {curl_err}")

        def _urllib_download() -> None:
            req = urllib.request.Request(
                INSTALL_SCRIPT_URL,
                headers={"User-Agent": "DeckyArctisManager/0.1"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                content = resp.read()
            with open(dest_path, "wb") as f:
                f.write(content)

        try:
            await asyncio.to_thread(_urllib_download)
            return (True, "")
        except Exception as exc:
            return (False, f"curl failed ({curl_err}); urllib failed ({exc})")

    async def install_manager(self) -> Dict[str, Any]:
        """Download and run linux-arctis-manager installer non-interactively."""
        _, uid, gid, home = self._get_deck_user()
        installer_path = "/tmp/lam-install.sh"
        noninteractive_path = "/tmp/lam-install-noninteractive.sh"
        shim_dir = "/tmp/lam-shim-bin"
        shim_log = "/tmp/lam-shim-cmds.log"

        def _cleanup() -> None:
            for path in (installer_path, noninteractive_path, shim_log):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
            try:
                if os.path.isdir(shim_dir):
                    shutil.rmtree(shim_dir, ignore_errors=True)
            except OSError:
                pass

        _cleanup()
        try:
            ok, dl_err = await self._download_installer(installer_path)
            if not ok:
                return {
                    "success": False,
                    "message": f"Failed to download installer: {dl_err}",
                    "status": await self.get_status(),
                }

            with open(installer_path, "r", encoding="utf-8", errors="replace") as f:
                script_text = f.read()

            # Inspect setcap flags requested by upstream install.sh
            cap_spec = "cap_dac_override+ep"
            cap_match = re.search(
                r"setcap\s+(?:-[a-zA-Z]+\s+)?['\"]?([a-zA-Z0-9_,+=]+)['\"]?",
                script_text,
            )
            if cap_match:
                cap_spec = cap_match.group(1)
            decky.logger.info(f"Detected setcap spec from install.sh: {cap_spec}")

            # Strip </dev/tty redirections so stdin answers flow into read prompts
            patched_script = re.sub(r"<\s*/dev/tty", "", script_text)
            with open(noninteractive_path, "w", encoding="utf-8") as f:
                f.write(patched_script)

            # Create shims for pkexec/sudo so install.sh never blocks on GUI/TTY auth
            os.makedirs(shim_dir, mode=0o755, exist_ok=True)
            shim_body = f"#!/bin/sh\necho \"$@\" >> {shim_log}\nexit 0\n"
            for shim_name in ("pkexec", "sudo"):
                shim_path = os.path.join(shim_dir, shim_name)
                with open(shim_path, "w", encoding="utf-8") as f:
                    f.write(shim_body)
                os.chmod(shim_path, 0o755)
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    os.chown(shim_path, uid, gid)

            os.chmod(noninteractive_path, 0o755)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(shim_dir, uid, gid)
                os.chown(noninteractive_path, uid, gid)

            rc, stdout, stderr = await self._run_as_deck(
                ["/bin/sh", noninteractive_path],
                input_data="y\ny\ny\ny\ny\n\n\n",
                timeout=180.0,
                extra_path=shim_dir,
            )
            decky.logger.info(f"install.sh exited with rc={rc}")
            if stdout.strip():
                decky.logger.info(f"install.sh stdout:\n{stdout[-2000:]}")
            if stderr.strip():
                decky.logger.warning(f"install.sh stderr:\n{stderr[-2000:]}")

            if os.path.isfile(shim_log):
                try:
                    with open(shim_log, "r", encoding="utf-8", errors="replace") as f:
                        shim_cmds = f.read().strip()
                    if shim_cmds:
                        decky.logger.info(f"Intercepted elevated cmds:\n{shim_cmds}")
                        shim_cap_match = re.search(
                            r"setcap\s+(?:-[a-zA-Z]+\s+)?['\"]?([a-zA-Z0-9_,+=]+)['\"]?",
                            shim_cmds,
                        )
                        if shim_cap_match:
                            cap_spec = shim_cap_match.group(1)
                except OSError:
                    pass

            helper_bin = os.path.join(home, ".local", "bin", "lam-hidraw-helper")
            setcap_ok, setcap_err = await self._apply_helper_setcap(
                helper_bin, cap_spec=cap_spec
            )

            await self._run_as_deck(
                ["systemctl", "--user", "daemon-reload"], timeout=15.0
            )
            await self._run_as_deck(
                ["systemctl", "--user", "enable", "--now", *SERVICES], timeout=20.0
            )
            await self._run_as_deck(
                ["systemctl", "--user", "restart", *SERVICES], timeout=20.0
            )

            await asyncio.sleep(1.5)
            status = await self.get_status()

            if not status["installed"]:
                detail = stderr.strip() or stdout.strip() or f"exit code {rc}"
                return {
                    "success": False,
                    "message": f"Installation did not produce lam-daemon ({detail[-250:]})",
                    "status": status,
                }

            if not setcap_ok:
                return {
                    "success": False,
                    "message": f"Installed binaries, but setcap failed: {setcap_err}",
                    "status": status,
                }

            if not status["service_running"]:
                return {
                    "success": False,
                    "message": "Installed successfully, but systemd user services failed to start.",
                    "status": status,
                }

            return {
                "success": True,
                "message": "Linux Arctis Manager installed and started.",
                "status": status,
            }
        except Exception as exc:
            decky.logger.exception("Unexpected error during install_manager")
            return {
                "success": False,
                "message": f"Installation error: {exc}",
                "status": await self.get_status(),
            }
        finally:
            _cleanup()

    async def restart_service(self) -> Dict[str, Any]:
        """Ensure helper capabilities and start/restart systemd user services."""
        _, _, _, home = self._get_deck_user()
        helper_bin = os.path.join(home, ".local", "bin", "lam-hidraw-helper")

        if os.path.isfile(helper_bin):
            has_cap = await self._check_helper_capabilities(helper_bin)
            if not has_cap:
                cap_ok, cap_err = await self._apply_helper_setcap(helper_bin)
                if not cap_ok:
                    decky.logger.warning(
                        f"Could not ensure setcap before service restart: {cap_err}"
                    )

        await self._run_as_deck(["systemctl", "--user", "daemon-reload"], timeout=15.0)
        rc_en, out_en, err_en = await self._run_as_deck(
            ["systemctl", "--user", "enable", "--now", *SERVICES], timeout=20.0
        )
        rc_rs, out_rs, err_rs = await self._run_as_deck(
            ["systemctl", "--user", "restart", *SERVICES], timeout=20.0
        )

        await asyncio.sleep(1.0)
        status = await self.get_status()
        if status["service_running"]:
            return {
                "success": True,
                "message": "Arctis Manager services are active.",
                "status": status,
            }

        err_msg = (
            err_rs.strip()
            or err_en.strip()
            or out_rs.strip()
            or out_en.strip()
            or f"systemctl exited ({rc_en}/{rc_rs})"
        )
        return {
            "success": False,
            "message": f"Failed to start services: {err_msg}",
            "status": status,
        }

    def _coerce_dict_key(self, key: Any, sample: Any) -> Any:
        """Convert JSON string dict keys back to int when sample values are ints."""
        if (
            isinstance(key, str)
            and isinstance(sample, int)
            and not isinstance(sample, bool)
        ):
            try:
                return int(key)
            except ValueError:
                return key
        return key

    def _normalize_select_options(
        self,
        cfg: Dict[str, Any],
        dynamic_list: Optional[List[Any]] = None,
        sample_value: Any = None,
    ) -> List[Dict[str, Any]]:
        """Normalize static or dynamic select options into [{value, label}]."""
        options: List[Dict[str, Any]] = []
        sample = sample_value if sample_value is not None else cfg.get("default_value")

        if dynamic_list is not None:
            mapping = cfg.get("options_mapping") or {}
            val_key = mapping.get("value", "id")
            lbl_key = mapping.get("label", "name")
            for item in dynamic_list:
                if isinstance(item, dict):
                    val = item.get(val_key, item.get("id"))
                    lbl = (
                        item.get(lbl_key)
                        or item.get("name")
                        or item.get("description")
                        or item.get("label")
                        or str(val)
                    )
                    options.append({"value": val, "label": str(lbl)})
                else:
                    options.append({"value": item, "label": str(item)})
            return options

        raw_options = cfg.get("options")
        if isinstance(raw_options, list):
            for item in raw_options:
                if isinstance(item, dict):
                    val = item.get("value", item.get("id", item.get("key")))
                    lbl = (
                        item.get("label")
                        or item.get("name")
                        or item.get("description")
                        or _humanize_key(str(val))
                    )
                    options.append({"value": val, "label": str(lbl)})
                else:
                    options.append({"value": item, "label": _humanize_key(str(item))})
            return options

        if isinstance(raw_options, dict):
            for k, v in raw_options.items():
                options.append(
                    {"value": self._coerce_dict_key(k, sample), "label": str(v)}
                )
            return options

        raw_values = cfg.get("values")
        if isinstance(raw_values, dict):
            for k, v in raw_values.items():
                options.append(
                    {"value": self._coerce_dict_key(k, sample), "label": str(v)}
                )
        elif isinstance(raw_values, list):
            for item in raw_values:
                if isinstance(item, dict):
                    val = item.get("value", item.get("id"))
                    lbl = item.get("label") or item.get("name") or str(val)
                    options.append({"value": val, "label": str(lbl)})
                else:
                    options.append({"value": item, "label": _humanize_key(str(item))})

        return options

    async def _build_setting_entries(
        self,
        section_name: str,
        values_map: Dict[str, Any],
        settings_config: Dict[str, Any],
        list_cache: Dict[str, List[Any]],
    ) -> List[Dict[str, Any]]:
        """Normalize raw settings values and config descriptors for the frontend."""
        entries: List[Dict[str, Any]] = []
        for key, current_val in values_map.items():
            cfg = settings_config.get(key)
            if not isinstance(cfg, dict):
                cfg = {}

            setting_type = cfg.get("type")
            if not setting_type:
                if isinstance(current_val, bool):
                    setting_type = "toggle"
                elif isinstance(current_val, (int, float)):
                    setting_type = "slider"
                else:
                    setting_type = "select"

            dynamic_list: Optional[List[Any]] = None
            options_source = cfg.get("options_source")
            if setting_type == "select" and isinstance(options_source, str) and options_source:
                if options_source not in list_cache:
                    ok, fetched, err = await self._dbus_call(
                        DBUS_SETTINGS_PATH,
                        DBUS_SETTINGS_IFACE,
                        "GetListOptions",
                        "s",
                        options_source,
                    )
                    if ok and isinstance(fetched, list):
                        list_cache[options_source] = fetched
                    else:
                        decky.logger.warning(
                            f"GetListOptions('{options_source}') failed: {err}"
                        )
                        list_cache[options_source] = []
                dynamic_list = list_cache[options_source]

            options = (
                self._normalize_select_options(
                    cfg, dynamic_list, sample_value=current_val
                )
                if setting_type == "select"
                else []
            )

            entries.append(
                {
                    "key": key,
                    "label": _humanize_key(key),
                    "section": section_name,
                    "value": current_val,
                    "type": setting_type,
                    "available": cfg.get("available", True),
                    "unavailable_reason": cfg.get("unavailable_reason"),
                    "default_value": cfg.get("default_value"),
                    "min": cfg.get("min"),
                    "max": cfg.get("max"),
                    "step": cfg.get("step", 1),
                    "min_label": cfg.get("min_label"),
                    "max_label": cfg.get("max_label"),
                    "values": cfg.get("values"),
                    "options": options,
                    "category": _classify_setting(key),
                }
            )
        return entries

    def _parse_status_payload(self, raw_status: Any) -> Dict[str, Any]:
        """Extract GameBuds/headset battery roles, charging state, and status items."""
        roles: Dict[str, Any] = {}
        status_items: List[Dict[str, Any]] = []
        power_status: Optional[str] = None
        charging_hints: Dict[str, Any] = {
            "left": None,
            "right": None,
            "case": None,
            "headset": None,
        }

        if not isinstance(raw_status, dict):
            return {
                "batteries": {},
                "status_items": [],
                "power_status": None,
                "roles": {},
            }

        for category, fields in raw_status.items():
            if not isinstance(fields, dict):
                continue
            for key, info in fields.items():
                if not isinstance(info, dict):
                    continue
                val = info.get("value")
                ftype = info.get("type", "label")
                role = info.get("role")
                if isinstance(role, str) and role:
                    roles[role] = val

                status_items.append(
                    {
                        "category": category,
                        "key": key,
                        "label": _humanize_key(key),
                        "value": val,
                        "type": ftype,
                        "role": role,
                    }
                )

                lower_key = key.lower()
                if lower_key in (
                    "headset_power_status",
                    "power_status",
                    "connection_status",
                ) and val is not None:
                    power_status = str(val)

                if "charg" in lower_key and "battery" not in lower_key:
                    if "left" in lower_key:
                        charging_hints["left"] = val
                    elif "right" in lower_key:
                        charging_hints["right"] = val
                    elif "case" in lower_key:
                        charging_hints["case"] = val
                    elif "headset" in lower_key:
                        charging_hints["headset"] = val

        # Build unified GameBuds / headset battery view per .docs/dbus.md roles
        left_val = roles.get("battery_left")
        left_case_val = roles.get("battery_case_left")
        if _is_valid_battery(left_val):
            left_battery = {
                "level": left_val,
                "source": "dongle",
                "charging": charging_hints["left"],
            }
        elif _is_valid_battery(left_case_val):
            left_battery = {
                "level": left_case_val,
                "source": "case",
                "charging": charging_hints["left"] if charging_hints["left"] is not None else True,
            }
        else:
            left_battery = {"level": None, "source": None, "charging": charging_hints["left"]}

        right_val = roles.get("battery_right")
        right_case_val = roles.get("battery_case_right")
        if _is_valid_battery(right_val):
            right_battery = {
                "level": right_val,
                "source": "dongle",
                "charging": charging_hints["right"],
            }
        elif _is_valid_battery(right_case_val):
            right_battery = {
                "level": right_case_val,
                "source": "case",
                "charging": charging_hints["right"] if charging_hints["right"] is not None else True,
            }
        else:
            right_battery = {"level": None, "source": None, "charging": charging_hints["right"]}

        case_val = roles.get("battery_case")
        case_battery = {
            "level": case_val if _is_valid_battery(case_val) else None,
            "charging": charging_hints["case"],
        }

        headset_val = roles.get("battery_headset")
        headset_battery = {
            "level": headset_val if _is_valid_battery(headset_val) else None,
            "charging": charging_hints["headset"],
        }

        dock_val = roles.get("battery_dock")
        dock_battery = {
            "level": dock_val if _is_valid_battery(dock_val) else None,
        }

        return {
            "batteries": {
                "left": left_battery,
                "right": right_battery,
                "case": case_battery,
                "headset": headset_battery,
                "dock": dock_battery,
                "raw_roles": roles,
            },
            "status_items": status_items,
            "power_status": power_status,
            "roles": roles,
        }

    async def get_device_state(self) -> Dict[str, Any]:
        """Fetch full daemon status, device battery/status telemetry, and settings."""
        sys_status = await self.get_status()
        if not sys_status["installed"] or not sys_status["service_running"]:
            return {
                **sys_status,
                "connected": False,
                "dbus_reachable": False,
                "power_status": None,
                "batteries": {},
                "status_items": [],
                "device_settings": [],
                "general_settings": [],
                "error": None,
            }

        status_ok, raw_status, status_err = await self._dbus_call(
            DBUS_STATUS_PATH, DBUS_STATUS_IFACE, "GetStatus"
        )
        settings_ok, raw_settings, settings_err = await self._dbus_call(
            DBUS_SETTINGS_PATH, DBUS_SETTINGS_IFACE, "GetSettings"
        )

        if not status_ok and not settings_ok:
            return {
                **sys_status,
                "connected": False,
                "dbus_reachable": False,
                "power_status": None,
                "batteries": {},
                "status_items": [],
                "device_settings": [],
                "general_settings": [],
                "error": status_err or settings_err,
            }

        parsed_status = self._parse_status_payload(raw_status if status_ok else {})

        device_map: Dict[str, Any] = {}
        general_map: Dict[str, Any] = {}
        settings_config: Dict[str, Any] = {}
        if settings_ok and isinstance(raw_settings, dict):
            if isinstance(raw_settings.get("device"), dict):
                device_map = raw_settings["device"]
            if isinstance(raw_settings.get("general"), dict):
                general_map = raw_settings["general"]
            if isinstance(raw_settings.get("settings_config"), dict):
                settings_config = raw_settings["settings_config"]

        list_cache: Dict[str, List[Any]] = {}
        device_settings = await self._build_setting_entries(
            "device", device_map, settings_config, list_cache
        )
        general_settings = await self._build_setting_entries(
            "general", general_map, settings_config, list_cache
        )

        connected = bool(
            (status_ok and isinstance(raw_status, dict) and len(raw_status) > 0)
            or len(device_map) > 0
        )

        return {
            **sys_status,
            "connected": connected,
            "dbus_reachable": True,
            "power_status": parsed_status["power_status"],
            "batteries": parsed_status["batteries"],
            "status_items": parsed_status["status_items"],
            "device_settings": device_settings,
            "general_settings": general_settings,
            "error": None if (status_ok and settings_ok) else (status_err or settings_err),
        }

    async def set_setting(self, setting: str, value: Any) -> Dict[str, Any]:
        """Write a device or general setting over D-Bus."""
        value_json = json.dumps(value)
        ok, res, err = await self._dbus_call(
            DBUS_SETTINGS_PATH,
            DBUS_SETTINGS_IFACE,
            "SetSettings",
            "ss",
            setting,
            value_json,
        )
        if not ok and "UnknownMethod" in err:
            ok, res, err = await self._dbus_call(
                DBUS_SETTINGS_PATH,
                DBUS_SETTINGS_IFACE,
                "SetSetting",
                "ss",
                setting,
                value_json,
            )

        success = bool(ok and res is not False)
        decky.logger.info(
            f"SetSettings({setting}={value_json}) -> ok={ok}, res={res}, err={err}"
        )
        return {
            "success": success,
            "result": res,
            "error": err if not success else "",
        }

    async def set_anc_mode(
        self, value: Any, setting_key: str = "noise_cancelling"
    ) -> Dict[str, Any]:
        """Convenience setter for ANC / Transparency / Off mode."""
        return await self.set_setting(setting_key, value)

    async def set_eq_preset(
        self, value: Any, setting_key: str = "equalizer_preset"
    ) -> Dict[str, Any]:
        """Convenience setter for EQ preset."""
        return await self.set_setting(setting_key, value)

    async def reload_configs(self) -> Dict[str, Any]:
        """Invoke ReloadConfigs on the Config D-Bus interface."""
        ok, res, err = await self._dbus_call(
            DBUS_CONFIG_PATH, DBUS_CONFIG_IFACE, "ReloadConfigs"
        )
        success = bool(ok and res is not False)
        decky.logger.info(f"ReloadConfigs -> ok={ok}, res={res}, err={err}")
        return {
            "success": success,
            "message": "Device configs reloaded." if success else (err or "Reload failed."),
        }

    async def _main(self):
        self.loop = asyncio.get_event_loop()
        decky.logger.info("Decky Arctis Manager backend initialized.")

    async def _unload(self):
        decky.logger.info("Decky Arctis Manager backend unloading.")

    async def _uninstall(self):
        decky.logger.info("Decky Arctis Manager backend uninstalled.")

    async def _migration(self):
        decky.logger.info("Decky Arctis Manager migration check.")
