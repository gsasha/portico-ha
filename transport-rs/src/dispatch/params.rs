//! Parameter specs, lane keys and the registry id of a request (`paramKey`, `configureKey`, `deviceParams`, `registryID`).

use super::*;

pub(super) const CONFIGURE_PARAMS: [Spec; 2] = [
    Spec {
        json: "driver",
        kind: Kind::Str,
    },
    Spec {
        json: "config",
        kind: Kind::Map,
    },
];
pub(super) const DEVICE_PARAMS: [Spec; 3] = [
    Spec {
        json: "device_id",
        kind: Kind::Str,
    },
    Spec {
        json: "host",
        kind: Kind::Str,
    },
    Spec {
        json: "port",
        kind: Kind::Int,
    },
];
pub(super) const HANGUP_PARAMS: [Spec; 2] = [
    Spec {
        json: "call_id",
        kind: Kind::Str,
    },
    Spec {
        json: "reason",
        kind: Kind::Str,
    },
];
pub(crate) const WEBRTC_PARAMS: [Spec; 3] = [
    Spec {
        json: "call_id",
        kind: Kind::Str,
    },
    Spec {
        json: "sdp",
        kind: Kind::Str,
    },
    Spec {
        json: "candidate",
        kind: Kind::Str,
    },
];
pub(super) const EXTERNAL_RING_PARAMS: [Spec; 3] = [
    Spec {
        json: "device_id",
        kind: Kind::Str,
    },
    Spec {
        json: "call_id",
        kind: Kind::Str,
    },
    Spec {
        json: "hold",
        kind: Kind::Float,
    },
];
pub(super) const TALK_HEALTH_PARAMS: [Spec; 1] = [Spec {
    json: "device_id",
    kind: Kind::Str,
}];

pub(crate) fn bad_params(msg: impl Into<String>) -> IpcError {
    IpcError::new("bad_params", msg)
}

/// `paramKey`: `field:value` for a non-empty string member, else `""` (no lane). The member is
/// decoded as Go decodes a string, so a lone surrogate escape (`"a\ud800"`) becomes U+FFFD and
/// still has a lane (R2-55); a duplicate key is won by the last one, as in a Go map.
pub(super) fn param_key(raw: &str, field: &str) -> String {
    let Some(entries) = crate::ipc::parse_object_entries(raw.trim().as_bytes()) else {
        return String::new();
    };
    let Some((_, v)) = entries.iter().rev().find(|(k, _)| k == field) else {
        return String::new();
    };
    let text = v.get();
    if !text.starts_with('"') {
        return String::new();
    }
    match gojson::unquote(text) {
        s if !s.is_empty() => format!("{field}:{s}"),
        _ => String::new(),
    }
}

/// R1-120: every string a hub `configure` carries under `password`, `token` or a key ending in
/// `_password`, `_token` or `_api_key` is registered with the panic hook, so a panic text can never print it (the drivers
/// register their own too; this is the one place every config passes).
pub(super) fn register_config_secrets(config: &serde_json::Map<String, Value>) {
    for (k, v) in config {
        if k == "password"
            || k == "token"
            || k.ends_with("_api_key")
            || k.ends_with("_password")
            || k.ends_with("_token")
        {
            if let Value::String(s) = v {
                crate::panic_hook::register_secret(s);
            }
        }
    }
}

pub fn call_id_key(raw: &str) -> String {
    param_key(raw, "call_id")
}

pub fn device_id_key(raw: &str) -> String {
    param_key(raw, "device_id")
}

/// `configureKey`: the id of the device a configure names, which lives NESTED in `config`.
/// Shares `device_id_key`'s key space so a configure and a deconfigure of one door queue.
pub fn configure_key(raw: &str) -> String {
    let Ok(vals) = gojson::decode(
        raw,
        "keyProbe",
        &[Spec {
            json: "config",
            kind: Kind::Map,
        }],
    ) else {
        return String::new();
    };
    let Some(Some(Value::Object(cfg))) = vals.first() else {
        return String::new();
    };
    for field in ["device_id", "host", "nvr_host"] {
        if let Some(Value::String(id)) = cfg.get(field) {
            if !id.is_empty() {
                return format!("device_id:{id}");
            }
        }
    }
    String::new()
}

/// `deviceParams`: addresses an already-configured device for dial/answer/open_relay/hangup.
pub(super) struct DeviceParams {
    pub(super) device_id: String,
    pub(super) host: String,
    pub(super) port: i64,
}

impl DeviceParams {
    pub(super) fn decode(params: &Params) -> Result<DeviceParams, IpcError> {
        let v =
            gojson::decode(raw_str(params), "deviceParams", &DEVICE_PARAMS).map_err(bad_params)?;
        Ok(DeviceParams {
            device_id: gojson::str_at(&v, 0),
            host: gojson::str_at(&v, 1),
            port: match v.get(2) {
                Some(Some(n)) => n.as_i64().unwrap_or(0),
                _ => 0,
            },
        })
    }

    /// `deviceParams.deviceKey()`: the device_id, or the host when there is none.
    pub(super) fn key(&self) -> String {
        if self.device_id.is_empty() {
            self.host.clone()
        } else {
            self.device_id.clone()
        }
    }

    pub(super) fn target(&self) -> MediaTarget {
        MediaTarget {
            host: self.host.clone(),
            port: self.port,
        }
    }
}

/// `registryID`: the key a configured driver is stored under, and the device_id
/// dial/answer/open_relay must then name.
pub(super) fn registry_id(d: &dyn Driver, cfg: &serde_json::Map<String, Value>) -> String {
    if let Some(id) = d.device_id().filter(|id| !id.is_empty()) {
        return id;
    }
    let host = |k: &str| match cfg.get(k) {
        Some(Value::String(s)) => s.clone(),
        _ => String::new(),
    };
    let h = host("host");
    if h.is_empty() {
        host("nvr_host") // unifi_protect's own id field
    } else {
        h
    }
}

#[cfg(test)]
mod secret_key_tests {
    #[test]
    fn config_keys_ending_in_password_or_token_are_registered() {
        let c = serde_json::json!({
            "rtsp_password": "rtsp-pw-secret-9917",
            "peer_token": "peer-token-secret-9918",
            "protect_api_key": "api-key-secret-9919",
            "host": "plain-host-9920",
        });
        super::register_config_secrets(c.as_object().unwrap());
        let t = crate::panic_hook::scrub(
            "rtsp-pw-secret-9917 peer-token-secret-9918 api-key-secret-9919 plain-host-9920",
        );
        assert_eq!(t, "*** *** *** plain-host-9920");
    }
}
