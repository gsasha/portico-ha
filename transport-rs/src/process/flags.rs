//! Command-line parsing that behaves like Go's `flag` package for the nine flags of
//! `cmd/portico-transport/main.go` (`flag.Parse`, ExitOnError):
//! `-flag` and `--flag`, `-flag=value`, `-flag value` (non-bool), bool flags take a value only
//! with `=`, `--` ends the flags, the first non-flag argument ends them too (the rest is
//! ignored, as Go never reads `flag.Args()`), `-h`/`-help`/`--help` print usage and exit 0, any
//! error prints the message and the usage to stderr and exits 2.

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Flags {
    pub socket: String,
    pub host: String,
    pub house_calls: bool,
    pub listen: String,
    pub ipc_token_file: String,
    pub ipc_allow_non_loopback: bool,
    pub ipc_allow_from: String,
    pub sip_from_env: bool,
    pub secrets_env: String,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Parsed {
    Ok(Flags),
    /// `-h` / `-help`: print usage, exit 0 (Go 1.15+).
    Help,
    /// A parse error: print the message, then the usage, exit 2.
    Err(String),
}

struct Def {
    name: &'static str,
    is_bool: bool,
    usage: &'static str,
    /// stores the (already normalised: `true` / `false` for a bool) value
    set: fn(&mut Flags, String),
}

/// Sorted by name, which is the order Go's `PrintDefaults` uses.
const DEFS: [Def; 9] = [
    Def { name: "host", is_bool: false, usage: "this host's own tailnet/LAN IPv4 address, for relay-mode device legs' SDP (§9.3, §9.9); empty = the local address of the SIP socket that talks to that device", set: |f, v| f.host = v },
    Def { name: "house-calls", is_bool: true, usage: "enable the house-call bridge (house_open / house_close, M7 3b-Go); off, both verbs answer not_supported and nothing is created", set: |f, v| f.house_calls = v == "true" },
    Def { name: "ipc-allow-from", is_bool: false, usage: "comma-separated IPs/CIDRs allowed to connect to the TCP IPC (the HA host)", set: |f, v| f.ipc_allow_from = v },
    Def { name: "ipc-allow-non-loopback", is_bool: true, usage: "allow -listen on a non-loopback address (also needs -ipc-allow-from)", set: |f, v| f.ipc_allow_non_loopback = v == "true" },
    Def { name: "ipc-token-file", is_bool: false, usage: "file holding the TCP IPC token (a docker secret); overrides PORTICO_IPC_TOKEN", set: |f, v| f.ipc_token_file = v },
    Def { name: "listen", is_bool: false, usage: "standalone/container mode (DEV-518 W8): serve the IPC over TCP at host:port instead of -socket; needs a token (PORTICO_IPC_TOKEN or -ipc-token-file); loopback unless -ipc-allow-non-loopback AND -ipc-allow-from", set: |f, v| f.listen = v },
    Def { name: "secrets-env", is_bool: false, usage: "read the SIP password/user/realm and the IPC token from this KEY=VALUE .env file (GATE_SIP_PASSWORD, GATE_SIP_USER, GATE_SIP_REALM, GATE_SIP_PEER_TOKEN; every other key is ignored); explicit PORTICO_* variables and -ipc-token-file override it; read once at start", set: |f, v| f.secrets_env = v },
    Def { name: "sip-from-env", is_bool: true, usage: "configure and start one gate_sip_server from PORTICO_SIP_* variables at start-up; the hub's later configure/deconfigure of it only adopt/detach", set: |f, v| f.sip_from_env = v == "true" },
    Def { name: "socket", is_bool: false, usage: "IPC unix socket path (§9.4)", set: |f, v| f.socket = v },
];

/// The text `flag.PrintDefaults` writes after "Usage of <argv0>:".
pub fn usage(argv0: &str) -> String {
    let mut s = format!("Usage of {argv0}:\n");
    for d in &DEFS {
        s.push_str("  -");
        s.push_str(d.name);
        if !d.is_bool {
            s.push_str(" string");
        }
        s.push_str("\n    \t");
        s.push_str(d.usage);
        s.push('\n');
    }
    s
}

/// Parse `args` (without argv0).
pub fn parse(args: &[String]) -> Parsed {
    let mut f = Flags::default();
    let mut i = 0;
    while i < args.len() {
        let arg = &args[i];
        // A non-flag (or a lone "-") ends parsing.
        if arg.len() < 2 || !arg.starts_with('-') {
            break;
        }
        i += 1;
        let mut name = &arg[1..];
        if name.starts_with('-') {
            name = &name[1..];
            if name.is_empty() {
                break; // "--" terminator
            }
        }
        if name.starts_with('-') || name.starts_with('=') {
            return Parsed::Err(format!("bad flag syntax: {arg}"));
        }
        let (name, value) = match name.find('=') {
            Some(p) => (&name[..p], Some(name[p + 1..].to_string())),
            None => (name, None),
        };
        if (name == "help" || name == "h") && !DEFS.iter().any(|d| d.name == name) {
            return Parsed::Help;
        }
        let Some(def) = DEFS.iter().find(|d| d.name == name) else {
            return Parsed::Err(format!("flag provided but not defined: -{name}"));
        };
        let value = if def.is_bool {
            match value {
                Some(v) => match crate::gostd::strconv::parse_bool(&v) {
                    Some(b) => b.to_string(),
                    None => {
                        return Parsed::Err(format!(
                            "invalid boolean value {} for -{name}: parse error",
                            crate::gostd::strconv::go_quote(&v)
                        ))
                    }
                },
                None => "true".to_string(),
            }
        } else {
            match value {
                Some(v) => v,
                None => {
                    if i < args.len() {
                        i += 1;
                        args[i - 1].clone()
                    } else {
                        return Parsed::Err(format!("flag needs an argument: -{name}"));
                    }
                }
            }
        };
        (def.set)(&mut f, value);
    }
    Parsed::Ok(f)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(a: &[&str]) -> Parsed {
        parse(&a.iter().map(|s| s.to_string()).collect::<Vec<_>>())
    }

    #[test]
    fn single_and_double_dash_and_equals() {
        let Parsed::Ok(f) = p(&[
            "-socket",
            "/a",
            "--host=1.2.3.4",
            "-house-calls",
            "--sip-from-env=false",
        ]) else {
            panic!()
        };
        assert_eq!(f.socket, "/a");
        assert_eq!(f.host, "1.2.3.4");
        assert!(f.house_calls);
        assert!(!f.sip_from_env);
    }

    #[test]
    fn bool_does_not_swallow_next_arg_and_first_non_flag_stops() {
        let Parsed::Ok(f) = p(&["-house-calls", "x", "-socket", "/a"]) else {
            panic!()
        };
        assert!(f.house_calls);
        assert_eq!(f.socket, "");
    }

    #[test]
    fn errors_and_help() {
        assert_eq!(
            p(&["-socket"]),
            Parsed::Err("flag needs an argument: -socket".into())
        );
        assert_eq!(
            p(&["--bogus"]),
            Parsed::Err("flag provided but not defined: -bogus".into())
        );
        assert_eq!(p(&["---x"]), Parsed::Err("bad flag syntax: ---x".into()));
        assert_eq!(p(&["-=x"]), Parsed::Err("bad flag syntax: -=x".into()));
        assert_eq!(
            p(&["--house-calls=maybe"]),
            Parsed::Err("invalid boolean value \"maybe\" for -house-calls: parse error".into())
        );
        assert_eq!(p(&["-h"]), Parsed::Help);
        assert_eq!(p(&["--help"]), Parsed::Help);
    }
}
