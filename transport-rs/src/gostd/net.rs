//! `net`: `SplitHostPort`, `JoinHostPort` and the address resolution the SIP code uses.

/// `net.SplitHostPort`. Errors are Go's texts.
pub fn split_host_port(hostport: &str) -> Result<(&str, &str), String> {
    // Go's AddrError.Error() leaves the `address X: ` prefix out when X is empty (R2-60)
    let err = |why: &str| {
        Err(if hostport.is_empty() {
            why.to_string()
        } else {
            format!("address {hostport}: {why}")
        })
    };
    let Some(i) = hostport.rfind(':') else {
        return err("missing port in address");
    };
    let (host, j, k);
    if hostport.starts_with('[') {
        let Some(end) = hostport.find(']') else {
            return err("missing ']' in address");
        };
        if end + 1 == hostport.len() {
            return err("missing port in address");
        }
        if end + 1 != i {
            return if hostport.as_bytes()[end + 1] == b':' {
                err("too many colons in address")
            } else {
                err("missing port in address")
            };
        }
        host = &hostport[1..end];
        j = 1;
        k = end + 1;
    } else {
        let h = &hostport[..i];
        if h.contains(':') {
            return err("too many colons in address");
        }
        host = h;
        j = 0;
        k = 0;
    }
    if hostport[j..].contains('[') {
        return err("unexpected '[' in address");
    }
    if hostport[k..].contains(']') {
        return err("unexpected ']' in address");
    }
    Ok((host, &hostport[i + 1..]))
}

/// `net.JoinHostPort`: a host with a colon (an IPv6 literal) is bracketed.
pub fn join_host_port(host: &str, port: impl std::fmt::Display) -> String {
    if host.contains(':') {
        format!("[{host}]:{port}")
    } else {
        format!("{host}:{port}")
    }
}

/// `localHost(addr)`: the host part of `host:port`, or `addr` unchanged if it does not split.
pub fn local_host(addr: &str) -> String {
    match split_host_port(addr) {
        Ok((h, _)) => h.to_string(),
        Err(_) => addr.to_string(),
    }
}

/// `net.ResolveUDPAddr("udp", host:port)` for a numeric port: the first IPv4 address of the
/// host if there is one, else the first address.
pub(crate) async fn resolve_udp(host: &str, port: i64) -> Result<std::net::SocketAddr, String> {
    let joined = format!("{host}:{port}");
    let (h, p) = split_host_port(&joined)?;
    // Go (`net.LookupPort` through `ResolveUDPAddr`): `address 70000: invalid port`, also for a
    // negative number.
    let p: u16 = p
        .parse()
        .map_err(|_| format!("address {p}: invalid port"))?;
    let addrs: Vec<_> = tokio::net::lookup_host((h, p))
        .await
        .map_err(|e| crate::gojson::io_err(&e))?
        .collect();
    addrs
        .iter()
        .find(|a| a.is_ipv4())
        .or_else(|| addrs.first())
        .copied()
        .ok_or_else(|| format!("lookup {h}: no such host"))
}

#[cfg(test)]
mod tests {
    use super::*;

    // R1-75: Go's text for a port outside 0..=65535 (confirmed with a Go program on 1.23).
    #[tokio::test]
    async fn resolve_udp_port_error_text_is_gos() {
        for p in [70000i64, -1, 65536] {
            assert_eq!(
                resolve_udp("127.0.0.1", p).await.unwrap_err(),
                format!("address {p}: invalid port")
            );
        }
        assert!(resolve_udp("127.0.0.1", 5060).await.is_ok());
    }

    #[test]
    fn split_host_port_cases() {
        assert_eq!(split_host_port("127.0.0.1:5060"), Ok(("127.0.0.1", "5060")));
        assert_eq!(split_host_port("[::1]:80"), Ok(("::1", "80")));
        assert_eq!(split_host_port(":80"), Ok(("", "80")));
        assert_eq!(split_host_port("a:"), Ok(("a", "")));
        assert!(split_host_port("::1:5060").is_err());
        assert!(split_host_port("nohost").is_err());
        // R2-88: the error texts are Go's AddrError texts, pinned here next to the function
        // (ipc/tcp.rs pins them again through its caller)
        assert_eq!(
            split_host_port(""),
            Err("missing port in address".to_string())
        ); // R2-60: no prefix
        for (input, why) in [
            ("a", "missing port in address"),
            ("[::1]", "missing port in address"),
            ("[::1]x:80", "missing port in address"),
            ("[::1", "missing ']' in address"),
            ("a:b:c", "too many colons in address"),
            ("::1:5060", "too many colons in address"),
            ("[::1]:80:1", "too many colons in address"),
            ("a[:80", "unexpected '[' in address"),
            ("a]:80", "unexpected ']' in address"),
        ] {
            assert_eq!(
                split_host_port(input),
                Err(format!("address {input}: {why}")),
                "{input}"
            );
        }
        assert_eq!(local_host("[::1]:80"), "::1");
        assert_eq!(local_host("weird"), "weird");
    }

    #[test]
    fn join_host_port_brackets_a_colon_host() {
        assert_eq!(join_host_port("a", 1), "a:1");
        assert_eq!(join_host_port("::1", "80"), "[::1]:80");
    }
}
