import {
  ButtonItem,
  DropdownItem,
  DropdownOption,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  Spinner,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FaBatteryEmpty,
  FaBatteryFull,
  FaBatteryHalf,
  FaBatteryQuarter,
  FaBatteryThreeQuarters,
  FaBolt,
  FaCheckCircle,
  FaDownload,
  FaExclamationTriangle,
  FaHeadphones,
  FaPlay,
  FaSlidersH,
  FaSync,
} from "react-icons/fa";

interface SystemStatus {
  installed: boolean;
  helper_installed: boolean;
  daemon_active: boolean;
  helper_active: boolean;
  daemon_state: string;
  helper_state: string;
  service_running: boolean;
  has_capabilities: boolean;
  daemon_path: string;
  helper_path: string;
}

interface BatteryEntry {
  level: number | null;
  source?: "dongle" | "case" | null;
  charging?: boolean | string | null;
}

interface BatteriesState {
  left?: BatteryEntry;
  right?: BatteryEntry;
  case?: BatteryEntry;
  headset?: BatteryEntry;
  dock?: BatteryEntry;
  raw_roles?: Record<string, unknown>;
}

interface StatusItem {
  category: string;
  key: string;
  label: string;
  value: unknown;
  type: string;
  role?: string | null;
}

interface SelectOption {
  value: unknown;
  label: string;
}

interface ToggleValues {
  on?: unknown;
  off?: unknown;
  on_label?: string;
  off_label?: string;
}

interface SettingEntry {
  key: string;
  label: string;
  section: "device" | "general";
  value: unknown;
  type: "toggle" | "select" | "slider" | string;
  available: boolean;
  unavailable_reason?: string | null;
  default_value?: unknown;
  min?: number | null;
  max?: number | null;
  step?: number | null;
  min_label?: string | null;
  max_label?: string | null;
  values?: ToggleValues | Record<string, unknown> | null;
  options: SelectOption[];
  category: "anc" | "transparency" | "eq" | "sidetone" | "mic" | "power" | "other" | string;
}

interface DeviceStateResponse extends SystemStatus {
  connected: boolean;
  dbus_reachable: boolean;
  power_status: string | null;
  batteries: BatteriesState;
  status_items: StatusItem[];
  device_settings: SettingEntry[];
  general_settings: SettingEntry[];
  error: string | null;
}

interface ActionResponse {
  success: boolean;
  message?: string;
  status?: SystemStatus;
}

interface SetSettingResponse {
  success: boolean;
  result?: unknown;
  error?: string;
}

const getDeviceState = callable<[], DeviceStateResponse>("get_device_state");
const installManager = callable<[], ActionResponse>("install_manager");
const restartService = callable<[], ActionResponse>("restart_service");
const setSetting = callable<[setting: string, value: unknown], SetSettingResponse>("set_setting");
const reloadConfigs = callable<[], ActionResponse>("reload_configs");

const POLL_INTERVAL_MS = 4000;

const CATEGORY_PRIORITY: Record<string, number> = {
  anc: 0,
  transparency: 1,
  eq: 2,
  sidetone: 3,
  mic: 4,
  power: 5,
  other: 6,
};

const BATTERY_ROLES = new Set([
  "battery_left",
  "battery_right",
  "battery_case",
  "battery_case_left",
  "battery_case_right",
  "battery_headset",
  "battery_dock",
]);

function formatTelemetryValue(item: StatusItem): string {
  if (item.value === null || item.value === undefined) {
    return "--";
  }
  if (typeof item.value === "boolean") {
    return item.value ? "On" : "Off";
  }
  if (item.type === "percentage" && typeof item.value === "number") {
    return `${Math.round(item.value)}%`;
  }
  return String(item.value).replace(/_/g, " ");
}

function isChargingFlag(val: boolean | string | null | undefined): boolean {
  if (typeof val === "boolean") {
    return val;
  }
  if (typeof val === "string") {
    const lower = val.toLowerCase();
    return lower === "true" || lower === "charging" || lower === "yes" || lower === "1";
  }
  return false;
}

function getBatteryColor(level: number | null): string {
  if (level === null) return "#6b7280";
  if (level > 50) return "#4ade80";
  if (level > 20) return "#facc15";
  return "#f87171";
}

function BatteryIcon({ level }: { level: number | null }) {
  const color = getBatteryColor(level);
  if (level === null) return <FaBatteryEmpty style={{ color }} />;
  if (level >= 85) return <FaBatteryFull style={{ color }} />;
  if (level >= 60) return <FaBatteryThreeQuarters style={{ color }} />;
  if (level >= 35) return <FaBatteryHalf style={{ color }} />;
  if (level >= 15) return <FaBatteryQuarter style={{ color }} />;
  return <FaBatteryEmpty style={{ color }} />;
}

interface BatteryRowProps {
  label: string;
  entry?: BatteryEntry;
}

function BatteryBarRow({ label, entry }: BatteryRowProps) {
  const level = entry?.level ?? null;
  const clamped = level !== null ? Math.max(0, Math.min(100, Math.round(level))) : null;
  const charging = isChargingFlag(entry?.charging);
  const color = getBatteryColor(clamped);

  let subtitle: string | undefined;
  if (entry?.source === "case") {
    subtitle = charging ? "In case (charging)" : "In case";
  } else if (charging) {
    subtitle = "Charging";
  }

  return (
    <PanelSectionRow>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "6px",
          width: "100%",
          padding: "4px 0",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            fontSize: "13px",
          }}
        >
          <span style={{ display: "flex", alignItems: "center", gap: "8px", fontWeight: 500 }}>
            <BatteryIcon level={clamped} />
            <span>{label}</span>
            {subtitle && (
              <span style={{ fontSize: "11px", opacity: 0.7 }}>({subtitle})</span>
            )}
          </span>
          <span
            style={{
              display: "flex",
              alignItems: "center",
              gap: "4px",
              fontWeight: 600,
              color: clamped !== null ? "#f3f4f6" : "#9ca3af",
            }}
          >
            {charging && <FaBolt style={{ color: "#facc15", fontSize: "11px" }} />}
            <span>{clamped !== null ? `${clamped}%` : "--"}</span>
          </span>
        </div>
        <div
          style={{
            width: "100%",
            height: "5px",
            borderRadius: "999px",
            backgroundColor: "rgba(255, 255, 255, 0.12)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              width: `${clamped ?? 0}%`,
              height: "100%",
              borderRadius: "999px",
              backgroundColor: color,
              transition: "width 0.25s ease",
            }}
          />
        </div>
      </div>
    </PanelSectionRow>
  );
}

interface SettingControlProps {
  setting: SettingEntry;
  disabled: boolean;
  onCommit: (key: string, value: unknown, debounceMs?: number) => void;
}

function SettingControl({ setting, disabled, onCommit }: SettingControlProps) {
  const isDisabled = disabled || !setting.available;
  const description =
    !setting.available && setting.unavailable_reason
      ? `Unavailable: ${setting.unavailable_reason.replace(/_/g, " ")}`
      : undefined;

  if (setting.type === "toggle") {
    const valuesObj = (setting.values ?? {}) as ToggleValues;
    const onVal = valuesObj.on !== undefined ? valuesObj.on : true;
    const offVal = valuesObj.off !== undefined ? valuesObj.off : false;
    const checked =
      setting.value === onVal ||
      (setting.value !== offVal && Boolean(setting.value));

    return (
      <PanelSectionRow>
        <ToggleField
          label={setting.label}
          description={description}
          tooltip={description}
          checked={checked}
          disabled={isDisabled}
          onChange={(nextChecked) =>
            onCommit(setting.key, nextChecked ? onVal : offVal)
          }
        />
      </PanelSectionRow>
    );
  }

  if (setting.type === "select") {
    const rgOptions: DropdownOption[] = setting.options.map((opt) => ({
      data: opt.value,
      label: opt.label,
    }));

    if (rgOptions.length === 0) {
      return (
        <PanelSectionRow>
          <Field label={setting.label} description={description}>
            <span>{setting.value !== null && setting.value !== undefined ? String(setting.value) : "--"}</span>
          </Field>
        </PanelSectionRow>
      );
    }

    return (
      <PanelSectionRow>
        <DropdownItem
          label={setting.label}
          description={description}
          tooltip={description}
          disabled={isDisabled}
          rgOptions={rgOptions}
          selectedOption={setting.value}
          strDefaultLabel={
            setting.value !== null && setting.value !== undefined
              ? String(setting.value)
              : "Select..."
          }
          onChange={(selected) => onCommit(setting.key, selected.data)}
        />
      </PanelSectionRow>
    );
  }

  if (setting.type === "slider") {
    const min = typeof setting.min === "number" ? setting.min : 0;
    const max = typeof setting.max === "number" ? setting.max : 100;
    const step = typeof setting.step === "number" && setting.step > 0 ? setting.step : 1;
    const value =
      typeof setting.value === "number"
        ? setting.value
        : typeof setting.default_value === "number"
          ? setting.default_value
          : min;

    return (
      <PanelSectionRow>
        <SliderField
          label={setting.label}
          description={description}
          tooltip={description}
          value={value}
          min={min}
          max={max}
          step={step}
          showValue={true}
          disabled={isDisabled}
          onChange={(nextValue) => onCommit(setting.key, nextValue, 120)}
        />
      </PanelSectionRow>
    );
  }

  return (
    <PanelSectionRow>
      <Field label={setting.label} description={description}>
        <span>{setting.value !== null && setting.value !== undefined ? String(setting.value) : "--"}</span>
      </Field>
    </PanelSectionRow>
  );
}

function Content() {
  const [state, setState] = useState<DeviceStateResponse | null>(null);
  const [initialLoading, setInitialLoading] = useState<boolean>(true);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [showGeneralSettings, setShowGeneralSettings] = useState<boolean>(false);

  const isMountedRef = useRef<boolean>(true);
  const isPollingRef = useRef<boolean>(false);
  const actionBusyRef = useRef<string | null>(null);
  const sliderTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  actionBusyRef.current = actionBusy;

  const refreshState = useCallback(async (silent = true) => {
    if (isPollingRef.current) return;
    isPollingRef.current = true;
    try {
      const next = await getDeviceState();
      if (isMountedRef.current) {
        setState(next);
      }
    } catch (err) {
      if (!silent) {
        toaster.toast({
          title: "Arctis Manager",
          body: `Failed to query daemon: ${String(err)}`,
        });
      }
    } finally {
      isPollingRef.current = false;
      if (isMountedRef.current) {
        setInitialLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    isMountedRef.current = true;
    void refreshState(true);

    const interval = setInterval(() => {
      if (!actionBusyRef.current) {
        void refreshState(true);
      }
    }, POLL_INTERVAL_MS);

    return () => {
      isMountedRef.current = false;
      clearInterval(interval);
      Object.values(sliderTimersRef.current).forEach((timerId) =>
        clearTimeout(timerId)
      );
      sliderTimersRef.current = {};
    };
  }, [refreshState]);

  const handleInstall = async () => {
    setActionBusy("installing");
    try {
      const res = await installManager();
      toaster.toast({
        title: res.success ? "Arctis Manager Installed" : "Installation Issue",
        body: res.message || (res.success ? "Ready to use." : "Check Decky logs."),
      });
      await refreshState(true);
    } catch (err) {
      toaster.toast({
        title: "Installation Failed",
        body: String(err),
      });
    } finally {
      if (isMountedRef.current) {
        setActionBusy(null);
      }
    }
  };

  const handleRestartService = async () => {
    setActionBusy("restarting");
    try {
      const res = await restartService();
      toaster.toast({
        title: res.success ? "Daemon Active" : "Service Start Failed",
        body: res.message || (res.success ? "Services started." : "Check systemctl logs."),
      });
      await refreshState(true);
    } catch (err) {
      toaster.toast({
        title: "Service Error",
        body: String(err),
      });
    } finally {
      if (isMountedRef.current) {
        setActionBusy(null);
      }
    }
  };

  const handleReloadConfigs = async () => {
    setActionBusy("reloading");
    try {
      const res = await reloadConfigs();
      toaster.toast({
        title: "Arctis Manager",
        body: res.message || (res.success ? "Configs reloaded." : "Reload failed."),
      });
      await refreshState(true);
    } catch (err) {
      toaster.toast({
        title: "Reload Failed",
        body: String(err),
      });
    } finally {
      if (isMountedRef.current) {
        setActionBusy(null);
      }
    }
  };

  const applySettingValue = useCallback(
    async (key: string, value: unknown) => {
      try {
        const res = await setSetting(key, value);
        if (!res.success) {
          toaster.toast({
            title: "Setting Not Applied",
            body: res.error || `Daemon rejected update for ${key}`,
          });
          await refreshState(true);
        }
      } catch (err) {
        toaster.toast({
          title: "D-Bus Error",
          body: String(err),
        });
        await refreshState(true);
      }
    },
    [refreshState]
  );

  const handleSettingChange = useCallback(
    (key: string, value: unknown, debounceMs = 0) => {
      // Optimistic local update for responsive QAM interaction
      setState((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          device_settings: prev.device_settings.map((s) =>
            s.key === key ? { ...s, value } : s
          ),
          general_settings: prev.general_settings.map((s) =>
            s.key === key ? { ...s, value } : s
          ),
        };
      });

      if (sliderTimersRef.current[key]) {
        clearTimeout(sliderTimersRef.current[key]);
        delete sliderTimersRef.current[key];
      }

      if (debounceMs > 0) {
        sliderTimersRef.current[key] = setTimeout(() => {
          delete sliderTimersRef.current[key];
          void applySettingValue(key, value);
        }, debounceMs);
      } else {
        void applySettingValue(key, value);
      }
    },
    [applySettingValue]
  );

  const sortedDeviceSettings = useMemo(() => {
    if (!state?.device_settings) return [];
    return [...state.device_settings].sort((a, b) => {
      const pa = CATEGORY_PRIORITY[a.category] ?? 99;
      const pb = CATEGORY_PRIORITY[b.category] ?? 99;
      return pa - pb;
    });
  }, [state?.device_settings]);

  const extraTelemetry = useMemo(() => {
    if (!state?.status_items) return [];
    const settingKeys = new Set([
      ...(state.device_settings ?? []).map((s) => s.key),
      ...(state.general_settings ?? []).map((s) => s.key),
    ]);
    return state.status_items.filter((item) => {
      if (item.role && BATTERY_ROLES.has(item.role)) return false;
      const lower = item.key.toLowerCase();
      if (
        lower === "headset_power_status" ||
        lower === "power_status" ||
        lower === "connection_status"
      ) {
        return false;
      }
      if (lower.includes("charg") && !lower.includes("battery")) {
        return false;
      }
      if (settingKeys.has(item.key)) {
        return false;
      }
      return true;
    });
  }, [state?.status_items, state?.device_settings, state?.general_settings]);

  if (initialLoading && !state) {
    return (
      <PanelSection title="Arctis Manager">
        <PanelSectionRow>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "10px",
              padding: "16px 0",
            }}
          >
            <Spinner />
            <span style={{ fontSize: "13px", opacity: 0.8 }}>
              Checking Linux Arctis Manager...
            </span>
          </div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  // State 1: Not Installed
  if (!state || !state.installed) {
    return (
      <PanelSection title="Setup Required">
        <PanelSectionRow>
          <div style={{ fontSize: "13px", lineHeight: 1.45, opacity: 0.9, padding: "4px 0" }}>
            <strong>linux-arctis-manager</strong> (<code>lam-daemon</code>) was not detected in{" "}
            <code>/home/deck/.local/bin</code>. Install it to manage your SteelSeries Arctis
            GameBuds directly from SteamOS Gaming Mode.
          </div>
        </PanelSectionRow>
        {actionBusy === "installing" && (
          <PanelSectionRow>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", padding: "6px 0" }}>
              <Spinner />
              <span style={{ fontSize: "12px", opacity: 0.85 }}>
                Downloading release, applying hidraw permissions &amp; enabling services...
              </span>
            </div>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={actionBusy !== null}
            onClick={handleInstall}
          >
            <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
              <FaDownload />
              <span>
                {actionBusy === "installing" ? "Installing..." : "Install Arctis Manager"}
              </span>
            </span>
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={actionBusy !== null}
            onClick={() => void refreshState(false)}
          >
            <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
              <FaSync />
              <span>Re-check Installation</span>
            </span>
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  // State 2A: Installed, Service Stopped
  if (!state.service_running) {
    return (
      <PanelSection title="Daemon Stopped">
        <PanelSectionRow>
          <Field label="lam-daemon.service">
            <span
              style={{
                color: state.daemon_active ? "#4ade80" : "#f87171",
                fontWeight: 600,
              }}
            >
              {state.daemon_state}
            </span>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="lam-hidraw-helper.service">
            <span
              style={{
                color: state.helper_active ? "#4ade80" : "#f87171",
                fontWeight: 600,
              }}
            >
              {state.helper_state}
            </span>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="HIDRAW Capabilities">
            <span
              style={{
                color: state.has_capabilities ? "#4ade80" : "#facc15",
                fontWeight: 600,
              }}
            >
              {state.has_capabilities ? "Configured" : "Will apply on start"}
            </span>
          </Field>
        </PanelSectionRow>
        {actionBusy === "restarting" && (
          <PanelSectionRow>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", padding: "6px 0" }}>
              <Spinner />
              <span style={{ fontSize: "12px", opacity: 0.85 }}>
                Starting systemd user services...
              </span>
            </div>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={actionBusy !== null}
            onClick={handleRestartService}
          >
            <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
              <FaPlay />
              <span>
                {actionBusy === "restarting" ? "Starting..." : "Start / Restart Daemon"}
              </span>
            </span>
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  // State 2B: Running, No Headset / Dongle Detected
  if (!state.connected) {
    return (
      <>
        <PanelSection title="Arctis GameBuds">
          <PanelSectionRow>
            <div
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: "10px",
                padding: "6px 0",
                fontSize: "13px",
                lineHeight: 1.4,
              }}
            >
              <FaExclamationTriangle style={{ color: "#facc15", marginTop: "2px", flexShrink: 0 }} />
              <div>
                <div style={{ fontWeight: 600, marginBottom: "4px" }}>
                  No Arctis Device Connected
                </div>
                <div style={{ opacity: 0.8 }}>
                  <code>lam-daemon</code> is running. Plug in your 2.4GHz USB-C dongle or charging
                  case, and take your GameBuds out of the case to connect.
                </div>
                {!state.dbus_reachable && state.error && (
                  <div style={{ marginTop: "6px", fontSize: "11px", color: "#fca5a5" }}>
                    D-Bus: {state.error}
                  </div>
                )}
              </div>
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={actionBusy !== null}
              onClick={() => void refreshState(false)}
            >
              <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
                <FaSync />
                <span>Refresh Status</span>
              </span>
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={actionBusy !== null}
              onClick={handleRestartService}
            >
              <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
                <FaPlay />
                <span>
                  {actionBusy === "restarting" ? "Restarting..." : "Restart Daemon"}
                </span>
              </span>
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      </>
    );
  }

  // State 3: Connected
  const batteries = state.batteries ?? {};
  const hasGameBudsBatteries =
    batteries.left?.level !== null && batteries.left?.level !== undefined ||
    batteries.right?.level !== null && batteries.right?.level !== undefined ||
    batteries.case?.level !== null && batteries.case?.level !== undefined;
  const hasHeadsetBattery =
    batteries.headset?.level !== null && batteries.headset?.level !== undefined;
  const hasDockBattery =
    batteries.dock?.level !== null && batteries.dock?.level !== undefined;

  return (
    <>
      <PanelSection title="Battery & Status">
        <PanelSectionRow>
          <Field label="Connection">
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
                color: "#4ade80",
                fontWeight: 600,
                textTransform: "capitalize",
              }}
            >
              <FaCheckCircle />
              <span>{state.power_status ?? "Connected"}</span>
            </span>
          </Field>
        </PanelSectionRow>

        {hasGameBudsBatteries && (
          <>
            <BatteryBarRow label="Left Bud" entry={batteries.left} />
            <BatteryBarRow label="Right Bud" entry={batteries.right} />
            <BatteryBarRow label="Charging Case" entry={batteries.case} />
          </>
        )}

        {hasHeadsetBattery && (
          <BatteryBarRow label="Headset" entry={batteries.headset} />
        )}

        {hasDockBattery && <BatteryBarRow label="Dock / Base" entry={batteries.dock} />}

        {!hasGameBudsBatteries && !hasHeadsetBattery && !hasDockBattery && (
          <PanelSectionRow>
            <Field label="Battery">
              <span style={{ opacity: 0.75 }}>Waiting for telemetry...</span>
            </Field>
          </PanelSectionRow>
        )}
      </PanelSection>

      {sortedDeviceSettings.length > 0 && (
        <PanelSection title="GameBuds Controls">
          {sortedDeviceSettings.map((setting) => (
            <SettingControl
              key={setting.key}
              setting={setting}
              disabled={actionBusy !== null}
              onCommit={handleSettingChange}
            />
          ))}
        </PanelSection>
      )}

      {extraTelemetry.length > 0 && (
        <PanelSection title="Device Telemetry">
          {extraTelemetry.map((item) => (
            <PanelSectionRow key={`${item.category}-${item.key}`}>
              <Field label={item.label}>
                <span style={{ textTransform: "capitalize" }}>
                  {formatTelemetryValue(item)}
                </span>
              </Field>
            </PanelSectionRow>
          ))}
        </PanelSection>
      )}

      {state.general_settings.length > 0 && (
        <PanelSection title="Daemon Settings">
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => setShowGeneralSettings((prev) => !prev)}
            >
              <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
                <FaSlidersH />
                <span>
                  {showGeneralSettings ? "Hide General Settings" : "Show General Settings"}
                </span>
              </span>
            </ButtonItem>
          </PanelSectionRow>
          {showGeneralSettings &&
            state.general_settings.map((setting) => (
              <SettingControl
                key={setting.key}
                setting={setting}
                disabled={actionBusy !== null}
                onCommit={handleSettingChange}
              />
            ))}
        </PanelSection>
      )}

      <PanelSection title="Service & Diagnostics">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={actionBusy !== null}
            onClick={() => void refreshState(false)}
          >
            <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
              <FaSync />
              <span>Refresh Now</span>
            </span>
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={actionBusy !== null}
            onClick={handleReloadConfigs}
          >
            <span>
              {actionBusy === "reloading" ? "Reloading..." : "Reload Device Configs"}
            </span>
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={actionBusy !== null}
            onClick={handleRestartService}
          >
            <span>
              {actionBusy === "restarting" ? "Restarting..." : "Restart Daemon"}
            </span>
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  return {
    name: "Arctis Manager",
    titleView: <div className={staticClasses.Title}>Arctis Manager</div>,
    content: <Content />,
    icon: <FaHeadphones />,
    onDismount() {},
  };
});
